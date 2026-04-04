"""IAM setup utilities for the Employee ETL pipeline."""

import json
import logging

import boto3
from botocore.exceptions import ClientError

from src.config import Config

logger = logging.getLogger(__name__)

_TRUST_POLICY = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "glue.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
)

_MANAGED_POLICIES = [
    "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole",
]


def _session(config: Config) -> boto3.Session:
    return boto3.Session(profile_name=config.aws_profile, region_name=config.aws_region)


def create_glue_role(config: Config) -> str:
    """Create the Glue IAM role. Idempotent.

    Returns:
        The role ARN.
    """
    iam = _session(config).client("iam")
    role_name = config.GLUE_IAM_ROLE_NAME

    try:
        response = iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=_TRUST_POLICY,
            Description="IAM role for the Employee ETL Glue pipeline",
        )
        arn = response["Role"]["Arn"]
        logger.info("Created IAM role: %s (%s)", role_name, arn)
        return arn
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "EntityAlreadyExists":
            logger.info("IAM role already exists: %s", role_name)
            return get_role_arn(config)
        raise


def attach_glue_policies(config: Config, role_name: str) -> None:
    """Attach required managed policies for Glue service role. Idempotent."""
    iam = _session(config).client("iam")

    for policy_arn in _MANAGED_POLICIES:
        try:
            iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
            logger.info("Attached policy %s to role %s", policy_arn, role_name)
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            # Already attached – not an error
            if code in ("EntityAlreadyExists", "PolicyNotAttachable", "DuplicateRequest"):
                logger.info(
                    "Policy %s already attached to role %s (skipping)", policy_arn, role_name
                )
            else:
                raise


def put_s3_inline_policy(config: Config, role_name: str) -> None:
    """Put an inline S3 policy scoped to the pipeline bucket.

    Idempotent by nature of put_role_policy.
    """
    iam = _session(config).client("iam")
    bucket = config.S3_BUCKET_NAME

    policy_document = json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:PutObject"],
                    "Resource": f"arn:aws:s3:::{bucket}/*",
                },
                {
                    "Effect": "Allow",
                    "Action": ["s3:ListBucket"],
                    "Resource": f"arn:aws:s3:::{bucket}",
                },
            ],
        }
    )

    iam.put_role_policy(
        RoleName=role_name,
        PolicyName="GlueS3BucketAccess",
        PolicyDocument=policy_document,
    )
    logger.info("Put inline policy GlueS3BucketAccess on role %s", role_name)


def get_role_arn(config: Config) -> str:
    """Return the ARN of the Glue IAM role.

    Raises:
        RuntimeError: If the role does not exist.
    """
    iam = _session(config).client("iam")
    role_name = config.GLUE_IAM_ROLE_NAME

    try:
        response = iam.get_role(RoleName=role_name)
        return response["Role"]["Arn"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "NoSuchEntity":
            raise RuntimeError(
                f"IAM role '{role_name}' does not exist. " "Run create_glue_role() first."
            ) from exc
        raise
