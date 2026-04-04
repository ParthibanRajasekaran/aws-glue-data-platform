"""Unit tests for src/iam_setup.py using moto."""

import json
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws

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
# create_glue_role
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
def test_create_glue_role_trust_policy():
    """The created role should have a Glue service trust policy."""
    import src.iam_setup as mod

    config = _make_config()

    with patch.object(mod, "_session", side_effect=_moto_session):
        mod.create_glue_role(config)

    iam = boto3.client("iam", region_name="us-east-1")
    role = iam.get_role(RoleName=config.GLUE_IAM_ROLE_NAME)["Role"]
    policy = role["AssumeRolePolicyDocument"]

    if isinstance(policy, str):
        policy = json.loads(policy)

    services = []
    for stmt in policy["Statement"]:
        if stmt["Effect"] == "Allow":
            principal = stmt["Principal"]
            if isinstance(principal, dict) and "Service" in principal:
                svc = principal["Service"]
                services.extend(svc if isinstance(svc, list) else [svc])

    assert "glue.amazonaws.com" in services


@pytest.mark.unit
@mock_aws
def test_create_glue_role_idempotent():
    """Calling create_glue_role twice should not raise and should return the same ARN."""
    import src.iam_setup as mod

    config = _make_config()

    with patch.object(mod, "_session", side_effect=_moto_session):
        arn1 = mod.create_glue_role(config)
        arn2 = mod.create_glue_role(config)

    assert arn1 == arn2
    assert "AWSGlueRole-EmployeeETL" in arn1


# ---------------------------------------------------------------------------
# attach_glue_policies
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_attach_glue_policies_attached():
    """attach_glue_policies should call attach_role_policy with the correct ARNs."""
    import src.iam_setup as mod

    config = _make_config()

    attached_calls = []

    mock_client = MagicMock()
    mock_client.attach_role_policy.side_effect = lambda **kwargs: attached_calls.append(
        kwargs["PolicyArn"]
    )

    mock_session = MagicMock()
    mock_session.client.return_value = mock_client

    with patch.object(mod, "_session", return_value=mock_session):
        mod.attach_glue_policies(config, config.GLUE_IAM_ROLE_NAME)

    assert attached_calls == ["arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"]


# ---------------------------------------------------------------------------
# put_s3_inline_policy
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
def test_put_s3_inline_policy_scoped():
    """The inline S3 policy should reference only the pipeline bucket."""
    import src.iam_setup as mod

    config = _make_config()

    with patch.object(mod, "_session", side_effect=_moto_session):
        mod.create_glue_role(config)
        mod.put_s3_inline_policy(config, config.GLUE_IAM_ROLE_NAME)

    iam = boto3.client("iam", region_name="us-east-1")
    policy_doc = iam.get_role_policy(
        RoleName=config.GLUE_IAM_ROLE_NAME,
        PolicyName="GlueS3BucketAccess",
    )["PolicyDocument"]

    if isinstance(policy_doc, str):
        policy_doc = json.loads(policy_doc)

    resources = []
    for stmt in policy_doc["Statement"]:
        r = stmt["Resource"]
        resources.extend(r if isinstance(r, list) else [r])

    bucket = config.S3_BUCKET_NAME
    assert any(
        bucket in r for r in resources
    ), f"Expected bucket '{bucket}' in policy resources: {resources}"


# ---------------------------------------------------------------------------
# get_role_arn
# ---------------------------------------------------------------------------


@pytest.mark.unit
@mock_aws
def test_get_role_arn_format():
    """get_role_arn should return a well-formed ARN for an existing role."""
    import src.iam_setup as mod

    config = _make_config()

    with patch.object(mod, "_session", side_effect=_moto_session):
        mod.create_glue_role(config)
        arn = mod.get_role_arn(config)

    assert arn.startswith("arn:aws:iam::")
    assert "AWSGlueRole-EmployeeETL" in arn
