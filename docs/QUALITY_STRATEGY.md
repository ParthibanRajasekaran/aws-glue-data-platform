# Quality Strategy

> Perspective: Zero-Trust data integrity. Every row entering the pipeline is assumed poisoned until proven clean. Every sink write is auditable. Every failure is loud.

---

## 1. Data Lineage

How a row travels from `employees.csv` to `EMP#1001`:

```
employees.csv (S3 raw layer)
  │  s3://<RAW-BUCKET>/raw/employees/
  │
  ▼
Glue Data Catalog  ──────────────────────────────────  schema authority
  │  database: hr_analytics / table: raw_employees
  │  column types enforced at read time (int, string, etc.)
  │
  ▼
etl_job.py — Step 1: from_catalog()
  │  Job Bookmark records the S3 object version processed.
  │  Each run is traceable via Glue job run ID in CloudWatch.
  │
  ▼
Step 2: Silver layer (clean_data)
  │  Rows with null EmployeeID dropped immediately.
  │  HireDate cast to DateType — invalid dates become null (visible in DQ).
  │
  ▼
Step 3–5: Joins + Window + Business logic
  │  Left joins on DeptID, ManagerID — unmatched rows get null attributes,
  │  not dropped. na.fill() before DynamoDB write prevents null attribute errors.
  │
  ▼
Step 6: Aggregate DQ gate (EvaluateDataQuality)  ◄── CIRCUIT BREAKER 1
  │  Completeness "employeeid" > 0.99
  │  ColumnDataType "salary" = "Double"
  │  CustomSql COUNT(*) WHERE salary < 0 = 0
  │  IsUnique "employeeid"
  │
  │  FAIL → RuntimeError, job aborts, zero writes to any sink.
  │  PASS → proceed
  │
  ▼
Step 6b: Row-level circuit breaker  ◄────────────────── CIRCUIT BREAKER 2
  │  Rows: null EmployeeID | null Salary | Salary ≤ 0
  │
  ├──► S3 Quarantine  s3://<QUARANTINE-BUCKET>/quarantine/employees/run=<JOB_NAME>/
  │      Format: JSON (human-readable for forensics)
  │      Encryption: same CMK as production buckets
  │      Glue role: PutObject only (cannot read back or delete)
  │
  └──► Filtered out — clean rows continue
  │
  ▼
Step 7: DynamoDB sink (operational)
  │  PK: EMP#<EmployeeID>  SK: PROFILE
  │  LastName: plain-text (HR API needs real names)
  │  Encrypted at rest: CUSTOMER_MANAGED KMS CMK
  │
  ▼
Step 8: Parquet sink (analytical)
     s3://<PARQUET-BUCKET>/employees/year=.../month=.../dept=.../
     LastName: SHA-256 hash (pseudonymisation — analytics needs patterns, not identities)
     Salary: visible (primary analytical metric; access controlled by IAM)
     Encrypted at rest: same CMK
     Partitions registered in Glue Catalog automatically (no MSCK REPAIR needed)
```

---

## 2. Circuit Breakers

Two independent breakers prevent bad data from reaching sinks.

### 2.1 Aggregate DQ Gate (Fail-Fast)

**Trigger**: Systemic data quality failure — the source file itself is poisoned.

| Rule | What it catches | Action |
|---|---|---|
| `Completeness "employeeid" > 0.99` | More than 1 % of employee IDs are null — upstream truncation or join failure | Abort entire job |
| `ColumnDataType "salary" = "Double"` | Schema drift — salary column changed type (e.g. a string column appeared in the CSV) | Abort entire job |
| `CustomSql COUNT(*) WHERE salary < 0 = 0` | Any negative pay value — sign error in source system | Abort entire job |
| `IsUnique "employeeid"` | Duplicate IDs — would silently overwrite DynamoDB items | Abort entire job |

**Behaviour on failure**: `raise RuntimeError` before `job.commit()`. Glue marks the run as FAILED. CloudWatch metric `numFailedTasks > 0` triggers the `hr-pipeline-glue-job-failed` alarm → SNS → on-call notification within 1 minute.

**Why threshold-based completeness**: `IsComplete` (100 % requirement) would abort on every run with a single null ID. `Completeness > 0.99` tolerates minor upstream gaps (up to 20 rows per 2000) while still catching systemic failures. The threshold is intentionally conservative.

### 2.2 Row-Level Circuit Breaker (Quarantine)

**Trigger**: Individual rows that pass aggregate rules but carry specific bad values.

**Why both breakers**: The aggregate `CustomSql` rule catches negative salaries at the job level but runs on the full dataset. The row-level breaker isolates the specific records and routes them to a forensic store. Without this, a passing aggregate gate would still silently write null-salary rows to DynamoDB.

**Quarantine path**: `s3://<QUARANTINE-BUCKET>/quarantine/employees/run=<JOB_NAME>/`

Each quarantined row includes a `_quarantine_reason` field (`null_employeeid`, `null_salary`, `negative_salary`) so the security team can triage without re-running the job.

### 2.3 Reconciliation Check (Post-Run)

**Trigger**: Source count ≠ sink count beyond the expected quarantine gap.

Run `src/reconciliation/reconcile.py` after each pipeline execution:

```bash
python src/reconciliation/reconcile.py --threshold 0.05
```

The script:
1. Counts rows in `s3://<RAW-BUCKET>/raw/employees/*.csv` (all files, minus headers)
2. Counts `SK=PROFILE` items in DynamoDB (paginated scan with `Select=COUNT`)
3. If gap > 5 %: publishes `HRPipeline/ReconciliationMismatch = 1` to CloudWatch → alarm → SNS
4. If gap ≤ 5 %: publishes metric = 0 (alarm stays green)

