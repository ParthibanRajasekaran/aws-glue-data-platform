"""Orchestrates all Phase 1 setup steps for the Employee ETL pipeline."""

import logging
import time

from src import dynamodb_setup, glue_setup, iam_setup, s3_setup
from src.config import Config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s – %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


class UploadVerificationError(Exception):
    """Raised when uploaded S3 object cannot be verified."""


def _run_step(step_name: str, fn, *args, **kwargs):
    """Execute a pipeline step, logging the name and re-raising on failure."""
    logger.info(">>> Starting step: %s", step_name)
    try:
        result = fn(*args, **kwargs)
        logger.info("<<< Completed step: %s", step_name)
        return result
    except Exception as exc:
        logger.error("!!! Step '%s' failed: %s", step_name, exc)
        raise


def run() -> None:
    """Execute all Phase 1 pipeline steps in order."""

    # ── Step 1: Load Config ──────────────────────────────────────────────────
    config = _run_step("Load Config", Config)

    # ── Step 2: Create S3 Bucket ─────────────────────────────────────────────
    _run_step("Create S3 Bucket", s3_setup.create_bucket, config)

    # ── Step 3: Configure Bucket Security ────────────────────────────────────
    _run_step("Configure Bucket Security", s3_setup.configure_bucket_security, config)

    # ── Step 4: Upload CSV to S3 ──────────────────────────────────────────────
    s3_key = config.S3_RAW_PREFIX + "employee_data.csv"
    s3_uri = _run_step(
        "Upload CSV to S3",
        s3_setup.upload_file,
        config,
        config.LOCAL_DATA_FILE,
        s3_key,
    )

    # ── Step 5: Verify Upload ─────────────────────────────────────────────────
    def _assert_upload(cfg, key):
        if not s3_setup.verify_upload(cfg, key):
            raise UploadVerificationError(
                f"Upload verification failed for s3://{cfg.S3_BUCKET_NAME}/{key}"
            )

    _run_step("Verify Upload", _assert_upload, config, s3_key)

    # ── Step 6: Create Glue IAM Role ──────────────────────────────────────────
    _run_step("Create Glue IAM Role", iam_setup.create_glue_role, config)

    # ── Step 7: Attach Managed Policies ───────────────────────────────────────
    _run_step(
        "Attach Glue Managed Policies",
        iam_setup.attach_glue_policies,
        config,
        config.GLUE_IAM_ROLE_NAME,
    )

    # ── Step 8: Put S3 Inline Policy ──────────────────────────────────────────
    _run_step(
        "Put S3 Inline Policy",
        iam_setup.put_s3_inline_policy,
        config,
        config.GLUE_IAM_ROLE_NAME,
    )

    # ── Step 9: Create Glue Database ──────────────────────────────────────────
    _run_step("Create Glue Database", glue_setup.create_database, config)

    # IAM roles take ~10s to propagate before Glue can assume them
    logger.info("Waiting 15s for IAM role propagation...")
    time.sleep(15)

    # ── Step 10: Create Glue Crawler ──────────────────────────────────────────
    resolved_role_arn = _run_step("Get Role ARN", iam_setup.get_role_arn, config)
    _run_step(
        "Create Glue Crawler",
        glue_setup.create_crawler,
        config,
        resolved_role_arn,
    )

    # ── Step 11: Run Crawler (blocking) ───────────────────────────────────────
    _run_step("Run Glue Crawler", glue_setup.run_crawler, config)

    # ── Step 12: Get Catalog Table ────────────────────────────────────────────
    catalog_table = _run_step(
        "Get Catalog Table",
        glue_setup.get_catalog_table,
        config,
        config.GLUE_TABLE_NAME,
    )
    col_count = len(catalog_table.get("StorageDescriptor", {}).get("Columns", []))

    # ── Step 13: Validate existing DynamoDB billing mode ──────────────────────
    _run_step(
        "Validate DynamoDB Billing Mode",
        dynamodb_setup.assert_pay_per_request,
        config,
    )

    # ── Step 14: Create DynamoDB Table ────────────────────────────────────────
    _run_step("Create DynamoDB Table", dynamodb_setup.create_table, config)

    # ── Step 15: Wait for DynamoDB Table Active ───────────────────────────────
    _run_step(
        "Wait for DynamoDB Table Active",
        dynamodb_setup.wait_for_table_active,
        config,
    )

    # ── Step 16: Summary Report ───────────────────────────────────────────────
    print(
        f"""
=== Phase 1 Setup Complete ===

S3 Bucket     : s3://{config.S3_BUCKET_NAME}
File Uploaded : {s3_uri}
IAM Role ARN  : {resolved_role_arn}
Glue Database : {config.GLUE_DATABASE_NAME}
Catalog Table : {config.GLUE_DATABASE_NAME}.{config.GLUE_TABLE_NAME} ({col_count} columns)
DynamoDB Table: {config.DYNAMODB_TABLE_NAME} (PAY_PER_REQUEST, ACTIVE)

Phase 1 standalone provisioning is complete.
Note: the CDK-deployed Glue ETL path uses separate resources (hr_analytics
catalog, aws-glue-demo-single-table with PK/SK keys, SSM-based config).
See infrastructure/infrastructure_stack.py and deploy.sh for that path.
"""
    )


if __name__ == "__main__":
    run()
