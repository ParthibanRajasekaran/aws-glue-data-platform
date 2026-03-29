"""
HR ETL PySpark Job — Phase 4
- Config fetched from SSM Parameter Store at runtime (Zero-Trust, no hardcoded names)
- Reads raw CSVs via from_catalog() for native Glue Data Lineage + Job Bookmark support
- Broadcast joins eliminate shuffle on small lookup tables
- Silver layer: null filtering, HireDate cast
- DQ gate: IsComplete + ColumnValues + IsUnique on EmployeeID — fail-fast before any write
- Null safety: na.fill() guards on all string/double columns before DynamoDB PutItem
- Hive-partitioned Parquet output (year / month / dept) for cost-efficient Athena queries
"""
import sys

import boto3
from awsglue.context import GlueContext
from awsglue.dynamicframe import DynamicFrame
from awsglue.job import Job
from awsglue.transforms import EvaluateDataQuality
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from pyspark.sql import functions as F
from pyspark.sql.types import DateType, DoubleType, StringType
from pyspark.sql.window import Window

# ── Job initialisation ───────────────────────────────────────────────────────
args = getResolvedOptions(sys.argv, ["JOB_NAME"])

sc = SparkContext()
glueContext = GlueContext(sc)
spark = glueContext.spark_session
job = Job(glueContext)
job.init(args["JOB_NAME"], args)

# ── Configuration from SSM Parameter Store (Zero-Trust: no hardcoded names) ──

def _fetch_ssm_config() -> dict:
    client = boto3.client("ssm", region_name="us-east-1")
    response = client.get_parameters(
        Names=[
            "/hr-pipeline/raw-bucket-name",
            "/hr-pipeline/parquet-bucket-name",
            "/hr-pipeline/dynamodb-table-name",
        ],
        WithDecryption=True,
    )
    return {p["Name"].rsplit("/", 1)[-1]: p["Value"] for p in response["Parameters"]}


_ssm = _fetch_ssm_config()
PARQUET_BASE = f"s3://{_ssm['parquet-bucket-name']}"
DYNAMO_TABLE = _ssm["dynamodb-table-name"]

# ── Step 1: Read source tables via Glue Catalog (enables lineage + bookmarks) ─
# Schema is authoritative in CDK CfnTable definitions — no inferSchema scan needed.
# transformation_ctx values are required for Job Bookmark tracking per source.

employees_dyf = glueContext.create_dynamic_frame.from_catalog(
    database="hr_analytics",
    table_name="raw_employees",
    transformation_ctx="employees_source",
)
employees = employees_dyf.toDF()

departments_dyf = glueContext.create_dynamic_frame.from_catalog(
    database="hr_analytics",
    table_name="raw_departments",
    transformation_ctx="departments_source",
)
departments = departments_dyf.toDF()

managers_dyf = glueContext.create_dynamic_frame.from_catalog(
    database="hr_analytics",
    table_name="raw_managers",
    transformation_ctx="managers_source",
)
managers = managers_dyf.toDF()

# ── Step 2: Silver layer — null guard + explicit type casts ──────────────────

def clean_data(df, id_col: str):
    """Drop rows with null primary key; cast HireDate to DateType if present."""
    df = df.filter(F.col(id_col).isNotNull())
    if "HireDate" in df.columns:
        df = df.withColumn("HireDate", F.col("HireDate").cast(DateType()))
    return df


employees = clean_data(employees, "EmployeeID")

# Type normalisation: join keys must be string; Salary/ranges to double for maths
employees = (
    employees
    .withColumn("DeptID",    F.col("DeptID").cast("string"))
    .withColumn("ManagerID", F.col("ManagerID").cast("string"))
    .withColumn("Salary",    F.col("Salary").cast("double"))
)

departments = (
    departments
    .withColumn("DeptID",         F.col("DeptID").cast("string"))
    .withColumn("MaxSalaryRange", F.col("MaxSalaryRange").cast("double"))
    .withColumn("MinSalaryRange", F.col("MinSalaryRange").cast("double"))
)

managers = managers.withColumn("ManagerID", F.col("ManagerID").cast("string"))

