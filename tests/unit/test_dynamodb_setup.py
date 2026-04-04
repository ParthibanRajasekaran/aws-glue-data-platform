"""Unit tests for src/dynamodb_setup.py using moto."""

from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from src.dynamodb_setup import BillingModeError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config():
    """Return a Config-like object that doesn't touch real AWS."""
    from src.config import Config

    obj = object.__new__(Config)
    obj.aws_profile = "glue-learner"
    obj.aws_region = "us-east-1"
    obj.aws_account_id = "123456789012"
    obj.S3_RAW_PREFIX = "raw/employees/"
    obj.S3_SCRIPTS_PREFIX = "scripts/"
    obj.GLUE_DATABASE_NAME = "employee_db"
    obj.GLUE_CRAWLER_NAME = "employee-csv-crawler"
    obj.GLUE_IAM_ROLE_NAME = "AWSGlueRole-EmployeeETL"
    obj.DYNAMODB_TABLE_NAME = "Employees"
    obj.LOCAL_DATA_FILE = "data/employee_data.csv"
    return obj


def _moto_session(config):
    """Return a boto3 Session that works inside moto."""
    return boto3.Session(region_name=config.aws_region)


# ---------------------------------------------------------------------------
# create_table
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
def test_create_table_partition_key():
    """The Employees table should use EmployeeID (N) as the HASH key."""
    import src.dynamodb_setup as mod

    config = _make_config()

    with patch.object(mod, "_session", side_effect=_moto_session):
        mod.create_table(config)

    ddb = boto3.client("dynamodb", region_name="us-east-1")
    desc = ddb.describe_table(TableName="Employees")["Table"]

    key_schema = {k["AttributeName"]: k["KeyType"] for k in desc["KeySchema"]}
    attr_types = {a["AttributeName"]: a["AttributeType"] for a in desc["AttributeDefinitions"]}

    assert key_schema.get("EmployeeID") == "HASH"
    assert attr_types.get("EmployeeID") == "N"


@pytest.mark.unit
@mock_aws
def test_create_table_billing_mode():
    """The Employees table should be created with PAY_PER_REQUEST billing."""
    import src.dynamodb_setup as mod

    config = _make_config()

    with patch.object(mod, "_session", side_effect=_moto_session):
        mod.create_table(config)

    ddb = boto3.client("dynamodb", region_name="us-east-1")
    desc = ddb.describe_table(TableName="Employees")["Table"]

    billing = desc.get("BillingModeSummary", {}).get("BillingMode", "")
    assert billing == "PAY_PER_REQUEST"


@pytest.mark.unit
@mock_aws
def test_create_table_idempotent():
    """Calling create_table twice should not raise and should return the same ARN."""
    import src.dynamodb_setup as mod

    config = _make_config()

    with patch.object(mod, "_session", side_effect=_moto_session):
        arn1 = mod.create_table(config)
        arn2 = mod.create_table(config)

    assert arn1 == arn2
    assert "Employees" in arn1


# ---------------------------------------------------------------------------
# BillingModeError on PROVISIONED
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_billing_mode_error_on_provisioned():
    """create_table should raise BillingModeError if an existing table uses PROVISIONED billing."""
    import src.dynamodb_setup as mod

    config = _make_config()

    mock_client = MagicMock()

    error_response = {
        "Error": {"Code": "ResourceInUseException", "Message": "Table already exists"}
    }
    mock_client.create_table.side_effect = ClientError(error_response, "CreateTable")
    mock_client.describe_table.return_value = {
        "Table": {
            "TableArn": "arn:aws:dynamodb:us-east-1:123456789012:table/Employees",
            "TableStatus": "ACTIVE",
            "BillingModeSummary": {"BillingMode": "PROVISIONED"},
        }
    }

    mock_session = MagicMock()
    mock_session.client.return_value = mock_client

    with patch.object(mod, "_session", return_value=mock_session):
        with pytest.raises(BillingModeError):
            mod.create_table(config)


# ---------------------------------------------------------------------------
# wait_for_table_active
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
def test_wait_for_table_active():
    """wait_for_table_active should return once the table is ACTIVE."""
    import src.dynamodb_setup as mod

    config = _make_config()

    with patch.object(mod, "_session", side_effect=_moto_session):
        mod.create_table(config)
        # moto sets the table to ACTIVE immediately
        mod.wait_for_table_active(config, timeout_seconds=30)


@pytest.mark.unit
def test_assert_pay_per_request_raises_for_provisioned():
    """Pre-flight guard should raise if table billing mode is PROVISIONED."""
    import src.dynamodb_setup as mod

    config = _make_config()

    mock_client = MagicMock()
    mock_client.describe_table.return_value = {
        "Table": {"BillingModeSummary": {"BillingMode": "PROVISIONED"}}
    }
    mock_session = MagicMock()
    mock_session.client.return_value = mock_client

    with patch.object(mod, "_session", return_value=mock_session):
        with pytest.raises(BillingModeError):
            mod.assert_pay_per_request(config)
