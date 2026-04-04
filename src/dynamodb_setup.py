"""DynamoDB setup utilities for the Employee ETL pipeline."""

import logging
import time

import boto3
from botocore.exceptions import ClientError

from src.config import Config

logger = logging.getLogger(__name__)

TABLE_KEY_SCHEMA = [{"AttributeName": "EmployeeID", "KeyType": "HASH"}]
TABLE_ATTRIBUTE_DEFINITIONS = [{"AttributeName": "EmployeeID", "AttributeType": "N"}]

_POLL_INTERVAL_SECONDS = 5


class TableActivationTimeoutError(Exception):
    """Raised when the DynamoDB table does not become ACTIVE within the timeout."""


class TableNotFoundError(Exception):
    """Raised when the DynamoDB table does not exist."""


class BillingModeError(Exception):
    """Raised when an existing table uses PROVISIONED billing instead of PAY_PER_REQUEST."""


def assert_pay_per_request(config: Config) -> None:
    """Pre-flight guard that fails fast if table billing mode is PROVISIONED."""
    dynamodb = _session(config).client("dynamodb")
    table_name = config.DYNAMODB_TABLE_NAME
    try:
        desc = dynamodb.describe_table(TableName=table_name)["Table"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            return
        raise

    billing = desc.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED")
    if billing != "PAY_PER_REQUEST":
        raise BillingModeError(
            f"Table '{table_name}' uses {billing}. This pipeline requires PAY_PER_REQUEST."
        )


def _session(config: Config) -> boto3.Session:
    return boto3.Session(profile_name=config.aws_profile, region_name=config.aws_region)


def create_table(config: Config) -> str:
    """Create the DynamoDB Employees table with PAY_PER_REQUEST billing.

    Returns:
        The table ARN.

    Raises:
        BillingModeError: If the table already exists but uses PROVISIONED billing.
    """
    dynamodb = _session(config).client("dynamodb")
    table_name = config.DYNAMODB_TABLE_NAME

    try:
        response = dynamodb.create_table(
            TableName=table_name,
            KeySchema=TABLE_KEY_SCHEMA,
            AttributeDefinitions=TABLE_ATTRIBUTE_DEFINITIONS,
            BillingMode="PAY_PER_REQUEST",
        )
        arn = response["TableDescription"]["TableArn"]
        logger.info("Created DynamoDB table '%s' (ARN: %s)", table_name, arn)
        return arn
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "ResourceInUseException":
            # Table already exists – check billing mode
            desc = dynamodb.describe_table(TableName=table_name)["Table"]
            billing = desc.get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED")
            if billing == "PROVISIONED":
                raise BillingModeError(
                    f"Table '{table_name}' already exists with PROVISIONED billing. "
                    "This pipeline requires PAY_PER_REQUEST."
                ) from exc
            arn = desc["TableArn"]
            logger.info(
                "DynamoDB table '%s' already exists (PAY_PER_REQUEST). ARN: %s",
                table_name,
                arn,
            )
            return arn
        raise


def wait_for_table_active(config: Config, timeout_seconds: int = 60) -> None:
    """Poll until the DynamoDB table reaches ACTIVE status.

    Args:
        config: Pipeline configuration.
        timeout_seconds: Maximum seconds to wait before raising.

    Raises:
        TableActivationTimeoutError: If the table is not ACTIVE within ``timeout_seconds``.
    """
    elapsed = 0
    while elapsed < timeout_seconds:
        status = get_table_status(config)
        logger.info("DynamoDB table '%s' status: %s", config.DYNAMODB_TABLE_NAME, status)
        if status == "ACTIVE":
            logger.info("DynamoDB table '%s' is ACTIVE.", config.DYNAMODB_TABLE_NAME)
            return
        time.sleep(_POLL_INTERVAL_SECONDS)
        elapsed += _POLL_INTERVAL_SECONDS

    raise TableActivationTimeoutError(
        f"DynamoDB table '{config.DYNAMODB_TABLE_NAME}' did not become ACTIVE "
        f"within {timeout_seconds} seconds."
    )


def get_table_status(config: Config) -> str:
    """Return the current status of the DynamoDB table.

    Raises:
        TableNotFoundError: If the table does not exist.
    """
    dynamodb = _session(config).client("dynamodb")
    table_name = config.DYNAMODB_TABLE_NAME

    try:
        response = dynamodb.describe_table(TableName=table_name)
        return response["Table"]["TableStatus"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ResourceNotFoundException":
            raise TableNotFoundError(f"DynamoDB table '{table_name}' does not exist.") from exc
        raise
