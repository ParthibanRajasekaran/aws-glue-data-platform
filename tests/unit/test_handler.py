"""
Unit tests for src/lambda/handler.py
All AWS calls are mocked — no real credentials or network required.
"""

import importlib.util
import json
import os
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

# ── Load handler module via importlib ─────────────────────────────────────────
# 'lambda' is a Python reserved keyword so we can't do `from src.lambda import handler`.
# We load it by file path instead.

_HANDLER_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "src", "lambda", "handler.py")

_mock_dynamodb = MagicMock()
_mock_ssm = MagicMock()
_mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "test-table"}}

with (
    patch("boto3.client", return_value=_mock_ssm),
    patch("boto3.resource", return_value=_mock_dynamodb),
):
    _spec = importlib.util.spec_from_file_location("handler", _HANDLER_PATH)
    _mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_mod)
    _mod._dynamodb = _mock_dynamodb

handler = _mod.handler

# ── Shared DynamoDB item fixture ──────────────────────────────────────────────

SAMPLE_ITEM = {
    "PK": "EMP#1001",
    "SK": "PROFILE",
    "EmployeeID": "1001",
    "FirstName": "Patricia",
    "LastName": "Martinez",
    "Email": "patricia@example.com",
    "DepartmentName": "Sales",
    "JobTitle": "Sales AE",
    "ManagerName": "Jennifer Jones",
    "ManagerLevel": "Director",
    "HireDate": "2019-07-09",
    "City": "Chicago",
    "State": "IL",
    "EmploymentStatus": "Contract",
    "Salary": Decimal("121131.00"),
    "CompaRatio": Decimal("0.79"),
    "HighestTitleSalary": Decimal("139838.00"),
    "RequiresReview": False,
}


# ── Tests: valid employee ID ───────────────────────────────────────────────────


class TestHandlerValidId(unittest.TestCase):
    """Handler returns 200 with the correct payload for a known employee."""

    def setUp(self):
        self._table = MagicMock()
        _mock_dynamodb.Table.return_value = self._table
        self._table.get_item.return_value = {"Item": SAMPLE_ITEM}

    def test_returns_200(self):
        result = handler({"employee_id": "1001"}, None)
        self.assertEqual(result["statusCode"], 200)

    def test_body_is_valid_json(self):
        result = handler({"employee_id": "1001"}, None)
        body = json.loads(result["body"])
        self.assertIsInstance(body, dict)

    def test_name_is_assembled(self):
        result = handler({"employee_id": "1001"}, None)
        body = json.loads(result["body"])
        self.assertEqual(body["Name"], "Patricia Martinez")

    def test_salary_is_float(self):
        result = handler({"employee_id": "1001"}, None)
        body = json.loads(result["body"])
        self.assertIsInstance(body["Salary"], float)
        self.assertAlmostEqual(body["Salary"], 121131.0)

    def test_compa_ratio_present(self):
        result = handler({"employee_id": "1001"}, None)
        body = json.loads(result["body"])
        self.assertIn("CompaRatio", body)
        self.assertAlmostEqual(body["CompaRatio"], 0.79)

    def test_requires_review_present(self):
        result = handler({"employee_id": "1001"}, None)
        body = json.loads(result["body"])
        self.assertIn("RequiresReview", body)
        self.assertFalse(body["RequiresReview"])

    def test_dynamo_called_with_correct_key(self):
        handler({"employee_id": "1001"}, None)
        self._table.get_item.assert_called_once_with(Key={"PK": "EMP#1001", "SK": "PROFILE"})


# ── Tests: missing employee_id field ─────────────────────────────────────────


class TestHandlerMissingId(unittest.TestCase):
    """Handler returns 400 when employee_id is absent or None."""

    def test_missing_id_returns_400(self):
        result = handler({}, None)
        self.assertEqual(result["statusCode"], 400)
        body = json.loads(result["body"])
        self.assertIn("error", body)

    def test_none_id_returns_400(self):
        result = handler({"employee_id": None}, None)
        self.assertEqual(result["statusCode"], 400)


# ── Tests: employee not found in DynamoDB ─────────────────────────────────────


class TestHandlerNotFound(unittest.TestCase):
    """Handler returns 404 when DynamoDB has no matching item."""

    def setUp(self):
        self._table = MagicMock()
        _mock_dynamodb.Table.return_value = self._table
        # DynamoDB returns no 'Item' key when the record doesn't exist
        self._table.get_item.return_value = {}

    def test_missing_item_returns_404(self):
        result = handler({"employee_id": "9999"}, None)
        self.assertEqual(result["statusCode"], 404)

    def test_404_body_contains_error(self):
        result = handler({"employee_id": "9999"}, None)
        body = json.loads(result["body"])
        self.assertIn("error", body)
        self.assertIn("9999", body["error"])


# ── Tests: SSM failure at cold start ─────────────────────────────────────────


class TestHandlerSsmFailure(unittest.TestCase):
    """Module-level SSM fetch raises RuntimeError when SSM is unavailable."""

    def test_ssm_failure_raises_runtime_error(self):
        from botocore.exceptions import ClientError

        failing_ssm = MagicMock()
        failing_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "not found"}},
            "GetParameter",
        )
        import importlib.util as _ilu

        with self.assertRaises(RuntimeError):
            with (
                patch("boto3.client", return_value=failing_ssm),
                patch("boto3.resource", return_value=MagicMock()),
            ):
                spec = _ilu.spec_from_file_location("handler_fail", _HANDLER_PATH)
                mod = _ilu.module_from_spec(spec)
                spec.loader.exec_module(mod)


# ── Tests: missing numeric fields ────────────────────────────────────────────


class TestHandlerMissingNumericFields(unittest.TestCase):
    """Handler returns 200 with None for missing Salary/CompaRatio fields."""

    def setUp(self):
        self._table = MagicMock()
        _mock_dynamodb.Table.return_value = self._table
        item_no_salary = {
            k: v
            for k, v in SAMPLE_ITEM.items()
            if k not in ("Salary", "CompaRatio", "HighestTitleSalary")
        }
        self._table.get_item.return_value = {"Item": item_no_salary}

    def test_missing_salary_returns_none_not_error(self):
        result = handler({"employee_id": "1001"}, None)
        self.assertEqual(result["statusCode"], 200)
        body = json.loads(result["body"])
        self.assertIsNone(body["Salary"])

    def test_missing_compa_ratio_returns_none_not_error(self):
        result = handler({"employee_id": "1001"}, None)
        body = json.loads(result["body"])
        self.assertIsNone(body["CompaRatio"])


if __name__ == "__main__":
    unittest.main()
