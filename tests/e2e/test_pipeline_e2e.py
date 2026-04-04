"""
End-to-end tests for the deployed Phase 2 pipeline.

Requires live AWS credentials and a completed deploy.sh run (outputs.json must exist).

Run:
    pytest tests/e2e/test_pipeline_e2e.py -v -m e2e
"""

import json
import os

import boto3
import pytest

# ── Load stack outputs ────────────────────────────────────────────────────────

OUTPUTS_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "outputs.json")


def _load_outputs() -> dict:
    if not os.path.exists(OUTPUTS_FILE):
        pytest.skip("outputs.json not found — run deploy.sh first")
    with open(OUTPUTS_FILE) as f:
        data = json.load(f)
    stack = data.get("HrPipelineStack", {})
    if not stack:
        pytest.skip("HrPipelineStack not found in outputs.json")
    return stack


@pytest.fixture(scope="session")
def stack():
    return _load_outputs()


@pytest.fixture(scope="session")
def s3(stack):
    return boto3.client("s3", region_name="us-east-1")


@pytest.fixture(scope="session")
def dynamodb(stack):
    return boto3.client("dynamodb", region_name="us-east-1")


@pytest.fixture(scope="session")
def lambda_client(stack):
    return boto3.client("lambda", region_name="us-east-1")


pytestmark = pytest.mark.e2e


# ── S3 Tests ──────────────────────────────────────────────────────────────────


class TestS3Contents:
    EXPECTED_CSVS = [
        "employee_data_updated.csv",
        "managers_data.csv",
        "departments_data.csv",
    ]

    def test_raw_bucket_exists(self, stack, s3):
        bucket = stack["RawBucketName"]
        response = s3.head_bucket(Bucket=bucket)
        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200

    @pytest.mark.parametrize("key", EXPECTED_CSVS)
    def test_csv_present_in_raw_bucket(self, stack, s3, key):
        bucket = stack["RawBucketName"]
        response = s3.head_object(Bucket=bucket, Key=key)
        assert response["ContentLength"] > 0, f"{key} should not be empty"

    def test_parquet_output_exists(self, stack, s3):
        bucket = stack["ParquetBucketName"]
        response = s3.list_objects_v2(Bucket=bucket, Prefix="employees/", MaxKeys=1)
        assert (
            response.get("KeyCount", 0) > 0
        ), "No Parquet files found — has the Glue job run successfully?"


# ── DynamoDB Tests ────────────────────────────────────────────────────────────


class TestDynamoDB:
    def test_table_is_active(self, stack, dynamodb):
        table_name = stack["DynamoTableName"]
        response = dynamodb.describe_table(TableName=table_name)
        assert response["Table"]["TableStatus"] == "ACTIVE"

    def test_table_has_at_least_1000_items(self, stack, dynamodb):
        table_name = stack["DynamoTableName"]
        response = dynamodb.scan(
            TableName=table_name,
            Select="COUNT",
        )
        count = response["Count"]
        # Handle pagination for large tables
        while "LastEvaluatedKey" in response:
            response = dynamodb.scan(
                TableName=table_name,
                Select="COUNT",
                ExclusiveStartKey=response["LastEvaluatedKey"],
            )
            count += response["Count"]
        assert count >= 1000, f"Expected ≥1000 records, found {count}"

    def test_sample_item_has_expected_keys(self, stack, dynamodb):
        table_name = stack["DynamoTableName"]
        response = dynamodb.get_item(
            TableName=table_name,
            Key={"PK": {"S": "EMP#1001"}, "SK": {"S": "PROFILE"}},
        )
        item = response.get("Item", {})
        assert item, "EMP#1001 not found — ETL may not have written correctly"
        expected_keys = {
            "PK",
            "SK",
            "EmployeeID",
            "FirstName",
            "LastName",
            "Salary",
            "CompaRatio",
            "RequiresReview",
        }
        for key in expected_keys:
            assert key in item, f"Missing key '{key}' in DynamoDB item"

    def test_billing_mode_is_pay_per_request(self, stack, dynamodb):
        table_name = stack["DynamoTableName"]
        response = dynamodb.describe_table(TableName=table_name)
        billing = response["Table"].get("BillingModeSummary", {}).get("BillingMode", "PROVISIONED")
        assert billing == "PAY_PER_REQUEST"


# ── Lambda Tests ──────────────────────────────────────────────────────────────


class TestLambda:
    def _invoke(self, lambda_client, function_name, payload: dict) -> dict:
        response = lambda_client.invoke(
            FunctionName=function_name,
            Payload=json.dumps(payload).encode(),
        )
        raw = response["Payload"].read()
        return json.loads(raw)

    def test_valid_employee_returns_200(self, stack, lambda_client):
        fn = stack["LambdaFunctionName"]
        result = self._invoke(lambda_client, fn, {"employee_id": "1001"})
        assert result["statusCode"] == 200

    def test_response_body_is_valid_json(self, stack, lambda_client):
        fn = stack["LambdaFunctionName"]
        result = self._invoke(lambda_client, fn, {"employee_id": "1001"})
        body = json.loads(result["body"])
        assert isinstance(body, dict)

    def test_response_contains_required_fields(self, stack, lambda_client):
        fn = stack["LambdaFunctionName"]
        result = self._invoke(lambda_client, fn, {"employee_id": "1001"})
        body = json.loads(result["body"])
        required = {
            "Name",
            "Department",
            "JobTitle",
            "Manager",
            "Salary",
            "CompaRatio",
            "RequiresReview",
        }
        for field in required:
            assert field in body, f"Missing field '{field}' in Lambda response"

    def test_compa_ratio_is_numeric(self, stack, lambda_client):
        fn = stack["LambdaFunctionName"]
        result = self._invoke(lambda_client, fn, {"employee_id": "1001"})
        body = json.loads(result["body"])
        assert isinstance(body["CompaRatio"], (int, float))

    def test_requires_review_is_boolean(self, stack, lambda_client):
        fn = stack["LambdaFunctionName"]
        result = self._invoke(lambda_client, fn, {"employee_id": "1001"})
        body = json.loads(result["body"])
        assert isinstance(body["RequiresReview"], bool)

    def test_missing_id_returns_400(self, stack, lambda_client):
        fn = stack["LambdaFunctionName"]
        result = self._invoke(lambda_client, fn, {})
        assert result["statusCode"] == 400

    def test_unknown_employee_returns_404(self, stack, lambda_client):
        fn = stack["LambdaFunctionName"]
        result = self._invoke(lambda_client, fn, {"employee_id": "99999"})
        assert result["statusCode"] == 404
