# Phase 1 Specification: Baseline Pipeline Setup

This document defines the Phase 1 contract for the employee Glue setup pipeline.
Phase 1 provisions all AWS infrastructure required for the ETL to run and validates
that the environment is safe before any data processing begins.

---

## Scope

Phase 1 covers the **standalone `pipeline_runner.py` provisioning script** only.
It provisions a self-contained learning/testing environment (`employee_db`,
`Employees` table keyed by `EmployeeID`) that is separate from the CDK-deployed
production stack.

The CDK stack (`infrastructure/infrastructure_stack.py`) provisions its own
resources independently: the `hr_analytics` Glue catalog, the
`aws-glue-demo-single-table` DynamoDB table with `PK`/`SK` string keys, and
SSM parameters for runtime config. Running Phase 1 does **not** configure the
prerequisites for the CDK ETL/Lambda path.

---

## Required Components

### `src/config.py`
- Resolves the AWS account ID via STS at startup (`__post_init__`).
- Centralises all resource names: bucket, database, crawler, IAM role, DynamoDB table.
- `GLUE_TABLE_NAME` matches the table name the Glue crawler derives from the S3 path
  suffix (e.g. `raw/employees/` → `employees`).
- Region defaults to `eu-west-2`; overridable via `AWS_REGION` environment variable.
- Raises `ValueError` if `AWS_REGION` is set to an empty string.

### `src/s3_setup.py`
- `create_bucket`: provisions one raw-data bucket; idempotent on `BucketAlreadyOwnedByYou`.
- `configure_bucket_security`: enforces three baseline controls on every run:
  - **Versioning** enabled (audit trail, enables point-in-time recovery).
  - **Public access block** (all four flags set to `True`).
  - **Default SSE-S3 encryption** (`AES256`) for all new objects.
- `upload_file`: streams the source CSV to `raw/employees/employee_data.csv`.
- `verify_upload`: confirms the object exists via `HeadObject` before proceeding.

### `src/iam_setup.py`
- `create_glue_role`: creates `AWSGlueRole-EmployeeETL` with a Glue trust policy; idempotent.
- `attach_glue_policies`: attaches only `AWSGlueServiceRole` (no broad DynamoDB access).
- `put_s3_inline_policy`: scoped inline policy granting `GetObject`/`PutObject` on
  bucket objects and `ListBucket` on the bucket; nothing wider.
- `get_role_arn`: resolves the role ARN for passing to the crawler; raises `RuntimeError`
  if the role does not exist.

### `src/glue_setup.py`
- `create_database`: creates `employee_db` in the Glue Data Catalog; idempotent.
- `create_crawler`: creates `employee-csv-crawler` targeting `raw/employees/`.
  - **Reconciliation on existing crawler**: calls `update_crawler` to sync role, targets,
    schema-change policy, and recrawl policy to the desired state.
  - **Schedule guard**: raises `CrawlerConfigurationError` if the existing crawler has a
    recurring schedule (prevents accidental automated runs).
- `run_crawler`: starts the crawler and polls until `READY` or timeout (5 minutes).
  - **Already RUNNING**: skips `start_crawler` and joins the in-flight run.
  - **STOPPING**: waits for `READY` before issuing `start_crawler` to avoid API errors.
  - **FAILED**: raises `CrawlerFailedError` with the error message from `LastCrawl`.

### `src/dynamodb_setup.py`
- `assert_pay_per_request`: pre-flight guard; raises `BillingModeError` if an existing
  `Employees` table uses `PROVISIONED` billing. Called before `create_table` to prevent
  silent capacity charges on re-runs.
- `create_table`: provisions `Employees` with `PAY_PER_REQUEST` billing and `EmployeeID`
  (Number) as the partition key; idempotent on `ResourceInUseException`.
- `wait_for_table_active`: polls until the table reaches `ACTIVE` status.

### `src/pipeline_runner.py`
- Orchestrates all setup modules in dependency order (Steps 1-16).
- Uses `_run_step` to wrap every function call with consistent start/fail/complete logging.
- Upload verification is wrapped in `_assert_upload` so failures are surfaced as logged
  step failures, not silent boolean returns.
- Prints a structured summary on success using `config` fields and runtime variables;
  no hard-coded resource names.

---

## Idempotency Contract

