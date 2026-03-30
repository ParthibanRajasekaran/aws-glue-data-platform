# Technical Specification & Data Contract
## HR Analytics ETL Pipeline - Phase 4

---

## 1. Architecture Overview

### Design Goals

| Goal | Implementation |
|---|---|
| **Zero-Trust Config** | No resource names or ARNs in source code. All runtime config fetched from SSM Parameter Store at job start. |
| **FinOps** | `G.1X` workers (minimum for batch ETL), `MaxRetries=0`, `Timeout=5 min`, Athena 100 MB scan cap per query. |
| **Governance** | Schema authority lives in CDK `CfnTable` definitions - not inferred at runtime. Static catalog eliminates the Glue Crawler cost. |
| **Data Lineage** | `from_catalog()` reads activate the native Glue Data Lineage graph, tracing data S3 → DynamoDB and S3 → Parquet end-to-end. |
| **Fail-Fast Quality** | `EvaluateDataQuality` gate runs before any write. A single failed rule aborts the job. |

### Pipeline Flow

```
S3 Raw CSVs  ──from_catalog()──►  Glue PySpark ETL Job (G.1X × 2)
                                         │
                              ┌──────────┴──────────┐
                              ▼                     ▼
                    DynamoDB Single-Table    Parquet (Hive-partitioned)
                    (PK: EMP#<id>)          s3://parquet-bucket/employees/
                    (SK: PROFILE)           partition: year / month / dept
                              │                     │
                              └──────────┬──────────┘
                                         ▼
                                Lambda API (Python 3.12)
                                GET employee by ID
```

### Key AWS Services

- **AWS Glue 4.0** - PySpark ETL job with Job Bookmarks (incremental processing)
- **AWS Glue Data Catalog** - Static schema definitions, no Crawler
- **Amazon DynamoDB** - Single-table design, KMS-encrypted
- **Amazon S3** - Raw CSV bucket + Parquet output bucket, KMS-encrypted
- **AWS Lambda** - Python 3.12 read API, invoked directly or via Function URL
- **Amazon Athena** - Ad-hoc SQL over Parquet via `hr_analytics_wg` workgroup
- **AWS KMS** - Customer-managed key with annual rotation; applied to all three S3 buckets and DynamoDB
- **AWS SSM Parameter Store** - Runtime config discovery (Zero-Trust)
- **Amazon CloudWatch** - Dashboard `hr-pipeline-observability`

---

## 2. Data Contract

### 2.1 Source Table: `raw_employees`

| Property | Value |
|---|---|
| Catalog database | `hr_analytics` |
| Table name | `raw_employees` |
| Table type | `EXTERNAL_TABLE` |
| Format | CSV (comma-delimited, header row skipped) |
| S3 location | `s3://<raw-bucket>/raw/employees/` |
| SerDe | `LazySimpleSerDe` |

**Schema**

| Column | Catalog Type | Notes |
|---|---|---|
| `EmployeeID` | `int` | Primary key - must be non-null and unique |
| `FirstName` | `string` | |
| `LastName` | `string` | |
| `Email` | `string` | |
| `DeptID` | `int` | Foreign key → `raw_departments.DeptID` |
| `Department` | `string` | Denormalised label (raw only) |
| `JobTitle` | `string` | |
| `Salary` | `int` | Raw integer; cast to `double` in Silver layer |
| `HireDate` | `string` | ISO-8601 (`YYYY-MM-DD`); cast to `date` in Silver layer |
| `City` | `string` | |
| `State` | `string` | |
| `EmploymentStatus` | `string` | e.g. `Full-Time`, `Contract` |
| `ManagerID` | `int` | Foreign key → `raw_managers.ManagerID` |
| `Manager` | `string` | Denormalised label (raw only) |

---

### 2.2 Source Table: `raw_managers`

