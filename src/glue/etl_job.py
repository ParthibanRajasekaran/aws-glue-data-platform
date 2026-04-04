"""
HR ETL PySpark Job — Phase 4
- Config fetched from SSM Parameter Store at runtime (Zero-Trust, no hardcoded names)
- Reads raw CSVs via from_catalog() for native Glue Data Lineage + Job Bookmark support
- Column names normalised to lowercase immediately after catalog read (Glue Catalog
  lowercases all names; mixed-case references cause AnalysisException at runtime)
- Broadcast joins eliminate shuffle on small lookup tables
- Silver layer: null filtering, hiredate cast
- DQ gate (aggregate): Completeness > 0.99, ColumnDataType, CustomSql negative-salary
  check, IsUnique on employeeid — fail-fast before any write if systemic failure
- DQ gate (row-level): quarantines individual bad rows to S3 so the rest of the batch
  can proceed; poisoned rows never reach DynamoDB or the Parquet analytical sink
- PII masking: LastName SHA-256 hashed in the Parquet analytical sink; plain-text in
  the DynamoDB operational sink for the HR API
- Null safety: na.fill() guards on all string/double columns before DynamoDB PutItem
- Hive-partitioned Parquet output (year / month / dept) for cost-efficient Athena queries
"""

import os
import sys

import boto3
from awsglue.context import GlueContext
from awsglue.dynamicframe import DynamicFrame
from awsglue.job import Job
from awsglue.utils import getResolvedOptions
from awsgluedq.transforms import EvaluateDataQuality
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
            "/hr-pipeline/quarantine-bucket-name",
        ],
        WithDecryption=True,
    )
    return {p["Name"].rsplit("/", 1)[-1]: p["Value"] for p in response["Parameters"]}


_ssm = _fetch_ssm_config()
PARQUET_BASE = f"s3://{_ssm['parquet-bucket-name']}"
DYNAMO_TABLE = _ssm["dynamodb-table-name"]
QUARANTINE_BASE = f"s3://{_ssm['quarantine-bucket-name']}"

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
            f"clean_data: column '{id_col}' not found. " f"Available columns: {df.columns}"
        )
    df = df.filter(F.col(id_col).isNotNull())
    if "hiredate" in df.columns:
        df = df.withColumn("hiredate", F.col("hiredate").cast(DateType()))
    return df


employees = clean_data(employees, "employeeid")

# Type normalisation: join keys must be string; salary/ranges to double for maths
employees = (
    employees.withColumn("deptid", F.col("deptid").cast("string"))
    .withColumn("managerid", F.col("managerid").cast("string"))
    .withColumn("salary", F.col("salary").cast("double"))
)

departments = (
    departments.withColumn("deptid", F.col("deptid").cast("string"))
    .withColumn("maxsalaryrange", F.col("maxsalaryrange").cast("double"))
    .withColumn("minsalaryrange", F.col("minsalaryrange").cast("double"))
)

managers = managers.withColumn("managerid", F.col("managerid").cast("string"))

