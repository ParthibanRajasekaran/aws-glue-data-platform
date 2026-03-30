"""
HR ETL PySpark Job — Phase 4
- Config fetched from SSM Parameter Store at runtime (Zero-Trust, no hardcoded names)
- Reads raw CSVs via from_catalog() for native Glue Data Lineage + Job Bookmark support
- Column names normalised to lowercase immediately after catalog read (Glue Catalog
  lowercases all names; mixed-case references cause AnalysisException at runtime)
- Broadcast joins eliminate shuffle on small lookup tables
- Silver layer: null filtering, hiredate cast
- DQ gate: IsComplete + ColumnValues + IsUnique on employeeid — fail-fast before any write
- Null safety: na.fill() guards on all string/double columns before DynamoDB PutItem
- Hive-partitioned Parquet output (year / month / dept) for cost-efficient Athena queries
"""
import os
import sys

import boto3
from awsglue.context import GlueContext
from awsglue.dynamicframe import DynamicFrame
from awsglue.job import Job
from awsgluedq.transforms import EvaluateDataQuality
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
logger = glueContext.get_logger()

# ── Configuration from SSM Parameter Store (Zero-Trust: no hardcoded names) ──

def _fetch_ssm_config() -> dict:
    client = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-east-1"))
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

def _lowercase_columns(df):
    """Normalise all column names to lowercase.

    The Glue Data Catalog stores column names in lowercase regardless of the
    original CSV headers.  Any mixed-case reference after toDF() raises
    AnalysisException: Column 'XYZ' does not exist.  Renaming once here means
    the rest of the job can use consistent lowercase names.
    """
    return df.toDF(*[c.lower() for c in df.columns])


employees_dyf = glueContext.create_dynamic_frame.from_catalog(
    database="hr_analytics",
    table_name="raw_employees",
    transformation_ctx="employees_source",
)
employees = _lowercase_columns(employees_dyf.toDF())

departments_dyf = glueContext.create_dynamic_frame.from_catalog(
    database="hr_analytics",
    table_name="raw_departments",
    transformation_ctx="departments_source",
)
departments = _lowercase_columns(departments_dyf.toDF())

managers_dyf = glueContext.create_dynamic_frame.from_catalog(
    database="hr_analytics",
    table_name="raw_managers",
    transformation_ctx="managers_source",
)
managers = _lowercase_columns(managers_dyf.toDF())

# ── Step 2: Silver layer — null guard + explicit type casts ──────────────────

def clean_data(df, id_col: str):
    """Drop rows with null primary key; cast hiredate to DateType if present.

    Args:
        df: Source DataFrame with lowercased column names.
        id_col: Lowercase name of the primary-key column to null-check.

    Raises:
        ValueError: if id_col is not present, so the job fails with a clear
            message rather than silently skipping the filter.
    """
    if id_col not in df.columns:
        raise ValueError(
            f"clean_data: column '{id_col}' not found. "
            f"Available columns: {df.columns}"
        )
    df = df.filter(F.col(id_col).isNotNull())
    if "hiredate" in df.columns:
        df = df.withColumn("hiredate", F.col("hiredate").cast(DateType()))
    return df


employees = clean_data(employees, "employeeid")

# Type normalisation: join keys must be string; salary/ranges to double for maths
employees = (
    employees
    .withColumn("deptid",    F.col("deptid").cast("string"))
    .withColumn("managerid", F.col("managerid").cast("string"))
    .withColumn("salary",    F.col("salary").cast("double"))
)

departments = (
    departments
    .withColumn("deptid",         F.col("deptid").cast("string"))
    .withColumn("maxsalaryrange", F.col("maxsalaryrange").cast("double"))
    .withColumn("minsalaryrange", F.col("minsalaryrange").cast("double"))
)

managers = managers.withColumn("managerid", F.col("managerid").cast("string"))

# ── Step 3: Broadcast joins — eliminates shuffle on small lookup tables ───────
# departments: 6 rows, managers: 100 rows — both fit in executor memory
enriched = employees.join(
    F.broadcast(departments.select(
        "deptid", "departmentname", "maxsalaryrange", "minsalaryrange", "budget"
    )),
    on="deptid",
    how="left",
)

enriched = enriched.join(
    F.broadcast(managers.select("managerid", "managername", "isactive", "level")),
    on="managerid",
    how="left",
)

# ── Step 4: Window function — HighestTitleSalary ──────────────────────────────
title_window = Window.partitionBy("jobtitle")
enriched = enriched.withColumn(
    "highesttitlesalary",
    F.max(F.col("salary")).over(title_window),
)

# ── Step 5: Business logic ────────────────────────────────────────────────────
enriched = enriched.withColumn(
    "comparatio",
    F.round(F.col("salary") / F.col("maxsalaryrange"), 2),
)