| Property | Value |
|---|---|
| Catalog database | `hr_analytics` |
| Table name | `raw_managers` |
| Table type | `EXTERNAL_TABLE` |
| Format | CSV (comma-delimited, header row skipped) |
| S3 location | `s3://<raw-bucket>/raw/managers/` |
| SerDe | `LazySimpleSerDe` |

**Schema**

| Column | Catalog Type | Notes |
|---|---|---|
| `ManagerID` | `int` | Primary key; cast to `string` before broadcast join |
| `ManagerName` | `string` | |
| `Department` | `string` | |
| `ManagerEmail` | `string` | |
| `IsActive` | `string` | Stored as literal `"True"` / `"False"` - never cast to boolean |
| `Level` | `string` | e.g. `Director`, `VP` |

---

### 2.3 Source Table: `raw_departments`

| Property | Value |
|---|---|
| Catalog database | `hr_analytics` |
| Table name | `raw_departments` |
| Table type | `EXTERNAL_TABLE` |
| Format | CSV (comma-delimited, header row skipped) |
| S3 location | `s3://<raw-bucket>/raw/departments/` |
| SerDe | `LazySimpleSerDe` |

**Schema**

| Column | Catalog Type | Notes |
|---|---|---|
| `DeptID` | `int` | Primary key; cast to `string` before broadcast join |
| `DepartmentName` | `string` | Authoritative name used in enriched output |
| `Budget` | `int` | Raw; `bigint` in enriched output for 64-bit headroom |
| `MinSalaryRange` | `int` | Cast to `double` in Silver layer |
| `MaxSalaryRange` | `int` | Cast to `double`; denominator for `CompaRatio` calculation |
| `IsRemoteFriendly` | `string` | |

---

### 2.4 Output Table: `employees` (Parquet, Hive-partitioned)

| Property | Value |
|---|---|
| Catalog database | `hr_analytics` |
| Table name | `employees` |
| Table type | `EXTERNAL_TABLE` |
| Format | Parquet / SNAPPY |
| S3 location | `s3://<parquet-bucket>/employees/` |
| Partition keys | `year` (int), `month` (int), `dept` (string) |
| SerDe | `ParquetHiveSerDe` |

**Schema** (non-partition columns)

| Column | Type | Derived From |
|---|---|---|
| `EmployeeID` | `int` | `raw_employees` |
| `FirstName` | `string` | `raw_employees` |
| `LastName` | `string` | `raw_employees` |
| `Email` | `string` | `raw_employees` |
| `DeptID` | `string` | `raw_employees` (cast) |
| `Department` | `string` | `raw_employees` |
| `JobTitle` | `string` | `raw_employees` |
| `Salary` | `double` | `raw_employees` (cast) |
| `HireDate` | `date` | `raw_employees` (cast from string) |
| `City` | `string` | `raw_employees` |
| `State` | `string` | `raw_employees` |
| `EmploymentStatus` | `string` | `raw_employees` |
| `ManagerID` | `string` | `raw_employees` (cast) |
| `Manager` | `string` | `raw_employees` |
| `DepartmentName` | `string` | `raw_departments` (broadcast join) |
| `MaxSalaryRange` | `double` | `raw_departments` (broadcast join, cast) |
| `MinSalaryRange` | `double` | `raw_departments` (broadcast join, cast) |
| `Budget` | `bigint` | `raw_departments` (broadcast join) |
| `ManagerName` | `string` | `raw_managers` (broadcast join) |
| `IsActive` | `string` | `raw_managers` (broadcast join) |
| `Level` | `string` | `raw_managers` (broadcast join) |
| `HighestTitleSalary` | `double` | Window: `MAX(Salary) OVER (PARTITION BY JobTitle)` |
| `CompaRatio` | `double` | `ROUND(Salary / MaxSalaryRange, 2)` |
| `RequiresReview` | `boolean` | `(CompaRatio > 1.0) OR (IsActive == "False")` |

---

### 2.5 Reconciliation View: `v_etl_reconciliation`