# ── Step 3: Broadcast joins — eliminates shuffle on small lookup tables ───────
# departments: 6 rows, managers: 100 rows — both fit in executor memory
enriched = employees.join(
    F.broadcast(
        departments.select("deptid", "departmentname", "maxsalaryrange", "minsalaryrange", "budget")
    ),
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

# isactive is stored as the string "True" / "False" — never cast to boolean.
# isactive null means the LEFT JOIN found no matching manager row; unknown
# supervision is treated as a risk requiring HR review.
enriched = enriched.withColumn(
    "requiresreview",
    (F.col("comparatio") > F.lit(1.0))
    | (F.col("isactive") == F.lit("False"))
    | F.col("isactive").isNull(),
)

# ── Step 6: Data Quality gate — fail-fast before writing to any sink ──────────
# Rule rationale:
#   Completeness > 0.99  — up to 1 % null IDs tolerated (not zero, to survive
#     minor upstream gaps); more than that is a systemic load failure → abort.
#   ColumnDataType        — enforces the schema contract: salary must be Double,
#     not a string residual from a bad CSV column swap.
#   CustomSql             — zero negative-pay values allowed; catches sign errors
#     that a completeness check cannot (non-null but wrong sign).
#   IsUnique              — duplicate employee IDs corrupt DynamoDB put-item
#     semantics (last-write wins, silently overwrites).
dq_ruleset = """
    Rules = [
        Completeness "employeeid" > 0.99,
        ColumnDataType "salary" = "Double",
        CustomSql "SELECT COUNT(*) FROM primary WHERE salary < 0" = 0,
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
    dq_result.select_fields(["Rule", "Outcome"]).toDF().filter(F.col("Outcome") == F.lit("Failed"))
)
if failed_rules.count() > 0:
    failed_rules.show(truncate=False)
    raise RuntimeError("Data quality checks failed - aborting job before any writes.")

# ── Step 6b: Row-level circuit breaker — quarantine to S3 ────────────────────
# Even when aggregate DQ rules pass, individual rows may still carry bad values
# (null EmployeeID, null Salary, or negative Salary). These are isolated here:
#
#   1. Written as JSON to the dedicated quarantine bucket so the security team
#      can inspect poisoned records without touching production sinks.
#   2. Logged to CloudWatch (/aws-glue/jobs/output) for alerting.
#   3. Filtered out — only clean rows reach DynamoDB and Parquet.
#
# Quarantine path: s3://<quarantine-bucket>/quarantine/employees/run=<job-name>/
# The run partition uses the Glue job name so each execution is traceable.
_bad_rows = enriched.filter(
    F.col("employeeid").isNull()
    | F.col("salary").isNull()
    | (F.col("salary") <= 0)
    | F.col("comparatio").isNull()  # unmatched dept — cannot assess pay-band compliance
)
_bad_count = _bad_rows.count()
if _bad_count > 0:
    logger.error(
        f"[DQ-CircuitBreaker] RowOutcome=Quarantined on {_bad_count} record(s). "
        f"Writing poisoned rows to {QUARANTINE_BASE}/quarantine/employees/ "
        "— these rows will NOT reach DynamoDB or Parquet."
    )
    # Cast hiredate back to string so JSON serialisation does not fail on DateType.
    _quarantine_df = _bad_rows.withColumn("hiredate", F.col("hiredate").cast("string")).withColumn(
        "_quarantine_reason",
        F.when(F.col("employeeid").isNull(), F.lit("null_employeeid"))
        .when(F.col("salary").isNull(), F.lit("null_salary"))
        .when(F.col("comparatio").isNull(), F.lit("null_comparatio_unmatched_dept"))
        .otherwise(F.lit("negative_salary")),
    )
    glueContext.write_dynamic_frame.from_options(
        frame=DynamicFrame.fromDF(_quarantine_df, glueContext, "quarantine_dyf"),
        connection_type="s3",
        connection_options={
            "path": f"{QUARANTINE_BASE}/quarantine/employees/run={args['JOB_NAME']}/",
        },
        format="json",
    )
    # Publish a metric so the QuarantinedRowsAlarm fires immediately — the post-ETL
    # reconciliation script catches count mismatches, but this gives sub-minute
    # alerting for any individual run that quarantines rows.
    boto3.client(
        "cloudwatch", region_name=os.environ.get("AWS_REGION", "us-east-1")
    ).put_metric_data(
        Namespace="HRPipeline",
        MetricData=[
            {
                "MetricName": "QuarantinedRowCount",
                "Value": float(_bad_count),
                "Unit": "Count",
            }
        ],
    )

# Only rows with a clean RowOutcome proceed to both sinks.
enriched = enriched.filter(
    F.col("employeeid").isNotNull()
    & F.col("salary").isNotNull()
    & (F.col("salary") > 0)
    & F.col("comparatio").isNotNull()
)

# ── Step 7: DynamoDB sink via native Glue DynamicFrame connector ──────────────
# DateType not serialisable by the DDB connector — cast HireDate back to string.
# PK/SK added as DynamoDB composite key attributes.
dynamo_df = (
    enriched.withColumn("hiredate", F.col("hiredate").cast("string"))
    .withColumn("PK", F.concat(F.lit("EMP#"), F.col("employeeid").cast("string")))
    .withColumn("SK", F.lit("PROFILE"))
)

# ── Step 7a: TitleCase rename — restore data contract for Lambda API ──────────
# Internal processing uses lowercase column names (Glue Catalog normalisation).
# DynamoDB attributes must be TitleCase so the Lambda handler can read them with
# item.get("EmployeeID"), item.get("Salary"), etc.
# "level" from the managers table maps to "ManagerLevel" to match the API contract.
_DYNAMO_RENAME = {
    "employeeid": "EmployeeID",
    "firstname": "FirstName",
    "lastname": "LastName",
    "email": "Email",
    "deptid": "DeptID",
    "department": "Department",
    "jobtitle": "JobTitle",
    "salary": "Salary",
    "hiredate": "HireDate",
    "city": "City",
    "state": "State",
    "employmentstatus": "EmploymentStatus",
    "managerid": "ManagerID",
    "manager": "Manager",
    "departmentname": "DepartmentName",
    "maxsalaryrange": "MaxSalaryRange",
    "minsalaryrange": "MinSalaryRange",
    "budget": "Budget",
    "managername": "ManagerName",
    "isactive": "IsActive",
    "level": "ManagerLevel",
    "highesttitlesalary": "HighestTitleSalary",
    "comparatio": "CompaRatio",
    "requiresreview": "RequiresReview",
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
#
# PII masking: LastName is SHA-256 hashed for the analytical sink.
#   - Analytical consumers (Athena, BI tools) need aggregate patterns, not
#     individual identities — hashing satisfies GDPR pseudonymisation.
#   - The DynamoDB operational sink retains plain-text LastName for the HR API.
#   - Salary is NOT masked: it is the primary analytical metric and is already
#     controlled by IAM (only the Glue role and Lambda role can read the data).
parquet_df = (
    enriched.withColumn("year", F.year(F.col("hiredate")))
    .withColumn("month", F.month(F.col("hiredate")))
    .withColumn("dept", F.col("deptid"))
    .withColumn("lastname", F.sha2(F.col("lastname").cast("string"), 256))
    # Email is personal data under UK GDPR (ICO guidance) — pseudonymise in the
    # analytical sink alongside LastName.  The DynamoDB operational sink retains
    # plain-text email so the HR API can surface contact details to authorised callers.
    .withColumn("email", F.sha2(F.col("email").cast("string"), 256))
)

# ── Step 8a: Atomic Parquet write — staging + swap ───────────────────────────
# The previous "purge-then-getSink" approach had a compliance risk window:
# if getSink failed midway, the production partition was left empty (data loss).
# A new file could not be distinguished from a missing file by Athena.
#
# Two-phase atomic pattern:
#   Phase 1 — Stage  : Spark writes to employees_staging/ (prod untouched).
#                      If this fails, raise RuntimeError — nothing is lost.
#   Phase 2 — Verify : Assert staging is non-empty before touching prod at all.
#   Phase 3 — Swap   : Purge only the affected prod partitions; copy staging
#                      files server-side (no data egress cost); purge staging.
#   Phase 4 — Catalog: Register/update partition metadata in Glue Data Catalog
#                      so Athena sees fresh partitions instantly without
#                      MSCK REPAIR TABLE.
#
# Remaining 1% risk: if Phase 3 copy fails mid-partition, that partition is
# temporarily empty.  The reconciliation alarm catches this on the next run,
# and the raw S3 source is always available for a full bookmark-reset replay.
#
# Why not df.write.mode("overwrite")?  It bypasses getSink and severs Glue
# Catalog integration — partitions become invisible to Athena without a manual
# MSCK REPAIR TABLE.  This function achieves overwrite semantics while keeping
# full catalog continuity via explicit BatchCreatePartition / UpdatePartition.


def _atomic_parquet_write(
    parquet_df,
    bucket_name: str,
    prod_prefix: str,
    staging_prefix: str,
    glue_database: str,
    glue_table: str,
    region: str,
) -> None:
    """Write Parquet files to S3 atomically: stage → verify → swap → catalog.

    Args:
        parquet_df    : DataFrame with year, month, dept partition columns.
        bucket_name   : Parquet S3 bucket name (no s3:// prefix).
        prod_prefix   : Production key prefix, no trailing slash ("employees").
        staging_prefix: Staging key prefix, no trailing slash ("employees_staging").
        glue_database : Glue Data Catalog database name.
        glue_table    : Glue Data Catalog table name.
        region        : AWS region for boto3 clients.

    IAM required:
        s3:PutObject, s3:GetObject, s3:DeleteObject on employees* objects
        s3:ListBucket with employees/* and employees_staging/* prefix conditions
        glue:BatchCreatePartition, glue:UpdatePartition on the target table
    """
    s3 = boto3.client("s3", region_name=region)
    glue_client = boto3.client("glue", region_name=region)
    paginator = s3.get_paginator("list_objects_v2")

    staging_path = f"s3://{bucket_name}/{staging_prefix}/"

    # ── Phase 1: Write to staging (production is untouched if this fails) ────
    logger.info(f"[AtomicWrite] Phase 1 — Staging write to {staging_path}")
    (
        parquet_df.write.mode("overwrite")
        .option("compression", "snappy")
        .partitionBy("year", "month", "dept")
        .parquet(staging_path)
    )

    # ── Phase 2: Verify staging is non-empty before touching production ───────
    logger.info("[AtomicWrite] Phase 2 — Verifying staging output")
    staging_parquet_keys = []
    for page in paginator.paginate(Bucket=bucket_name, Prefix=f"{staging_prefix}/"):
        staging_parquet_keys.extend(
            obj["Key"] for obj in page.get("Contents", []) if obj["Key"].endswith(".parquet")
        )
    if not staging_parquet_keys:
        raise RuntimeError(
            "[AtomicWrite] Phase 2 FAILED: staging produced no Parquet files. "
            "Production is untouched — reset the job bookmark and replay."
        )
    logger.info(
        f"[AtomicWrite] Phase 2 — Verified: {len(staging_parquet_keys)} " "staging file(s) ready."
    )

    # ── Phase 3: Swap — purge prod partitions; server-side copy staging → prod ─
    logger.info("[AtomicWrite] Phase 3 — Swapping staging to production")
    affected_partitions = parquet_df.select("year", "month", "dept").distinct().collect()

    # 3a: Purge affected production partition prefixes.
    # Optimisation: one paginated list over the entire prod_prefix instead of
    # N separate list_objects_v2 calls (one per partition).  The affected set
    # lookup is O(1) per key — far faster than 542 individual ListBucket calls.
    affected_set = {
        (str(row["year"]), str(row["month"]), str(row["dept"])) for row in affected_partitions
    }

    def _partition_tuple_from_key(key):
        """Extract (year, month, dept) from 'employees/year=X/month=Y/dept=Z/file'."""
        parts = key.split("/")
        # Expected: [prod_prefix, "year=X", "month=Y", "dept=Z", "filename"]
        if len(parts) < 5:
            return None
        try:
            year = parts[1].split("=")[1]
            month = parts[2].split("=")[1]
            dept = parts[3].split("=")[1]
            return (year, month, dept)
        except IndexError:
            return None

    keys_to_delete = []
    for page in paginator.paginate(Bucket=bucket_name, Prefix=f"{prod_prefix}/"):
        for obj in page.get("Contents", []):
            if _partition_tuple_from_key(obj["Key"]) in affected_set:
                keys_to_delete.append({"Key": obj["Key"]})

    purge_count = 0
    for i in range(0, len(keys_to_delete), 1000):
        s3.delete_objects(
            Bucket=bucket_name,
            Delete={"Objects": keys_to_delete[i : i + 1000]},
        )
        purge_count += min(1000, len(keys_to_delete) - i)

    # 3b: Server-side copy staging Parquet files to production (no egress cost)
    for staging_key in staging_parquet_keys:
        prod_key = prod_prefix + staging_key[len(staging_prefix) :]
        s3.copy_object(
            Bucket=bucket_name,
            CopySource={"Bucket": bucket_name, "Key": staging_key},
            Key=prod_key,
        )

    logger.info(
        f"[AtomicWrite] Phase 3 — Purged {purge_count} stale file(s); "
        f"copied {len(staging_parquet_keys)} fresh file(s) to production."
    )

    # 3c: Purge all staging objects (including Spark's _SUCCESS marker)
    all_staging_keys = []
    for page in paginator.paginate(Bucket=bucket_name, Prefix=f"{staging_prefix}/"):
        all_staging_keys.extend({"Key": obj["Key"]} for obj in page.get("Contents", []))
    for i in range(0, len(all_staging_keys), 1000):  # delete_objects max 1000
        s3.delete_objects(
            Bucket=bucket_name,
            Delete={"Objects": all_staging_keys[i : i + 1000]},
        )

    # ── Phase 4: Register / update partitions in Glue Data Catalog ───────────
    # BatchCreatePartition returns per-partition errors for existing ones rather
    # than raising; check the Errors list and call UpdatePartition for each.
    logger.info("[AtomicWrite] Phase 4 — Updating Glue Data Catalog")
    partition_inputs = [
        {
            "Values": [str(row["year"]), str(row["month"]), str(row["dept"])],
            "StorageDescriptor": {
                "Location": (
                    f"s3://{bucket_name}/{prod_prefix}"
                    f"/year={row['year']}/month={row['month']}/dept={row['dept']}/"
                ),
                "InputFormat": ("org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat"),
                "OutputFormat": ("org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat"),
                "SerdeInfo": {
                    "SerializationLibrary": (
                        "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
                    ),
                    "Parameters": {"serialization.format": "1"},
                },
                "Compressed": True,
            },
        }
        for row in affected_partitions
    ]

    for i in range(0, len(partition_inputs), 100):  # BatchCreatePartition max 100
        batch = partition_inputs[i : i + 100]
        response = glue_client.batch_create_partition(
            DatabaseName=glue_database,
            TableName=glue_table,
            PartitionInputList=batch,
        )
        for err in response.get("Errors", []):
            if err.get("ErrorDetail", {}).get("ErrorCode") == "AlreadyExistsException":
                values = err["PartitionValues"]
                part_input = next(p for p in batch if p["Values"] == values)
                glue_client.update_partition(
                    DatabaseName=glue_database,
                    TableName=glue_table,
                    PartitionValueList=values,
                    PartitionInput=part_input,
                )

    logger.info(
        f"[AtomicWrite] Complete — {len(affected_partitions)} partition(s) in "
        f"s3://{bucket_name}/{prod_prefix}/ refreshed and registered in Glue Catalog."
    )


_atomic_parquet_write(
    parquet_df=parquet_df,
    bucket_name=_ssm["parquet-bucket-name"],
    prod_prefix="employees",
    staging_prefix="employees_staging",
    glue_database="hr_analytics",
    glue_table="employees",
    region=os.environ.get("AWS_REGION", "us-east-1"),
)

# REQUIRED: without this Glue marks the job run as failed
job.commit()
