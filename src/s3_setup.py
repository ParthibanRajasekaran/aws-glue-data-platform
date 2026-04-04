"""S3 setup utilities for the Employee ETL pipeline."""

import logging
import os

import boto3
from botocore.exceptions import ClientError

from src.config import Config

logger = logging.getLogger(__name__)


class BucketCreationError(Exception):
    """Raised when an S3 bucket cannot be created."""


def _session(config: Config) -> boto3.Session:
    return boto3.Session(profile_name=config.aws_profile, region_name=config.aws_region)


def create_bucket(config: Config) -> str:
    """Create the S3 bucket for the pipeline. Idempotent.

    Returns:
        The bucket name.

    Raises:
        BucketCreationError: If the bucket cannot be created for any reason
            other than it already existing in the same account.
    """
    s3 = _session(config).client("s3")
    bucket = config.S3_BUCKET_NAME

    try:
        if config.aws_region == "us-east-1":
            s3.create_bucket(Bucket=bucket)
        else:
            s3.create_bucket(
                Bucket=bucket,
                CreateBucketConfiguration={"LocationConstraint": config.aws_region},
            )
        logger.info("Created S3 bucket: %s", bucket)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "BucketAlreadyOwnedByYou":
            logger.info("S3 bucket already exists (owned by this account): %s", bucket)
        elif code == "BucketAlreadyExists":
            raise BucketCreationError(
                f"Bucket name '{bucket}' is already taken by another AWS account. "
                "Choose a different globally unique bucket name."
            ) from exc
        else:
            raise BucketCreationError(f"Failed to create bucket '{bucket}': {exc}") from exc

    return bucket


def configure_bucket_security(config: Config) -> None:
    """Enable baseline bucket security controls. Idempotent."""
    s3 = _session(config).client("s3")
    bucket = config.S3_BUCKET_NAME

    # Enable versioning
    s3.put_bucket_versioning(
        Bucket=bucket,
        VersioningConfiguration={"Status": "Enabled"},
    )
    logger.info("Enabled versioning on bucket: %s", bucket)

    # Block all public access
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        },
    )
    logger.info("Blocked all public access on bucket: %s", bucket)

    # Enforce SSE-S3 encryption for all new objects
    s3.put_bucket_encryption(
        Bucket=bucket,
        ServerSideEncryptionConfiguration={
            "Rules": [
                {
                    "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
                }
            ]
        },
    )
    logger.info("Enabled default SSE-S3 encryption on bucket: %s", bucket)


def upload_file(config: Config, local_path: str, s3_key: str) -> str:
    """Upload a local file to S3.

    Args:
        config: Pipeline configuration.
        local_path: Path to the local file.
        s3_key: Destination key inside the bucket.

    Returns:
        The full S3 URI, e.g. ``s3://bucket/key``.

    Raises:
        FileNotFoundError: If ``local_path`` does not exist on disk.
    """
    if not os.path.exists(local_path):
        raise FileNotFoundError(f"Local file not found: {local_path}")

    file_size = os.path.getsize(local_path)
    bucket = config.S3_BUCKET_NAME
    s3 = _session(config).client("s3")

    logger.info(
        "Uploading %s (%.1f KB) to s3://%s/%s",
        local_path,
        file_size / 1024,
        bucket,
        s3_key,
    )
    s3.upload_file(local_path, bucket, s3_key)

    uri = f"s3://{bucket}/{s3_key}"
    logger.info("Upload complete: %s", uri)
    return uri


class UploadVerificationError(Exception):
    """Raised when upload verification fails for a non-404 reason (e.g. AccessDenied)."""


def verify_upload(config: Config, s3_key: str) -> bool:
    """Check whether an object exists in the bucket.

    Returns:
        True if the object exists, False if it does not exist.

    Raises:
        UploadVerificationError: For any ClientError other than a 404/NoSuchKey
            (e.g. AccessDenied, invalid credentials, network errors).  Swallowing
            these would silently mask real pipeline configuration failures.
    """
    s3 = _session(config).client("s3")
    try:
        s3.head_object(Bucket=config.S3_BUCKET_NAME, Key=s3_key)
        logger.info("Verified object exists: s3://%s/%s", config.S3_BUCKET_NAME, s3_key)
        return True
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code in ("404", "NoSuchKey"):
            logger.warning("Object not found: s3://%s/%s", config.S3_BUCKET_NAME, s3_key)
            return False
        raise UploadVerificationError(
            f"Unexpected error verifying s3://{config.S3_BUCKET_NAME}/{s3_key}: "
            f"{code} — {exc.response['Error'].get('Message', '')}"
        ) from exc