# ── Step 3: Broadcast joins — eliminates shuffle on small lookup tables ───────
# departments: 6 rows, managers: 100 rows — both fit in executor memory
enriched = employees.join(
    F.broadcast(departments.select(
        "DeptID", "DepartmentName", "MaxSalaryRange", "MinSalaryRange", "Budget"
    )),
    on="DeptID",
    how="left",
)

enriched = enriched.join(
    F.broadcast(managers.select("ManagerID", "ManagerName", "IsActive", "Level")),
    on="ManagerID",
    how="left",
)

# ── Step 4: Window function — HighestTitleSalary ──────────────────────────────
title_window = Window.partitionBy("JobTitle")
enriched = enriched.withColumn(
    "HighestTitleSalary",
    F.max(F.col("Salary")).over(title_window),
)

# ── Step 5: Business logic ────────────────────────────────────────────────────
enriched = enriched.withColumn(
    "CompaRatio",
    F.round(F.col("Salary") / F.col("MaxSalaryRange"), 2),
)

# IsActive is stored as the string "True" / "False" — never cast to boolean
enriched = enriched.withColumn(
    "RequiresReview",
    (F.col("CompaRatio") > F.lit(1.0)) | (F.col("IsActive") == F.lit("False")),
)

# ── Step 6: Data Quality gate — fail-fast before writing to any sink ──────────
dq_ruleset = """
    Rules = [
        IsComplete "EmployeeID",
        ColumnValues "Salary" > 0,
        IsUnique "EmployeeID"
    ]
"""

enriched_dyf = DynamicFrame.fromDF(enriched, glueContext, "enriched_dyf")

dq_result = EvaluateDataQuality.apply(
    frame=enriched_dyf,
    ruleset=dq_ruleset,
    publishing_options={
        "dataQualityEvaluationContext": "hr_etl_dq",
        "enableDataQualityResultsPublishing": True,
    },
    additional_options={"performanceTuning.caching": "CACHE_NOTHING"},
)

failed_rules = (
    dq_result.select_fields(["Rule", "Outcome"])
    .toDF()
    .filter(F.col("Outcome") == F.lit("Failed"))
)
if failed_rules.count() > 0:
    failed_rules.show(truncate=False)
    raise Exception("Data quality checks failed — aborting job before any writes.")

# ── Step 7: DynamoDB sink via native Glue DynamicFrame connector ──────────────
# DateType not serialisable by the DDB connector — cast HireDate back to string.
# PK/SK added as DynamoDB composite key attributes.
dynamo_df = (
    enriched
    .withColumn("HireDate", F.col("HireDate").cast("string"))
    .withColumn("PK", F.concat(F.lit("EMP#"), F.col("EmployeeID").cast("string")))
    .withColumn("SK", F.lit("PROFILE"))
)

# Explicit null guard: a null DateType casts to a null StringType;
# name the column directly rather than relying on schema inspection.
dynamo_df = dynamo_df.na.fill("", ["HireDate"])

# Null safety: DynamoDB rejects null attribute values; fill with safe defaults.
# LEFT JOINs above can produce nulls for unmatched departments/managers rows.
dynamo_df = dynamo_df.na.fill(
    "",
    [f.name for f in dynamo_df.schema.fields if isinstance(f.dataType, StringType)],
)
dynamo_df = dynamo_df.na.fill(
    0.0,
    [f.name for f in dynamo_df.schema.fields if isinstance(f.dataType, DoubleType)],
)

glueContext.write_dynamic_frame.from_options(
    frame=DynamicFrame.fromDF(dynamo_df, glueContext, "dynamo_dyf"),
    connection_type="dynamodb",
    connection_options={
        "dynamodb.output.tableName": DYNAMO_TABLE,
        "dynamodb.throughput.write.percent": "0.5",
    },
)

# ── Step 8: Parquet sink — Hive-partitioned for cost-efficient Athena queries ─
# HireDate is still DateType in `enriched`; year/month extracted before write.
parquet_df = (
    enriched
    .withColumn("year",  F.year(F.col("HireDate")))
    .withColumn("month", F.month(F.col("HireDate")))
    .withColumn("dept",  F.col("DeptID"))
)

(
    parquet_df.write
    .mode("overwrite")
    .partitionBy("year", "month", "dept")
    .parquet(f"{PARQUET_BASE}/employees/")
)

# REQUIRED: without this Glue marks the job run as failed
job.commit()
