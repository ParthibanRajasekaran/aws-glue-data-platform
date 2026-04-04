"""Integration tests for src/glue_setup.py – require live AWS credentials."""

import pytest

from src import glue_setup, iam_setup
from src.config import Config

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def live_config():
    """Real Config backed by live AWS (glue-learner profile)."""
    return Config()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.integration
def test_database_created(live_config):
    """create_database should create (or confirm existing) the Glue database."""
    glue_setup.create_database(live_config)

    import boto3

    glue = boto3.Session(
        profile_name=live_config.aws_profile,
        region_name=live_config.aws_region,
    ).client("glue")

    response = glue.get_database(Name=live_config.GLUE_DATABASE_NAME)
    assert response["Database"]["Name"] == live_config.GLUE_DATABASE_NAME


@pytest.mark.integration
def test_crawler_created(live_config):
    """create_crawler should create (or confirm existing) the Glue crawler."""
    role_arn = iam_setup.get_role_arn(live_config)
    glue_setup.create_crawler(live_config, role_arn)

    import boto3

    glue = boto3.Session(
        profile_name=live_config.aws_profile,
        region_name=live_config.aws_region,
    ).client("glue")

    response = glue.get_crawler(Name=live_config.GLUE_CRAWLER_NAME)
    assert response["Crawler"]["Name"] == live_config.GLUE_CRAWLER_NAME


@pytest.mark.integration
def test_crawler_reaches_ready(live_config):
    """run_crawler should block until the crawler reaches READY state without raising."""
    glue_setup.run_crawler(live_config)

    import boto3

    glue = boto3.Session(
        profile_name=live_config.aws_profile,
        region_name=live_config.aws_region,
    ).client("glue")

    response = glue.get_crawler(Name=live_config.GLUE_CRAWLER_NAME)
    assert response["Crawler"]["State"] == "READY"


@pytest.mark.integration
def test_catalog_table_registered(live_config):
    """get_catalog_table should return the table dict without raising."""
    table = glue_setup.get_catalog_table(live_config, live_config.GLUE_TABLE_NAME)
    assert table["Name"] == live_config.GLUE_TABLE_NAME
    assert table["DatabaseName"] == live_config.GLUE_DATABASE_NAME


@pytest.mark.integration
def test_catalog_table_column_count(live_config):
    """The catalogued table should have at least 10 columns (CSV has 12)."""
    table = glue_setup.get_catalog_table(live_config, live_config.GLUE_TABLE_NAME)
    columns = table.get("StorageDescriptor", {}).get("Columns", [])
    assert (
        len(columns) >= 10
    ), f"Expected at least 10 columns, got {len(columns)}: {[c['Name'] for c in columns]}"