A `VIRTUAL_VIEW` in the Glue Catalog, queryable from Athena. Compares raw source row count against enriched output row count as a post-run sanity check.

```sql
SELECT 'raw_employees'     AS data_source, COUNT(*) AS row_count
FROM hr_analytics.raw_employees
UNION ALL
SELECT 'employees_parquet' AS data_source, COUNT(*) AS row_count
FROM hr_analytics.employees
```

**Output schema:** `data_source` (varchar), `row_count` (bigint)

---

## 3. Silver Layer Transformations

Applied in `src/glue/etl_job.py` before any joins or quality checks.

| Transformation | Column(s) | Rule |
|---|---|---|
| Null key drop | `EmployeeID` | Rows with null `EmployeeID` are discarded |
| Date cast | `HireDate` | `string → DateType` (`YYYY-MM-DD`) |
| Join key normalisation | `DeptID`, `ManagerID` | Cast to `string` so join keys are type-consistent |
| Salary normalisation | `Salary` | Cast to `double` |
| Salary range normalisation | `MaxSalaryRange`, `MinSalaryRange` | Cast to `double` |
| Null fill (string) | All `StringType` columns | `na.fill("")` - prevents DynamoDB `SerializationException` |
| Null fill (double) | All `DoubleType` columns | `na.fill(0.0)` - prevents DynamoDB `SerializationException` |

---

## 4. Quality Gates

The `EvaluateDataQuality` step runs **after** Silver layer transformations and **before** any write to DynamoDB or Parquet. A single failed rule raises an exception and aborts the job run.

```
Rules = [
    IsComplete "EmployeeID",
    ColumnValues "Salary" > 0,
    IsUnique "EmployeeID"
]
```

| Rule | What it checks | Failure behaviour |
|---|---|---|
| `IsComplete "EmployeeID"` | Zero null values in `EmployeeID` column | Job aborts; no data written |
| `ColumnValues "Salary" > 0` | Every row has `Salary` strictly greater than zero | Job aborts; no data written |
| `IsUnique "EmployeeID"` | No duplicate `EmployeeID` values in the dataset | Job aborts; no data written |

Results are published to the Glue Data Quality console (`hr_etl_dq` context) with `enableDataQualityResultsPublishing: true`.

---

## 5. Configuration Discovery (SSM Parameter Store)

All runtime resource identifiers are fetched at Glue job startup via `boto3 ssm.get_parameters()`. No names or ARNs are hardcoded in `etl_job.py`.

| SSM Path | Controls | Consumer |
|---|---|---|
| `/hr-pipeline/kms-key-arn` | KMS key ARN used for S3 and DynamoDB encryption | Audit / key rotation tooling |
| `/hr-pipeline/raw-bucket-name` | S3 bucket name for raw CSV source data | Not read by current ETL (reads via Catalog) |
| `/hr-pipeline/parquet-bucket-name` | S3 bucket name for Parquet output | `PARQUET_BASE` in `etl_job.py` |
| `/hr-pipeline/dynamodb-table-name` | DynamoDB table name for the single-table design | `DYNAMO_TABLE` in `etl_job.py` |

**IAM scope:** The Glue job role has `ssm:GetParameter` and `ssm:GetParameters` restricted to `arn:aws:ssm:us-east-1:<account>:parameter/hr-pipeline/*` - no broader SSM access.

---

## 6. Observability

### CloudWatch Dashboard: `hr-pipeline-observability`

| Widget | Namespace | Metric | Dimension | Statistic | Period |
|---|---|---|---|---|---|
| Glue ETL - Succeeded Tasks | `Glue` | `glue.driver.aggregate.numSucceededTasks` | `JobName` | Sum | 5 min |
| Glue ETL - Failed Tasks | `Glue` | `glue.driver.aggregate.numFailedTasks` | `JobName` | Sum | 5 min |
| Athena - Bytes Scanned per Query | `AWS/Athena` | `DataScannedInBytes` | `WorkGroup: hr_analytics_wg` | Sum | 5 min |

