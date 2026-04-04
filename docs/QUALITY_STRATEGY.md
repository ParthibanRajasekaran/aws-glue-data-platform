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
etl_job.py - Step 1: from_catalog()
  │  Job Bookmark records the S3 object version processed.
  │  Each run is traceable via Glue job run ID in CloudWatch.
  │
  ▼
Step 2: Silver layer (clean_data)
  │  Rows with null EmployeeID dropped immediately.
  │  HireDate cast to DateType; invalid dates become null (visible in DQ).
  │
  ▼
Step 3–5: Joins + Window + Business logic
  │  Left joins on DeptID, ManagerID; unmatched rows get null attributes,
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
  └──► Filtered out; clean rows continue
  │
  ▼
Step 7: DynamoDB sink (operational)
  │  PK: EMP#<EmployeeID>  SK: PROFILE
  │  LastName: plain-text (HR API needs real names)
  │  Encrypted at rest: CUSTOMER_MANAGED KMS CMK
  │
  ▼
Step 8a: Partition-level purge (dynamic overwrite)  ◄── STALE-DATA GUARD
  │  Before writing, list and delete all existing files in the partitions
  │  this run will touch. Only affected partitions are cleared; historical
  │  partitions outside this batch are left intact.
  │  (Implements dynamic partition overwrite without leaving getSink.)
  │
  ▼
Step 8b: Parquet sink (analytical)
     s3://<PARQUET-BUCKET>/employees/year=.../month=.../dept=.../
     LastName: SHA-256 hash (pseudonymised; analytics needs patterns, not identities)
     Salary: visible (primary analytical metric; access controlled by IAM)
     Encrypted at rest: same CMK
     Partitions registered in Glue Catalog automatically (no MSCK REPAIR needed)