The 5 % default threshold accounts for rows that the DQ circuit breaker legitimately quarantined. An unexpected gap above this signals a silent write failure.

---

## 3. Auditability

> How we prove to a regulator that "no leaks" occurred.

### 3.1 Trace IDs

Every Glue job run produces a unique **Job Run ID** (e.g. `jr_abc123`) visible in:
- AWS Glue console → Job Runs
- CloudWatch log streams `/aws-glue/jobs/output`, `/aws-glue/jobs/error`, `/aws-glue/jobs/logs-v2`
- The quarantine S3 prefix `run=<JOB_NAME>/` (correlates bad rows to the run that found them)

The DQ result context `"dataQualityEvaluationContext": "hr_etl_dq"` is published to AWS Glue Data Quality Results — queryable via the console or API.

### 3.2 Encryption Evidence

All storage-at-rest encryption uses a single Customer-Managed KMS Key:

| Sink | Encryption |
|---|---|
| S3 Raw bucket | `BucketEncryption.KMS` + CMK |
| S3 Parquet bucket | `BucketEncryption.KMS` + CMK |
| S3 Assets bucket | `BucketEncryption.KMS` + CMK |
| S3 Quarantine bucket | `BucketEncryption.KMS` + CMK |
| DynamoDB table | `TableEncryption.CUSTOMER_MANAGED` + CMK |

Key rotation is enabled (`enable_key_rotation=True`). AWS KMS CloudTrail logs every `Decrypt` and `GenerateDataKey` call with the caller's IAM identity — this is the "no-leak" proof trail.

### 3.3 IAM Scope (Least-Privilege)

The Glue role (`GlueDataAccessPolicy`) has eight scoped statements — no wildcard resources:

| Statement | Scope |
|---|---|
| `S3RawRead` | `GetObject`, `ListBucket` on raw prefix only |
| `S3ParquetWrite` | `PutObject`, `DeleteObject` on `employees*` prefix only |
| `S3AssetsTempDir` | Full CRUD on assets bucket (shuffle spill + bookmarks) |
| `S3QuarantineWrite` | `PutObject` on `quarantine/*` only — **cannot read or delete** |
| `DynamoDBScopedWrite` | `DescribeTable`, `PutItem`, `BatchWriteItem` on one table |
| `KmsEncryptDecrypt` | `GenerateDataKey*`, `Decrypt` on one key |
| `SsmReadConfig` | `GetParameter` on `/hr-pipeline/*` only |
| `GlueCatalogPartitions` | `GetTable`, `BatchCreatePartition`, `UpdateTable` on two resources |

The Lambda role gets `grant_read_data()` on DynamoDB plus `kms:Decrypt` on the CMK. Without the explicit `kms:Decrypt` grant, every `GetItem` call against a CUSTOMER_MANAGED-encrypted table returns `KMSAccessDeniedException`.

### 3.4 CloudWatch Alarms

| Alarm | Metric | Threshold | Action |
|---|---|---|---|
| `hr-pipeline-glue-job-failed` | `Glue/numFailedTasks` | > 0 | SNS → on-call |
| `hr-pipeline-reconciliation-mismatch` | `HRPipeline/ReconciliationMismatch` | > 0 | SNS → on-call |

Both alarms route to `hr-pipeline-alerts` SNS topic. Subscribe an email address or PagerDuty endpoint to receive notifications.

---

## 4. Recovery Point Objective (RPO)

**Target RPO**: Zero data loss for records that passed DQ validation. Quarantined rows are preserved indefinitely in S3.

### 4.1 Job Bookmark (Incremental Replay)

The ETL job runs with `--job-bookmark-option=job-bookmark-enable`. Glue records the S3 object version and byte offset of each successfully processed file. On re-run:

- Files already processed in a previous run are skipped.
- Only new or modified files are processed.

To **reset and replay from scratch** (e.g. after a DynamoDB table restore):

```bash
aws glue reset-job-bookmark --job-name <GLUE_JOB_NAME>
```

Then re-trigger the job. All source CSVs are re-processed from the beginning.

### 4.2 S3 as Source of Truth

Raw CSVs in `s3://<RAW-BUCKET>/raw/employees/` are the authoritative source. Because the ETL only reads from S3 (never modifies it), the pipeline is **idempotent**: re-running against the same S3 data produces the same DynamoDB and Parquet output. DynamoDB `PutItem` overwrites existing items with identical data.

### 4.3 Replay Steps

If a pipeline run fails after partial writes:

1. Identify the failed Glue Job Run ID in CloudWatch.
2. Check quarantine bucket for rows that were isolated — determine if re-processing is safe.
3. Reset the job bookmark if a full replay is needed: `aws glue reset-job-bookmark`.
4. Re-trigger the job via the Glue console or `aws glue start-job-run`.
5. After completion, run `python src/reconciliation/reconcile.py` to verify counts converge.

### 4.4 Quarantine Remediation

Quarantined rows are not permanently lost — they are stored in S3 for manual remediation:

1. Download from `s3://<QUARANTINE-BUCKET>/quarantine/employees/run=<JOB_NAME>/`
2. Fix the underlying data quality issue in the source CSV.
3. Re-upload the corrected records to `s3://<RAW-BUCKET>/raw/employees/`.
4. Reset the job bookmark and re-run the pipeline.
