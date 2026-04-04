"""Glue Data Catalog setup utilities for the Employee ETL pipeline."""

import logging
import time

import boto3
from botocore.exceptions import ClientError

from src.config import Config

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SECONDS = 10
_MAX_POLLS = 30  # 30 × 10 s = 5 minutes


class CrawlerTimeoutError(Exception):
    """Raised when the crawler does not finish within the allowed time."""


class CrawlerFailedError(Exception):
    """Raised when the crawler finishes with a FAILED state."""


class TableNotFoundError(Exception):
    """Raised when a requested Glue catalog table does not exist."""


class CrawlerConfigurationError(Exception):
    """Raised when an existing crawler has unsafe configuration drift."""


def _session(config: Config) -> boto3.Session:
    return boto3.Session(profile_name=config.aws_profile, region_name=config.aws_region)


def create_database(config: Config) -> None:
    """Create the Glue Data Catalog database. Idempotent."""
    glue = _session(config).client("glue")
    db_name = config.GLUE_DATABASE_NAME

    try:
        glue.create_database(
            DatabaseInput={
                "Name": db_name,
                "Description": "Employee data catalog database for the ETL pipeline",
            }
        )
        logger.info("Created Glue database: %s", db_name)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "AlreadyExistsException":
            logger.info("Glue database already exists: %s", db_name)
        else:
            raise


def create_crawler(config: Config, role_arn: str) -> None:
    """Create the Glue crawler that catalogues the raw CSV data. Idempotent."""
    glue = _session(config).client("glue")
    crawler_name = config.GLUE_CRAWLER_NAME
    target_path = f"s3://{config.S3_BUCKET_NAME}/{config.S3_RAW_PREFIX}"

    try:
        glue.create_crawler(
            Name=crawler_name,
            Role=role_arn,
            DatabaseName=config.GLUE_DATABASE_NAME,
            Targets={"S3Targets": [{"Path": target_path}]},
            SchemaChangePolicy={
                "UpdateBehavior": "LOG",
                "DeleteBehavior": "LOG",
            },
            RecrawlPolicy={"RecrawlBehavior": "CRAWL_EVERYTHING"},
            Description="Crawls raw employee CSV data from S3",
        )
        logger.info("Created Glue crawler '%s' targeting %s", crawler_name, target_path)
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "AlreadyExistsException":
            logger.info("Glue crawler already exists: %s - reconciling config", crawler_name)
            existing = glue.get_crawler(Name=crawler_name)["Crawler"]
            if existing.get("Schedule"):
                raise CrawlerConfigurationError(
                    f"Crawler '{crawler_name}' has a schedule configured. "
                    "Disable the schedule before running this pipeline."
                )
            glue.update_crawler(
                Name=crawler_name,
                Role=role_arn,
                DatabaseName=config.GLUE_DATABASE_NAME,
                Targets={"S3Targets": [{"Path": target_path}]},
                SchemaChangePolicy={
                    "UpdateBehavior": "LOG",
                    "DeleteBehavior": "LOG",
                },
                RecrawlPolicy={"RecrawlBehavior": "CRAWL_EVERYTHING"},
            )
            logger.info("Reconciled crawler '%s' with desired config", crawler_name)
        else:
            raise


def run_crawler(config: Config) -> None:
    """Start the crawler and block until it completes.

    Polls every 10 seconds, up to 5 minutes.

    Raises:
        CrawlerTimeoutError: If the crawler is still running after 5 minutes.
        CrawlerFailedError: If the crawler ends in a FAILED state.
    """
    glue = _session(config).client("glue")
    crawler_name = config.GLUE_CRAWLER_NAME

    # Check current state before starting
    info = glue.get_crawler(Name=crawler_name)["Crawler"]
    state = info.get("State", "READY")

    if state == "RUNNING":
        logger.warning("Crawler '%s' is already RUNNING – waiting for completion.", crawler_name)
    else:
        if state == "STOPPING":
            logger.warning(
                "Crawler '%s' is STOPPING – waiting until READY before starting.", crawler_name
            )
            for poll in range(1, _MAX_POLLS + 1):
                time.sleep(_POLL_INTERVAL_SECONDS)
                info = glue.get_crawler(Name=crawler_name)["Crawler"]
                state = info.get("State", "UNKNOWN")
                logger.info(
                    "Crawler '%s' – pre-start poll %d/%d – state: %s",
                    crawler_name,
                    poll,
                    _MAX_POLLS,
                    state,
                )
                if state == "READY":
                    break
                if state != "STOPPING":
                    raise CrawlerFailedError(
                        f"Crawler '{crawler_name}' entered unexpected state before start: {state}"
                    )
            else:
                raise CrawlerTimeoutError(
                    f"Crawler '{crawler_name}' did not become READY within "
                    f"{_MAX_POLLS * _POLL_INTERVAL_SECONDS} seconds."
                )
        glue.start_crawler(Name=crawler_name)
        logger.info("Started Glue crawler: %s", crawler_name)

    for poll in range(1, _MAX_POLLS + 1):
        time.sleep(_POLL_INTERVAL_SECONDS)
        info = glue.get_crawler(Name=crawler_name)["Crawler"]
        current_state = info.get("State", "UNKNOWN")
        logger.info(
            "Crawler '%s' – poll %d/%d – state: %s",
            crawler_name,
            poll,
            _MAX_POLLS,
            current_state,
        )

        if current_state == "READY":
            # Finished – check last crawl for errors
            last_crawl = info.get("LastCrawl", {})
            status = last_crawl.get("Status", "")
            if status == "FAILED":
                raise CrawlerFailedError(
                    f"Crawler '{crawler_name}' finished with FAILED status. "
                    f"Error: {last_crawl.get('ErrorMessage', 'unknown')}"
                )
            logger.info("Crawler '%s' completed successfully.", crawler_name)
            return

        if current_state not in ("RUNNING", "STOPPING"):
            raise CrawlerFailedError(
                f"Crawler '{crawler_name}' entered unexpected state: {current_state}"
            )

    raise CrawlerTimeoutError(
        f"Crawler '{crawler_name}' did not complete within "
        f"{_MAX_POLLS * _POLL_INTERVAL_SECONDS} seconds."
    )


def get_catalog_table(config: Config, table_name: str) -> dict:
    """Retrieve a table definition from the Glue Data Catalog.

    Args:
        config: Pipeline configuration.
        table_name: Name of the table within the configured database.

    Returns:
        The raw table dict returned by Glue.

    Raises:
        TableNotFoundError: If the table does not exist.
    """
    glue = _session(config).client("glue")

    try:
        response = glue.get_table(
            DatabaseName=config.GLUE_DATABASE_NAME,
            Name=table_name,
        )
        table = response["Table"]
        col_count = len(table.get("StorageDescriptor", {}).get("Columns", []))
        logger.info(
            "Found catalog table %s.%s (%d columns)",
            config.GLUE_DATABASE_NAME,
            table_name,
            col_count,
        )
        return table
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("EntityNotFoundException", "EntityNotFound"):
            raise TableNotFoundError(
                f"Table '{table_name}' not found in database '{config.GLUE_DATABASE_NAME}'."
            ) from exc
        raise
