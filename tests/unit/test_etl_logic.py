"""
Unit tests for the ETL transformation logic from src/glue/etl_job.py.

The ETL script uses module-level Glue/Spark initialisation that can't run
outside AWS. This file re-implements the pure transformation logic using a
local SparkSession so we can test it without any AWS dependencies.

Run:
    pip install pyspark
    pytest tests/unit/test_etl_logic.py -v
"""

import pytest

try:
    from pyspark.sql import SparkSession
    from pyspark.sql import functions as F
    from pyspark.sql.window import Window

    PYSPARK_AVAILABLE = True
except ImportError:
    PYSPARK_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not PYSPARK_AVAILABLE, reason="pyspark not installed — run: pip install pyspark"
)


# ── Shared SparkSession (one per test session) ────────────────────────────────


@pytest.fixture(scope="session")
def spark():
    session = (
        SparkSession.builder.master("local[1]")
        .appName("etl-unit-tests")
        .config("spark.sql.shuffle.partitions", "1")
        .config("spark.ui.enabled", "false")
        .getOrCreate()
    )
    session.sparkContext.setLogLevel("ERROR")
    yield session
    session.stop()


# ── Helper: mirror the ETL transformation logic ───────────────────────────────


def run_etl_transformations(spark, employees_data, departments_data, managers_data):
    """
    Apply the same transformations as etl_job.py using a local SparkSession.
    Returns the enriched DataFrame.
    """
    employees = spark.createDataFrame(employees_data)
    departments = spark.createDataFrame(departments_data)
    managers = spark.createDataFrame(managers_data)

    # Cast types (mirrors etl_job.py Step 2)
    employees = (
        employees.withColumn("DeptID", F.col("DeptID").cast("string"))
        .withColumn("ManagerID", F.col("ManagerID").cast("string"))
        .withColumn("Salary", F.col("Salary").cast("double"))
    )
    departments = (
        departments.withColumn("DeptID", F.col("DeptID").cast("string"))
        .withColumn("MaxSalaryRange", F.col("MaxSalaryRange").cast("double"))
        .withColumn("MinSalaryRange", F.col("MinSalaryRange").cast("double"))
    )
    managers = managers.withColumn("ManagerID", F.col("ManagerID").cast("string"))

    # Joins (mirrors etl_job.py Step 3)
    enriched = employees.join(
        departments.select("DeptID", "DepartmentName", "MaxSalaryRange", "MinSalaryRange"),
        on="DeptID",
        how="left",
    )
    enriched = enriched.join(
        managers.select("ManagerID", "ManagerName", "IsActive", "Level"),
        on="ManagerID",
        how="left",
    )

    # Window (mirrors etl_job.py Step 4)
    title_window = Window.partitionBy("JobTitle")
    enriched = enriched.withColumn(
        "HighestTitleSalary",
        F.max(F.col("Salary")).over(title_window),
    )

    # Business logic (mirrors etl_job.py Step 5)
    enriched = enriched.withColumn(
        "CompaRatio",
        F.round(F.col("Salary") / F.col("MaxSalaryRange"), 2),
    )
    # IsActive null means the LEFT JOIN found no matching manager row.
    # Unknown supervision is treated as a risk requiring HR review (mirrors etl_job.py).
    enriched = enriched.withColumn(
        "RequiresReview",
        (F.col("CompaRatio") > F.lit(1.0))
        | (F.col("IsActive") == F.lit("False"))
        | F.col("IsActive").isNull(),
    )
    return enriched


# ── Test fixtures ─────────────────────────────────────────────────────────────

DEPARTMENTS = [
    {"DeptID": "500", "DepartmentName": "Sales", "MaxSalaryRange": 100000, "MinSalaryRange": 40000},
]

MANAGERS = [
    {"ManagerID": "2000", "ManagerName": "Alice Smith", "IsActive": "True", "Level": "Director"},
    {"ManagerID": "2001", "ManagerName": "Bob Jones", "IsActive": "False", "Level": "VP"},
]


# ── Tests ─────────────────────────────────────────────────────────────────────


