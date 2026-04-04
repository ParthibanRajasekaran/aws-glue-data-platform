"""Unit tests for src/s3_setup.py using moto."""

import os
import tempfile
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_config():
    """
    Return a Config-like object that doesn't touch real AWS.
    We build it manually so no STS call is made and no real profile is needed.
    """

    from src.config import Config

    # Bypass __post_init__ by creating the object then overriding attributes
    obj = object.__new__(Config)
    # Set all dataclass fields to their defaults
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
    """Return a boto3 Session that works inside moto (no profile lookup needed)."""
    return boto3.Session(region_name=config.aws_region)


# ---------------------------------------------------------------------------
# create_bucket
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
def test_create_bucket_success():
    """create_bucket should return the bucket name and the bucket should exist."""
    import src.s3_setup as mod

    config = _make_config()

    with patch.object(mod, "_session", side_effect=_moto_session):
        bucket_name = mod.create_bucket(config)

    assert bucket_name == config.S3_BUCKET_NAME

    s3 = boto3.client("s3", region_name="us-east-1")
    buckets = [b["Name"] for b in s3.list_buckets()["Buckets"]]
    assert config.S3_BUCKET_NAME in buckets


@pytest.mark.unit
@mock_aws
def test_create_bucket_idempotent():
    """Calling create_bucket twice should not raise and should return the same name."""
    import src.s3_setup as mod

    config = _make_config()

    with patch.object(mod, "_session", side_effect=_moto_session):
        first = mod.create_bucket(config)
        second = mod.create_bucket(config)

    assert first == second


# ---------------------------------------------------------------------------
# configure_bucket_security
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
def test_configure_bucket_security_versioning():
    """configure_bucket_security should enable versioning on the bucket."""
    import src.s3_setup as mod

    config = _make_config()

    with patch.object(mod, "_session", side_effect=_moto_session):
        mod.create_bucket(config)
        mod.configure_bucket_security(config)

    s3 = boto3.client("s3", region_name="us-east-1")
    versioning = s3.get_bucket_versioning(Bucket=config.S3_BUCKET_NAME)
    assert versioning.get("Status") == "Enabled"


@pytest.mark.unit
@mock_aws
def test_configure_bucket_security_public_access_blocked():
    """configure_bucket_security should block all public access."""
    import src.s3_setup as mod

    config = _make_config()

    with patch.object(mod, "_session", side_effect=_moto_session):
        mod.create_bucket(config)
        mod.configure_bucket_security(config)

    s3 = boto3.client("s3", region_name="us-east-1")
    resp = s3.get_public_access_block(Bucket=config.S3_BUCKET_NAME)
    block_cfg = resp["PublicAccessBlockConfiguration"]

    assert block_cfg["BlockPublicAcls"] is True
    assert block_cfg["IgnorePublicAcls"] is True
    assert block_cfg["BlockPublicPolicy"] is True
    assert block_cfg["RestrictPublicBuckets"] is True


@pytest.mark.unit
@mock_aws
def test_configure_bucket_security_encryption():
    """configure_bucket_security should enforce SSE-S3 default encryption."""
    import src.s3_setup as mod

    config = _make_config()

    with patch.object(mod, "_session", side_effect=_moto_session):
        mod.create_bucket(config)
        mod.configure_bucket_security(config)

    s3 = boto3.client("s3", region_name="us-east-1")
    resp = s3.get_bucket_encryption(Bucket=config.S3_BUCKET_NAME)
    rules = resp["ServerSideEncryptionConfiguration"]["Rules"]
    assert rules[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"] == "AES256"


# ---------------------------------------------------------------------------
# upload_file
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
def test_upload_file_success():
    """upload_file should return the S3 URI and the object should be reachable."""
    import src.s3_setup as mod

    config = _make_config()

    with patch.object(mod, "_session", side_effect=_moto_session):
        mod.create_bucket(config)

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
            f.write("col1,col2\nval1,val2\n")
            tmp_path = f.name

        try:
            s3_key = "raw/employees/test.csv"
            uri = mod.upload_file(config, tmp_path, s3_key)
        finally:
            os.unlink(tmp_path)

    assert uri == f"s3://{config.S3_BUCKET_NAME}/{s3_key}"

    s3 = boto3.client("s3", region_name="us-east-1")
    s3.head_object(Bucket=config.S3_BUCKET_NAME, Key=s3_key)  # no exception = exists


@pytest.mark.unit
@mock_aws
def test_upload_file_not_found():
    """upload_file should raise FileNotFoundError for a non-existent local path."""
    import src.s3_setup as mod

    config = _make_config()

    with patch.object(mod, "_session", side_effect=_moto_session):
        mod.create_bucket(config)
        with pytest.raises(FileNotFoundError):
            mod.upload_file(config, "/nonexistent/path/data.csv", "raw/employees/data.csv")


# ---------------------------------------------------------------------------
# verify_upload
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
def test_verify_upload_returns_true():
    """verify_upload should return True when the object exists."""
    import src.s3_setup as mod

    config = _make_config()

    with patch.object(mod, "_session", side_effect=_moto_session):
        mod.create_bucket(config)

        s3_key = "raw/employees/employee_data.csv"
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.put_object(Bucket=config.S3_BUCKET_NAME, Key=s3_key, Body=b"data")

        result = mod.verify_upload(config, s3_key)

    assert result is True


@pytest.mark.unit
@mock_aws
def test_verify_upload_returns_false():
    """verify_upload should return False when the object does not exist."""
    import src.s3_setup as mod

    config = _make_config()

    with patch.object(mod, "_session", side_effect=_moto_session):
        mod.create_bucket(config)
        result = mod.verify_upload(config, "raw/employees/missing.csv")

    assert result is False


@pytest.mark.unit
def test_verify_upload_raises_on_access_denied():
    """verify_upload should re-raise UploadVerificationError for AccessDenied,
    not silently return False — so real credential/permission issues are surfaced.
    """
    from unittest.mock import MagicMock

    import src.s3_setup as mod
    from src.s3_setup import UploadVerificationError

    config = _make_config()

    mock_s3 = MagicMock()
    mock_s3.head_object.side_effect = ClientError(
        {"Error": {"Code": "403", "Message": "Access Denied"}}, "HeadObject"
    )
    mock_session = MagicMock()
    mock_session.client.return_value = mock_s3

    with patch.object(mod, "_session", return_value=mock_session):
        with pytest.raises(UploadVerificationError):
            mod.verify_upload(config, "raw/employees/employee_data.csv")