### Glue Job Configuration

| Setting | Value | Rationale |
|---|---|---|
| `WorkerType` | `G.1X` | Minimum valid type for batch ETL; 4 vCPU, 16 GB memory per worker |
| `NumberOfWorkers` | `2` | 1 driver + 1 executor - sufficient for HR dataset scale |
| `MaxRetries` | `0` | Fail fast; no silent retry masking data quality issues |
| `Timeout` | `5 minutes` | Hard stop to prevent runaway DPU billing |
| `MaxConcurrentRuns` | `1` | Prevents interleaved DynamoDB writes from parallel runs |
| `GlueVersion` | `4.0` | Latest stable; required for `EvaluateDataQuality` native support |
| Job Bookmarks | Enabled | `--job-bookmark-option: job-bookmark-enable` - incremental processing |

### Athena Workgroup: `hr_analytics_wg`

| Setting | Value |
|---|---|
| `BytesScannedCutoffPerQuery` | `104857600` (100 MB) |
| `EnforceWorkGroupConfiguration` | `true` - client overrides not permitted |
| `PublishCloudWatchMetricsEnabled` | `true` |
| Result location | `s3://<assets-bucket>/athena-results/` |

### Log Groups

| Log Group | Managed by | Retention |
|---|---|---|
| `/aws-glue/jobs/logs-v2` | Glue (auto-created) | Default (never expires) |
| `/aws-glue/jobs/error` | Glue (auto-created) | Default (never expires) |
| `/aws-glue/jobs/output` | CDK | 1 day |

---

## 7. IAM Security Posture

No `Resource: "*"` exists in any custom IAM statement.

| # | Principal | Actions | Scoped To |
|---|---|---|---|
| 1 | Glue job role | `s3:GetObject`, `s3:ListBucket` | `raw_bucket/raw/*` prefix only |
| 2 | Glue job role | `s3:PutObject`, `s3:DeleteObject` | `parquet_bucket/employees/*` only |
| 3 | Glue job role | `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket` | `assets_bucket/*` (TempDir) |
| 4 | Glue job role | `dynamodb:PutItem`, `UpdateItem`, `DeleteItem`, `BatchWriteItem`, `DescribeTable` | `aws-glue-demo-single-table` only |
| 5 | Glue job role | `kms:GenerateDataKey*`, `kms:Decrypt` | Specific pipeline KMS key ARN only |
| 6 | Glue job role | `ssm:GetParameter`, `ssm:GetParameters` | `arn:aws:ssm:us-east-1:<account>:parameter/hr-pipeline/*` |
| 7 | Lambda role | `dynamodb:GetItem` | `aws-glue-demo-single-table` only |

---

## 8. Lambda API Contract

**Function:** `HrPipelineStack-HrApiHandler`
**Runtime:** Python 3.12
**Invocation:** Direct Lambda invocation (event payload)

### Request

```json
{ "employee_id": "1001" }
```

### Response - 200 OK

```json
{
  "EmployeeID": "1001",
  "Name": "Patricia Martinez",
  "Email": "patricia.martinez1000@example.com",
  "Department": "Sales",
  "JobTitle": "Sales AE",
  "Manager": "Jennifer Jones",
  "ManagerLevel": "Director",
  "HireDate": "2019-07-09 00:00:00",
  "City": "Chicago",
  "State": "IL",
  "EmploymentStatus": "Contract",
  "Salary": 121131.0,
  "CompaRatio": 0.79,
  "HighestTitleSalary": 139838.0,
  "RequiresReview": false
}
```

### Error Responses

| Status | Condition |
|---|---|
| `400` | `employee_id` missing or `null` in event payload |
| `404` | No DynamoDB item found for the given `employee_id` |

### DynamoDB Key Pattern

| Attribute | Value |
|---|---|
| `PK` | `EMP#<employee_id>` |
| `SK` | `PROFILE` |