class TestCompaRatio:
    def test_compa_ratio_below_1_when_salary_under_max(self, spark):
        employees = [
            {
                "EmployeeID": "1",
                "DeptID": "500",
                "ManagerID": "2000",
                "JobTitle": "AE",
                "Salary": 80000,
                "FirstName": "A",
                "LastName": "B",
                "Email": "",
                "Department": "Sales",
                "Manager": "Alice",
            },
        ]
        df = run_etl_transformations(spark, employees, DEPARTMENTS, MANAGERS)
        row = df.collect()[0]
        assert row["CompaRatio"] == 0.80
        assert row["RequiresReview"] is False

    def test_compa_ratio_above_1_when_salary_exceeds_max(self, spark):
        employees = [
            {
                "EmployeeID": "2",
                "DeptID": "500",
                "ManagerID": "2000",
                "JobTitle": "AE",
                "Salary": 110000,
                "FirstName": "C",
                "LastName": "D",
                "Email": "",
                "Department": "Sales",
                "Manager": "Alice",
            },
        ]
        df = run_etl_transformations(spark, employees, DEPARTMENTS, MANAGERS)
        row = df.collect()[0]
        assert row["CompaRatio"] == 1.10
        assert row["RequiresReview"] is True, "Salary > MaxSalaryRange must flag RequiresReview"


class TestRequiresReview:
    def test_requires_review_true_when_manager_inactive(self, spark):
        """RequiresReview is True when manager.IsActive == 'False', even if salary is under max."""
        employees = [
            {
                "EmployeeID": "3",
                "DeptID": "500",
                "ManagerID": "2001",
                "JobTitle": "Rep",
                "Salary": 50000,
                "FirstName": "E",
                "LastName": "F",
                "Email": "",
                "Department": "Sales",
                "Manager": "Bob",
            },
        ]
        df = run_etl_transformations(spark, employees, DEPARTMENTS, MANAGERS)
        row = df.collect()[0]
        assert row["CompaRatio"] == 0.50  # well under max
        assert row["RequiresReview"] is True, "Inactive manager must flag RequiresReview"

    def test_requires_review_false_when_active_manager_and_under_max(self, spark):
        employees = [
            {
                "EmployeeID": "4",
                "DeptID": "500",
                "ManagerID": "2000",
                "JobTitle": "Rep",
                "Salary": 60000,
                "FirstName": "G",
                "LastName": "H",
                "Email": "",
                "Department": "Sales",
                "Manager": "Alice",
            },
        ]
        df = run_etl_transformations(spark, employees, DEPARTMENTS, MANAGERS)
        row = df.collect()[0]
        assert row["RequiresReview"] is False

    def test_requires_review_true_when_both_conditions_met(self, spark):
        """Both over-max salary AND inactive manager — still True."""
        employees = [
            {
                "EmployeeID": "5",
                "DeptID": "500",
                "ManagerID": "2001",
                "JobTitle": "AE",
                "Salary": 120000,
                "FirstName": "I",
                "LastName": "J",
                "Email": "",
                "Department": "Sales",
                "Manager": "Bob",
            },
        ]
        df = run_etl_transformations(spark, employees, DEPARTMENTS, MANAGERS)
        row = df.collect()[0]
        assert row["RequiresReview"] is True


class TestHighestTitleSalary:
    def test_window_returns_max_salary_per_job_title(self, spark):
        """HighestTitleSalary should be the max salary across all rows with the same JobTitle."""
        employees = [
            {
                "EmployeeID": "10",
                "DeptID": "500",
                "ManagerID": "2000",
                "JobTitle": "AE",
                "Salary": 80000,
                "FirstName": "K",
                "LastName": "L",
                "Email": "",
                "Department": "Sales",
                "Manager": "Alice",
            },
            {
                "EmployeeID": "11",
                "DeptID": "500",
                "ManagerID": "2000",
                "JobTitle": "AE",
                "Salary": 95000,
                "FirstName": "M",
                "LastName": "N",
                "Email": "",
                "Department": "Sales",
                "Manager": "Alice",
            },
        ]
        df = run_etl_transformations(spark, employees, DEPARTMENTS, MANAGERS)
        rows = {r["EmployeeID"]: r for r in df.collect()}
        assert rows["10"]["HighestTitleSalary"] == 95000.0
        assert rows["11"]["HighestTitleSalary"] == 95000.0

    def test_different_titles_have_independent_max(self, spark):
        employees = [
            {
                "EmployeeID": "20",
                "DeptID": "500",
                "ManagerID": "2000",
                "JobTitle": "AE",
                "Salary": 80000,
                "FirstName": "O",
                "LastName": "P",
                "Email": "",
                "Department": "Sales",
                "Manager": "Alice",
            },
            {
                "EmployeeID": "21",
                "DeptID": "500",
                "ManagerID": "2000",
                "JobTitle": "Manager",
                "Salary": 120000,
                "FirstName": "Q",
                "LastName": "R",
                "Email": "",
                "Department": "Sales",
                "Manager": "Alice",
            },
        ]
        df = run_etl_transformations(spark, employees, DEPARTMENTS, MANAGERS)
        rows = {r["EmployeeID"]: r for r in df.collect()}
        assert rows["20"]["HighestTitleSalary"] == 80000.0
        assert rows["21"]["HighestTitleSalary"] == 120000.0