# isactive is stored as the string "True" / "False" — never cast to boolean
enriched = enriched.withColumn(
    "requiresreview",
    (F.col("comparatio") > F.lit(1.0)) | (F.col("isactive") == F.lit("False")),
)

# ── Step 6: Data Quality gate — fail-fast before writing to any sink ──────────
dq_ruleset = """
    Rules = [
        IsComplete "employeeid",
        ColumnValues "salary" > 0,
        IsUnique "employeeid"
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
    raise RuntimeError("Data quality checks failed - aborting job before any writes.")

# ── Step 6b: Row-level circuit breaker ────────────────────────────────────────
# Even when aggregate rules pass, individual rows may still carry a bad
# RowOutcome (null EmployeeID or non-positive Salary). Quarantine those rows:
# log them to CloudWatch (visible in /aws-glue/jobs/output) and exclude them
# from the DynamoDB write so corrupt records never reach production.
_bad_rows = enriched.filter(
    F.col("employeeid").isNull()
    | F.col("salary").isNull()
    | (F.col("salary") <= 0)
)
_bad_count = _bad_rows.count()
if _bad_count > 0:
    logger.error(
        f"[DQ-CircuitBreaker] RowOutcome=Error on {_bad_count} record(s). "
        "These rows are quarantined and will NOT be written to DynamoDB."
    )
    _bad_rows.select("employeeid", "salary", "jobtitle").show(truncate=False)

# Only rows with a clean RowOutcome proceed to both sinks.
enriched = enriched.filter(
    F.col("employeeid").isNotNull()
    & F.col("salary").isNotNull()
    & (F.col("salary") > 0)
)

# ── Step 7: DynamoDB sink via native Glue DynamicFrame connector ──────────────
# DateType not serialisable by the DDB connector — cast HireDate back to string.
# PK/SK added as DynamoDB composite key attributes.
dynamo_df = (
    enriched
    .withColumn("hiredate", F.col("hiredate").cast("string"))
    .withColumn("PK", F.concat(F.lit("EMP#"), F.col("employeeid").cast("string")))
    .withColumn("SK", F.lit("PROFILE"))
)

# ── Step 7a: TitleCase rename — restore data contract for Lambda API ──────────
# Internal processing uses lowercase column names (Glue Catalog normalisation).
# DynamoDB attributes must be TitleCase so the Lambda handler can read them with
# item.get("EmployeeID"), item.get("Salary"), etc.
# "level" from the managers table maps to "ManagerLevel" to match the API contract.
_DYNAMO_RENAME = {
    "employeeid":         "EmployeeID",
    "firstname":          "FirstName",
    "lastname":           "LastName",
    "email":              "Email",
    "deptid":             "DeptID",
    "department":         "Department",
    "jobtitle":           "JobTitle",
    "salary":             "Salary",
    "hiredate":           "HireDate",
    "city":               "City",
    "state":              "State",
    "employmentstatus":   "EmploymentStatus",
    "managerid":          "ManagerID",
    "manager":            "Manager",
    "departmentname":     "DepartmentName",
    "maxsalaryrange":     "MaxSalaryRange",
    "minsalaryrange":     "MinSalaryRange",
    "budget":             "Budget",
    "managername":        "ManagerName",
    "isactive":           "IsActive",
    "level":              "ManagerLevel",
    "highesttitlesalary": "HighestTitleSalary",
    "comparatio":         "CompaRatio",
    "requiresreview":     "RequiresReview",
}
for old, new in _DYNAMO_RENAME.items():
    if old in dynamo_df.columns:
        dynamo_df = dynamo_df.withColumnRenamed(old, new)

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

# ── Step 8: Parquet sink — catalog-aware write with automatic partition reg ───
# getSink replaces the raw df.write.parquet() call.  With enableUpdateCatalog=True
# and updateBehavior="UPDATE_IN_DATABASE", Glue calls glue:BatchCreatePartition
# after each write so partitions appear in the Data Catalog immediately — no
# MSCK REPAIR TABLE required before running Athena queries.
parquet_df = (
    enriched
    .withColumn("year",  F.year(F.col("hiredate")))
    .withColumn("month", F.month(F.col("hiredate")))
    .withColumn("dept",  F.col("deptid"))
)

parquet_sink = glueContext.getSink(
    path=f"{PARQUET_BASE}/employees/",
    connection_type="s3",
    updateBehavior="UPDATE_IN_DATABASE",
    partitionKeys=["year", "month", "dept"],
    enableUpdateCatalog=True,
    transformation_ctx="parquet_sink",
)
parquet_sink.setCatalogInfo(
    catalogDatabase="hr_analytics",
    catalogTableName="employees",
)
parquet_sink.setFormat("glueparquet", useGlueParquetWriter=True)
parquet_sink.writeFrame(DynamicFrame.fromDF(parquet_df, glueContext, "parquet_dyf"))

# REQUIRED: without this Glue marks the job run as failed
job.commit()
