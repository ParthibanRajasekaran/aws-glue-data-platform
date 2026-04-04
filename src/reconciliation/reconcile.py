"""
Reconciliation script — Source CSV vs DynamoDB sink count.

Run this after each ETL pipeline execution to verify no records were silently
dropped between the S3 raw layer and the DynamoDB operational sink.

Usage:
    python src/reconciliation/reconcile.py [--threshold 0.05]

Exit codes:
    0 — counts match within threshold (or only expected DQ quarantine gap)
    1 — configuration error (missing SSM parameters)
    2 — count mismatch exceeds threshold (alarm published to CloudWatch)

The script publishes a custom CloudWatch metric:
    Namespace : HRPipeline
    MetricName: ReconciliationMismatch
    Value     : 1 if mismatch > threshold, else 0

A CloudWatch Alarm (hr-pipeline-reconciliation-mismatch) fires when this
metric is 1 and notifies the hr-pipeline-alerts SNS topic.
"""

import argparse
import sys

import boto3
from botocore.exceptions import ClientError

# ── Configuration ─────────────────────────────────────────────────────────────

_NAMESPACE = "HRPipeline"
_METRIC_NAME = "ReconciliationMismatch"
_DEFAULT_THRESHOLD = 0.05  # 5 % — allows for expected DQ quarantine gap


def _fetch_config(region: str) -> dict:
    """Fetch runtime config from SSM Parameter Store."""
    ssm = boto3.client("ssm", region_name=region)
    try:
        response = ssm.get_parameters(
            Names=[
                "/hr-pipeline/raw-bucket-name",
                "/hr-pipeline/dynamodb-table-name",
            ],
            WithDecryption=True,
        )
    except ClientError as exc:
        raise RuntimeError(
            f"Failed to fetch SSM parameters: {exc.response['Error']['Message']}"
        ) from exc

    params = {p["Name"].rsplit("/", 1)[-1]: p["Value"] for p in response["Parameters"]}
    missing = {"raw-bucket-name", "dynamodb-table-name"} - params.keys()
    if missing:
        raise RuntimeError(f"SSM parameters not found: {missing}")
    return params


# ── Source count (S3 raw CSV) ─────────────────────────────────────────────────


def count_source_rows(bucket: str, prefix: str, region: str) -> int:
    """
    Count data rows in the S3 raw CSV layer.

    Lists all .csv objects under ``prefix`` and counts newlines minus the
    header line per file.  Handles multi-file uploads (e.g. when the pipeline
    splits large CSVs across multiple uploads).
    """
    s3 = boto3.client("s3", region_name=region)
    paginator = s3.get_paginator("list_objects_v2")

    total = 0
    found_any = False
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if not key.endswith(".csv"):
                continue
            found_any = True
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            lines = body.decode("utf-8", errors="replace").splitlines()
            # Subtract 1 for the header row; ignore blank trailing lines.
            data_lines = [ln for ln in lines[1:] if ln.strip()]
            total += len(data_lines)

    if not found_any:
        raise RuntimeError(
            f"No .csv files found at s3://{bucket}/{prefix} — "
            "has the pipeline upload step run yet?"
        )
    return total


# ── Sink count (DynamoDB) ─────────────────────────────────────────────────────


def count_sink_items(table_name: str, region: str) -> int:
    """
    Count PROFILE items in DynamoDB (one per employee).

    Uses a FilterExpression scan with Select=COUNT — no item data is returned,
    so this is cheap (reads consume capacity proportional to item count only).
    For tables with > 1 MB of data this automatically paginates.
    """
    ddb = boto3.client("dynamodb", region_name=region)
    paginator = ddb.get_paginator("scan")

    total = 0
    for page in paginator.paginate(
        TableName=table_name,
        FilterExpression="SK = :sk",
        ExpressionAttributeValues={":sk": {"S": "PROFILE"}},
        Select="COUNT",
    ):
        total += page["Count"]
    return total


# ── CloudWatch metric publish ─────────────────────────────────────────────────


def publish_metric(value: int, region: str) -> None:
    """Publish ReconciliationMismatch metric (0 = clean, 1 = mismatch)."""
    cw = boto3.client("cloudwatch", region_name=region)
    cw.put_metric_data(
        Namespace=_NAMESPACE,
        MetricData=[
            {
                "MetricName": _METRIC_NAME,
                "Value": value,
                "Unit": "Count",
            }
        ],
    )


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--threshold",
        type=float,
        default=_DEFAULT_THRESHOLD,
        help=(
            "Maximum allowed fractional gap between source and sink counts "
            f"(default: {_DEFAULT_THRESHOLD:.0%}).  The gap is expected to be "
            "non-zero when the DQ circuit breaker has quarantined bad rows."
        ),
    )
    parser.add_argument(
        "--region",
        default="us-east-1",
        help="AWS region (default: us-east-1)",
    )
    args = parser.parse_args()

    try:
        config = _fetch_config(args.region)
    except RuntimeError as exc:
        print(f"[reconcile] ERROR: {exc}", file=sys.stderr)
        return 1

    raw_bucket = config["raw-bucket-name"]
    table_name = config["dynamodb-table-name"]

    print(f"[reconcile] Counting source rows in s3://{raw_bucket}/raw/employees/")
    source_count = count_source_rows(raw_bucket, "raw/employees/", args.region)
    print(f"[reconcile] Source rows : {source_count}")

    print(f"[reconcile] Counting sink items in DynamoDB table '{table_name}'")
    sink_count = count_sink_items(table_name, args.region)
    print(f"[reconcile] Sink items  : {sink_count}")

    gap = source_count - sink_count
    gap_pct = gap / source_count if source_count > 0 else 0.0
    print(f"[reconcile] Gap        : {gap} rows ({gap_pct:.1%})")

    if gap_pct > args.threshold:
        print(
            f"[reconcile] MISMATCH: gap {gap_pct:.1%} exceeds threshold "
            f"{args.threshold:.0%}. Publishing metric=1 to CloudWatch.",
            file=sys.stderr,
        )
        publish_metric(1, args.region)
        return 2

    print(
        f"[reconcile] OK: gap {gap_pct:.1%} is within threshold "
        f"{args.threshold:.0%}. Publishing metric=0 to CloudWatch."
    )
    publish_metric(0, args.region)
    return 0


if __name__ == "__main__":
    sys.exit(main())