Every provisioning function must be safe to call on repeated runs against an already
configured environment:

| Resource | Idempotency mechanism |
|---|---|
| S3 bucket | `BucketAlreadyOwnedByYou` swallowed |
| Bucket security | `put_*` calls are unconditional overwrites |
| IAM role | `EntityAlreadyExists` swallowed |
| Policy attachment | `put_role_policy` is idempotent by API contract |
| Glue database | `AlreadyExistsException` swallowed |
| Glue crawler | `AlreadyExistsException` triggers `update_crawler` reconciliation |
| DynamoDB table | `ResourceInUseException` triggers billing-mode validation |

---

## Security Contract

| Control | Requirement |
|---|---|
| S3 encryption | Default SSE-S3 (`AES256`) enforced on every run |
| S3 public access | All four public-access-block flags set to `True` |
| S3 versioning | Enabled for audit trail and recovery |
| IAM managed policies | `AWSGlueServiceRole` only (no `AmazonDynamoDBFullAccess`) |
| IAM inline policy | Scoped to the pipeline bucket; no wildcard resources |
| DynamoDB billing | `PAY_PER_REQUEST` enforced; `PROVISIONED` rejected before write |
| Crawler schedule | Rejected at runtime; prevents unintended automated crawls |

---

## Error Taxonomy

| Exception | Module | Trigger |
|---|---|---|
| `BucketCreationError` | `s3_setup` | S3 API error other than already-exists |
| `UploadVerificationError` | `pipeline_runner` | `HeadObject` returns 404 after upload |
| `CrawlerConfigurationError` | `glue_setup` | Existing crawler has a schedule |
| `CrawlerFailedError` | `glue_setup` | Crawler ends in FAILED state or hits unexpected state |
| `CrawlerTimeoutError` | `glue_setup` | Crawler still running after 5 minutes |
| `TableNotFoundError` | `glue_setup` | Catalog table absent after crawler run |
| `BillingModeError` | `dynamodb_setup` | Existing table uses PROVISIONED billing |
| `TableActivationTimeoutError` | `dynamodb_setup` | Table not ACTIVE within timeout |

---

## Testing Contract

### Unit tests (no AWS credentials required)

| File | Coverage |
|---|---|
| `tests/test_s3_setup.py` | `create_bucket` (success, idempotent), `configure_bucket_security` (versioning, public-access-block, SSE-S3 encryption), `upload_file` (success, file-not-found), `verify_upload` (exists, missing) |
| `tests/test_iam_setup.py` | `create_glue_role` (success, idempotent), `attach_glue_policies` (single policy only), `put_s3_inline_policy` |
| `tests/test_glue_setup_unit.py` | `create_crawler` reconciliation (correct params asserted), schedule-present guard, `run_crawler` RUNNING wait, STOPPING wait |
| `tests/test_dynamodb_setup.py` | `create_table` (partition key, billing mode, idempotent), PROVISIONED billing rejection, `wait_for_table_active`, `assert_pay_per_request` |

### Integration tests (live AWS, `glue-learner` profile)

| File | Coverage |
|---|---|
| `tests/test_glue_setup.py` | Live database creation, crawler creation, crawler run (blocking), catalog table registered with ≥10 columns |

Run integration tests with:
```bash
pytest tests/test_glue_setup.py -v -m integration
```

---

## Pipeline Execution Order

```
1.  Load Config            - STS identity + resource name resolution
2.  Create S3 Bucket       - idempotent bucket provisioning
3.  Configure Bucket       - versioning + public-access-block + SSE-S3
4.  Upload CSV             - raw/employees/employee_data.csv -> S3
5.  Verify Upload          - HeadObject confirmation
6.  Create Glue IAM Role   - trust policy + service role
7.  Attach Managed Policy  - AWSGlueServiceRole only
8.  Put S3 Inline Policy   - scoped bucket access
9.  Create Glue Database   - employee_db
    [15 s IAM propagation delay]
10. Get Role ARN           - resolved for crawler registration
11. Create/Reconcile Crawler
12. Run Crawler            - blocking poll until READY
13. Get Catalog Table      - validate employee_db.employees exists
14. Validate DynamoDB Billing Mode - assert PAY_PER_REQUEST
15. Create DynamoDB Table  - Employees (idempotent)
16. Wait for Table ACTIVE
    -> Summary printed
```
