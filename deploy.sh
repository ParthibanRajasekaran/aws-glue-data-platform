#!/bin/bash
# deploy.sh — Phase 3 full deployment
# Usage: bash deploy.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "========================================"
echo " Phase 3 — AWS CDK Deploy"
echo "========================================"

# ── 1. Install CDK CLI ────────────────────────────────────────────────────────
if ! command -v cdk &>/dev/null; then
    echo "[1/7] Installing AWS CDK CLI..."
    npm install -g aws-cdk
else
    echo "[1/7] CDK CLI already installed: $(cdk --version)"
fi

# ── 2. Install Python CDK dependencies ───────────────────────────────────────
echo "[2/7] Installing Python CDK dependencies..."
pip install -r infrastructure/requirements.txt --quiet

# ── 3. CDK Bootstrap (idempotent — safe to run on every deploy) ──────────────
echo "[3/7] Bootstrapping CDK (us-east-1)..."
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text --region us-east-1)
cdk bootstrap "aws://${ACCOUNT_ID}/us-east-1" --quiet

# ── 4. CDK Deploy ─────────────────────────────────────────────────────────────
echo "[4/7] Deploying HrPipelineStack..."
cdk deploy HrPipelineStack \
    --require-approval never \
    --outputs-file outputs.json \
    --app "python3 infrastructure/app.py"

echo "Stack deployed. Outputs written to outputs.json."

# ── 5. Parse stack outputs ────────────────────────────────────────────────────
RAW_BUCKET=$(jq -r '.HrPipelineStack.RawBucketName' outputs.json)
PARQUET_BUCKET=$(jq -r '.HrPipelineStack.ParquetBucketName' outputs.json)
GLUE_JOB=$(jq -r '.HrPipelineStack.GlueJobName' outputs.json)
LAMBDA_FN=$(jq -r '.HrPipelineStack.LambdaFunctionName' outputs.json)
DYNAMO_TABLE=$(jq -r '.HrPipelineStack.DynamoTableName' outputs.json)

echo ""
echo "  Raw bucket    : $RAW_BUCKET"
echo "  Parquet bucket: $PARQUET_BUCKET"
echo "  Glue job      : $GLUE_JOB"
echo "  Lambda        : $LAMBDA_FN"
echo "  DynamoDB table: $DYNAMO_TABLE"
echo ""

# ── 6. Upload CSVs to raw bucket (per-subdirectory — matches CfnTable locations)
echo "[6/7] Uploading CSVs to s3://$RAW_BUCKET/raw/..."
aws s3 cp data/employee_data_updated.csv "s3://${RAW_BUCKET}/raw/employees/employee_data_updated.csv" --region us-east-1
aws s3 cp data/managers_data.csv         "s3://${RAW_BUCKET}/raw/managers/managers_data.csv"          --region us-east-1
aws s3 cp data/departments_data.csv      "s3://${RAW_BUCKET}/raw/departments/departments_data.csv"    --region us-east-1
echo "CSVs uploaded."

# ── 7. Start Glue ETL job ─────────────────────────────────────────────────────
echo "[7/7] Starting Glue job: $GLUE_JOB..."
RUN_ID=$(aws glue start-job-run \
    --job-name "$GLUE_JOB" \
    --region us-east-1 \
    --query 'JobRunId' \
    --output text)

echo ""
echo "========================================"
echo " Deploy complete!"
echo "========================================"
echo ""
echo "  Glue job run ID: $RUN_ID"
echo ""
echo "  Monitor job:  aws glue get-job-run --job-name '$GLUE_JOB' --run-id '$RUN_ID' --region us-east-1 --query 'JobRun.JobRunState'"
echo "  Verify API:   bash verify_api.sh"
echo ""
echo "  The Glue job typically takes 3-5 minutes to complete."
echo "  After it succeeds, run verify_api.sh to test the Lambda."

# ── Cleanup (run manually when done) ─────────────────────────────────────────
# cdk destroy HrPipelineStack --app "python3 infrastructure/app.py"
# Note: KMS key enters a 7-day scheduled deletion window after stack destroy.