class TestJoins:
    def test_department_name_joined(self, spark):
        employees = [
            {
                "EmployeeID": "30",
                "DeptID": "500",
                "ManagerID": "2000",
                "JobTitle": "AE",
                "Salary": 70000,
                "FirstName": "S",
                "LastName": "T",
                "Email": "",
                "Department": "Sales",
                "Manager": "Alice",
            },
        ]
        df = run_etl_transformations(spark, employees, DEPARTMENTS, MANAGERS)
        row = df.collect()[0]
        assert row["DepartmentName"] == "Sales"

    def test_manager_name_joined(self, spark):
        employees = [
            {
                "EmployeeID": "31",
                "DeptID": "500",
                "ManagerID": "2000",
                "JobTitle": "AE",
                "Salary": 70000,
                "FirstName": "U",
                "LastName": "V",
                "Email": "",
                "Department": "Sales",
                "Manager": "Alice",
            },
        ]
        df = run_etl_transformations(spark, employees, DEPARTMENTS, MANAGERS)
        row = df.collect()[0]
        assert row["ManagerName"] == "Alice Smith"

    def test_null_managerid_left_join_does_not_raise(self, spark):
        """Employee with no matching manager gets null ManagerName and RequiresReview=True."""
        employees = [
            {
                "EmployeeID": "32",
                "DeptID": "500",
                "ManagerID": "9999",
                "JobTitle": "AE",
                "Salary": 70000,
                "FirstName": "W",
                "LastName": "X",
                "Email": "",
                "Department": "Sales",
                "Manager": "",
            },
        ]
        df = run_etl_transformations(spark, employees, DEPARTMENTS, MANAGERS)
        row = df.collect()[0]
        assert row["ManagerName"] is None
        assert row["RequiresReview"] is True, (
            "Unmatched manager (null IsActive) must flag RequiresReview — "
            "unknown supervision is a compensation risk"
        )


class TestCompaRatioBoundary:
    def test_compa_ratio_exactly_1_does_not_require_review(self, spark):
        """salary == maxsalaryrange gives comparatio == 1.0; RequiresReview must be False."""
        employees = [
            {
                "EmployeeID": "40",
                "DeptID": "500",
                "ManagerID": "2000",
                "JobTitle": "AE",
                "Salary": 100000,
                "FirstName": "Y",
                "LastName": "Z",
                "Email": "",
                "Department": "Sales",
                "Manager": "Alice",
            },
        ]
        df = run_etl_transformations(spark, employees, DEPARTMENTS, MANAGERS)
        row = df.collect()[0]
        assert row["CompaRatio"] == 1.0
        assert row["RequiresReview"] is False, "Exactly at band max should not flag RequiresReview"


class TestCircuitBreakerNullDept:
    def test_unmatched_deptid_produces_null_comparatio(self, spark):
        """Employee whose DeptID has no match in departments gets null CompaRatio.
        The row-level circuit breaker must catch this before it reaches any sink.
        """
        employees = [
            {
                "EmployeeID": "50",
                "DeptID": "9999",  # no matching department
                "ManagerID": "2000",
                "JobTitle": "AE",
                "Salary": 70000,
                "FirstName": "AA",
                "LastName": "BB",
                "Email": "aa@example.com",
                "Department": "",
                "Manager": "Alice",
            },
        ]
        df = run_etl_transformations(spark, employees, DEPARTMENTS, MANAGERS)
        row = df.collect()[0]
        # CompaRatio = salary / null = null — circuit breaker must quarantine this row
        assert (
            row["CompaRatio"] is None
        ), "Unmatched dept produces null CompaRatio — row must be quarantined"

    def test_matched_deptid_produces_valid_comparatio(self, spark):
        """Control case: matched dept produces a numeric CompaRatio."""
        employees = [
            {
                "EmployeeID": "51",
                "DeptID": "500",
                "ManagerID": "2000",
                "JobTitle": "AE",
                "Salary": 75000,
                "FirstName": "CC",
                "LastName": "DD",
                "Email": "cc@example.com",
                "Department": "Sales",
                "Manager": "Alice",
            },
        ]
        df = run_etl_transformations(spark, employees, DEPARTMENTS, MANAGERS)
        row = df.collect()[0]
        assert row["CompaRatio"] == 0.75
        assert row["CompaRatio"] is not None