```

---

## 2. Circuit Breakers

Two independent breakers prevent bad data from reaching sinks.

### 2.1 Aggregate DQ Gate (Fail-Fast)

**Trigger**: Systemic data quality failure - the source file itself is poisoned.

| Rule | What it catches | Action |
|---|---|---|
| `Completeness "employeeid" > 0.99` | More than 1% of employee IDs are null (upstream truncation or join failure) | Abort entire job |
| `ColumnDataType "salary" = "Double"` | Schema drift - salary column changed type (e.g. a string column appeared in the CSV) | Abort entire job |
| `CustomSql COUNT(*) WHERE salary < 0 = 0` | Any negative pay value - sign error in source system | Abort entire job |
| `IsUnique "employeeid"` | Duplicate IDs (would silently overwrite DynamoDB items) | Abort entire job |

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

The DQ result context `"dataQualityEvaluationContext": "hr_etl_dq"` is published to AWS Glue Data Quality Results, queryable via the console or API.

### 3.2 Encryption Evidence

All storage-at-rest encryption uses a single Customer-Managed KMS Key:

| Sink | Encryption |
|---|---|
| S3 Raw bucket | `BucketEncryption.KMS` + CMK |
| S3 Parquet bucket | `BucketEncryption.KMS` + CMK |
| S3 Assets bucket | `BucketEncryption.KMS` + CMK |
| S3 Quarantine bucket | `BucketEncryption.KMS` + CMK |
| DynamoDB table | `TableEncryption.CUSTOMER_MANAGED` + CMK |

Key rotation is enabled (`enable_key_rotation=True`). AWS KMS CloudTrail logs every `Decrypt` and `GenerateDataKey` call with the caller's IAM identity. That's the no-leak proof trail.

### 3.3 IAM Scope (Least-Privilege)

The Glue role (`GlueDataAccessPolicy`) has nine scoped statements, no wildcard resources:

| Statement | Scope |
|---|---|
| `S3RawRead` | `GetObject`, `ListBucket` on raw prefix only |
| `S3ParquetListForAtomicWrite` | `ListBucket` on parquet bucket with `employees/*` and `employees_staging/*` conditions only |
| `S3ParquetWrite` | `PutObject`, `GetObject`, `DeleteObject` on `employees*` objects (covers staging prefix too) |
| `S3AssetsTempDir` | Full CRUD on assets bucket (shuffle spill + bookmarks) |
| `S3QuarantineWrite` | `PutObject` on `quarantine/*` only - **cannot read or delete** |
| `DynamoDBScopedWrite` | `DescribeTable`, `PutItem`, `BatchWriteItem` on one table |
| `KmsEncryptDecrypt` | `GenerateDataKey*`, `Decrypt` on one key |
| `SsmReadConfig` | `GetParameter` on `/hr-pipeline/*` only |
| `GlueCatalogPartitions` | `GetTable`, `BatchCreatePartition`, `UpdatePartition`, `UpdateTable` on two resources |

`S3ParquetListForAtomicWrite` and `S3ParquetWrite` are intentionally split into two statements because `s3:ListBucket` is a bucket-level action that must target the bucket ARN, while object actions target the `employees*` key prefix. Combining them into one statement would require the bucket ARN as a resource, which would silently grant list access to all prefixes.

The Lambda role gets `grant_read_data()` on DynamoDB plus `kms:Decrypt` on the CMK. Without the explicit `kms:Decrypt` grant, every `GetItem` call against a CUSTOMER_MANAGED-encrypted table returns `KMSAccessDeniedException`.

### 3.4 CloudWatch Alarms

| Alarm | Metric | Threshold | Action |
|---|---|---|---|
| `hr-pipeline-glue-job-failed` | `Glue/numFailedTasks` | > 0 | SNS → on-call |
| `hr-pipeline-reconciliation-mismatch` | `HRPipeline/ReconciliationMismatch` | > 0 | SNS → on-call |

Both alarms route to `hr-pipeline-alerts` SNS topic. Subscribe an email address or PagerDuty endpoint to receive notifications.

---

## 4. Stale Data and the Atomic Partition Write Pattern

> This section documents a compliance incident discovered during live pipeline validation and the two-phase atomic fix that prevents recurrence.

### 4.1 Root Cause

Glue's `getSink()` API **appends** new Parquet files to existing S3 partition prefixes. It does not overwrite or replace existing files. When a transformation changes between pipeline runs, Athena scans all files in the partition (old and new), producing mixed output.

**Concrete impact observed**: The `lastname` column was SHA-256 hashed starting from run N. Runs N-1 and earlier had written plain-text last names to the same S3 partitions. Athena returned both values in a single query result: a direct GDPR violation (pseudonymised and non-pseudonymised PII co-existing in the analytical sink).

### 4.2 Why This Is a Compliance Violation

The Differential Privacy model implemented here relies on a hard boundary:

| Sink | `lastname` value |
|---|---|
| DynamoDB (operational) | Plain-text (HR API requires real names) |
| S3 Parquet (analytical) | SHA-256 hash only; data scientists must never see real names |

If old plain-text files survive in the Parquet prefix, that boundary collapses. Any Athena query over a multi-run partition exposes real last names regardless of the current job's masking logic.

### 4.3 The Fix: Two-Phase Atomic Write (Stage → Verify → Swap)

The first fix (purge-then-getSink) had a residual 1% risk: if `getSink` failed midway, the production partition was left **empty** (data loss). The empty partition looked identical to a missing partition to Athena - silent failure.

`_atomic_parquet_write()` in `etl_job.py` implements a two-phase commit:

```
Phase 1 - Stage:   Spark writes to employees_staging/
                   Production is completely untouched.
                   If this fails → RuntimeError, nothing lost, safe to replay.

Phase 2 - Verify:  Assert staging/ contains ≥ 1 Parquet file.
                   If empty → RuntimeError before touching production.

Phase 3 - Swap:    Purge only affected production partition prefixes.
                   Server-side copy staging → production (no egress cost).
                   Purge staging prefix (including Spark's _SUCCESS marker).

Phase 4 - Catalog: BatchCreatePartition for new partitions.
                   UpdatePartition for partitions that already exist.
                   Athena sees fresh data instantly; no MSCK REPAIR TABLE.
```

**Visualised:**
```
employees_staging/year=2019/month=7/dept=500/
  part-00000-...snappy.parquet   ← Phase 1 writes here

  Phase 2: verified non-empty ✓

  Phase 3a: employees/year=2019/month=7/dept=500/ → purged
  Phase 3b: staging files → server-side copied to employees/
  Phase 3c: staging prefix → purged

employees/year=2019/month=7/dept=500/
  part-00000-...snappy.parquet   ← only SHA-256 hashed lastname survives
```

### 4.4 Why Not `df.write.mode("overwrite")`?

The Spark DataFrame API `df.write.mode("overwrite").parquet(path)` with `spark.sql.sources.partitionOverwriteMode=dynamic` achieves the same partition-level overwrite. However, it bypasses `getSink`, which means:

- Glue does not call `glue:BatchCreatePartition` after the write.
- New partitions do not appear in the Glue Data Catalog automatically.
- Athena queries against the catalog table return no results for new partitions until `MSCK REPAIR TABLE` is run manually.

`_atomic_parquet_write()` achieves overwrite semantics **and** keeps full catalog continuity via explicit `BatchCreatePartition` / `UpdatePartition` - strictly superior for this architecture.

### 4.5 Remaining Risk and Mitigations

The 1% risk that remains: if Phase 3 copy fails mid-partition, that specific partition is empty until the next successful run.

| Risk | Mitigation |
|---|---|
| Phase 3 copy failure | Reconciliation alarm catches empty/missing rows within the next run |
| Partial staging write | Phase 2 verify catches it before production is touched |
| Staging left orphaned | Phase 3c cleanup runs in the same execution; stale staging is overwritten by the next run's Phase 1 (`mode("overwrite")`) |
| Catalog out of sync | Phase 4 runs after every successful swap; `UpdatePartition` handles re-runs |

### 4.6 IAM for the Atomic Pattern

The split between `S3ParquetListForAtomicWrite` and `S3ParquetWrite` is deliberate:

- `s3:ListBucket` is a **bucket-level** action requiring the bucket ARN as resource. Combining it with object actions in one statement forces the bucket ARN into the object statement, silently granting listing on all prefixes.
- Separating them with a `StringLike` prefix condition (`employees/*` and `employees_staging/*`) ensures the Glue role cannot list `athena-results/` or any other prefix in the same bucket, so lateral movement is blocked.

### 4.7 Prevention Checklist for Future Engineers

If you change any column transformation in the Parquet sink path:

- [ ] Confirm `_atomic_parquet_write()` is called immediately after `parquet_df` is computed.
- [ ] After deploying the new job, run `python src/reconciliation/reconcile.py` to verify counts.
- [ ] Query Athena: `SELECT MIN(LENGTH(lastname)), MAX(LENGTH(lastname)) FROM hr_analytics.employees` - both values must be 64. Any deviation means a stale plain-text file survived the swap.
- [ ] If stale files are found: check IAM `S3ParquetListForAtomicWrite` is attached and its prefix conditions include `employees/*` and `employees_staging/*`.
- [ ] If staging prefix contains files after a job run: the job failed during Phase 3; the next run's Phase 1 overwrites staging cleanly; run reconciliation to verify production counts.

---

## 5. Recovery Point Objective (RPO)

**Target RPO**: Zero data loss for records that passed DQ validation. Quarantined rows are preserved indefinitely in S3.

### 5.1 Job Bookmark (Incremental Replay)

The ETL job runs with `--job-bookmark-option=job-bookmark-enable`. Glue records the S3 object version and byte offset of each successfully processed file. On re-run:

- Files already processed in a previous run are skipped.
- Only new or modified files are processed.

To **reset and replay from scratch** (e.g. after a DynamoDB table restore):

```bash
aws glue reset-job-bookmark --job-name <GLUE_JOB_NAME>
```

Then re-trigger the job. All source CSVs are re-processed from the beginning.

### 5.2 S3 as Source of Truth

Raw CSVs in `s3://<RAW-BUCKET>/raw/employees/` are the authoritative source. Because the ETL only reads from S3 (never modifies it), the pipeline is **idempotent**: re-running against the same S3 data produces the same DynamoDB and Parquet output. DynamoDB `PutItem` overwrites existing items with identical data.

### 5.3 Replay Steps

If a pipeline run fails after partial writes:

1. Identify the failed Glue Job Run ID in CloudWatch.
2. Check quarantine bucket for rows that were isolated; determine if re-processing is safe.
3. Reset the job bookmark if a full replay is needed: `aws glue reset-job-bookmark`.
4. Re-trigger the job via the Glue console or `aws glue start-job-run`.
5. After completion, run `python src/reconciliation/reconcile.py` to verify counts converge.

### 5.4 Quarantine Remediation

Quarantined rows are not permanently lost. They are stored in S3 for manual remediation:

1. Download from `s3://<QUARANTINE-BUCKET>/quarantine/employees/run=<JOB_NAME>/`
2. Fix the underlying data quality issue in the source CSV.
3. Re-upload the corrected records to `s3://<RAW-BUCKET>/raw/employees/`.
4. Reset the job bookmark and re-run the pipeline.
