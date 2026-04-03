# aws-glue-demo - HR Analytics ETL Pipeline

HR compensation data typically lives across disconnected spreadsheets - raw employee records, salary bands, and manager hierarchies that nobody has joined together. This pipeline ingests those three CSV sources, enriches them with computed fields (compa-ratio, salary band position, manager activity flags), and serves the result two ways: a low-latency Lambda API for individual lookups and a partitioned Parquet data lake for Athena analytics.

Built with AWS CDK (Python), PySpark on Glue 4.0, and a deliberate focus on making every infrastructure decision auditable and cost-justified.

---

## What was verified in production

- **542 Parquet partition files** written to S3 (`year/month/dept`) - confirmed via AWS CLI audit
- **0 records with Salary <= 0** reached DynamoDB - `EvaluateDataQuality` circuit breaker confirmed effective across 1,000 scanned items
- **100 MB Athena scan guardrail** enforced at workgroup level, cannot be overridden per session
- **Zero hardcoded resource names** in source code - all config fetched from SSM at runtime

See [VERIFICATION.md](VERIFICATION.md) for the full CLI audit trail and the three production bugs that were root-caused and fixed during the build.

---

## Architecture

### Pipeline Overview

![Pipeline Architecture](docs/images/workflow.png)

### Data Access - KMS Encryption & SSM Zero-Trust Config

![Data Access Architecture](docs/images/data-access.png)

### End-to-End Architecture

```mermaid
flowchart TD
    subgraph Config["Zero-Trust Config"]
        SSM["SSM Parameter Store\nAll resource names resolved at runtime\nNo hardcoded ARNs in source code"]
    end

    subgraph Phase1["Phase 1 - Standalone Provisioner (pipeline_runner.py only)"]
        S3RAW["S3 Raw Bucket\nSSE-S3 encryption · Versioning\nAll public access blocked"]
        IAM["IAM Role: AWSGlueRole-EmployeeETL\nAWSGlueServiceRole (managed)\nScoped inline: GetObject/PutObject/ListBucket"]
        CRAWLER["Glue Crawler: employee-csv-crawler\nTargets raw/employees/\nIdempotent - reconciles on re-run"]
        DDB_TBL["DynamoDB: Employees (standalone schema)\nPAY_PER_REQUEST · PK: EmployeeID (Number)\nNot used by the CDK ETL path"]
    end

    subgraph Sources["Source Layer - S3 Raw Bucket (KMS CMK)"]
        EMP_CSV["employees.csv\nraw/employees/"]
        DEPT_CSV["departments.csv\nraw/departments/"]
        MGR_CSV["managers.csv\nraw/managers/"]
    end

    subgraph Catalog["Glue Data Catalog - hr_analytics (CDK-deployed)"]
        EMP_TBL["raw_employees\nCDK CfnTable - schema authority"]
        DEPT_TBL["raw_departments\nCDK CfnTable - schema authority"]
        MGR_TBL["raw_managers\nCDK CfnTable - schema authority"]
    end

    subgraph ETL["Phase 2 - PySpark ETL (Glue 4.0, G.1X x 2, Job Bookmarks ON)"]
        LC["(1) _lowercase_columns()\nNormalise all column names"]
        J1["(2) Broadcast JOIN\nemployees left-outer departments ON deptid"]
        J2["(3) Broadcast JOIN\nenriched left-outer managers ON managerid"]
        WIN["(4) Window Function\nMAX salary OVER PARTITION BY jobtitle"]
        CALC["(5) Derived Columns\ncomparatio = ROUND salary / maxsalaryrange, 2\nrequiresreview = comparatio > 1.0 OR isactive = False"]
        DQ["(6) EvaluateDataQuality Gate\nIsComplete(employeeid) · salary > 0 · IsUnique(employeeid)\nCircuit breaker - fail-fast on any violation"]
        TC["(7) TitleCase Remap\n24 columns total · Restores API contract"]
    end

    subgraph Sinks["Dual Sink"]
        ATOMIC["_atomic_parquet_write()\nStage to _staging/ prefix\nVerify row count\nRename to final prefix (atomic swap)"]
        S3PQ["S3 Parquet Bucket (KMS CMK)\nyear= / month= / dept= partitions\nSNAPPY compressed\nAthena workgroup: 100 MB scan guardrail"]
        DDB_WRITE["DynamoDB: aws-glue-demo-single-table\nPK (STRING) / SK (STRING)\nKMS CMK · PAY_PER_REQUEST"]
        QUAR["S3 Quarantine Bucket (KMS CMK)\nDQ-rejected rows only\nIsolated from clean data path"]
    end

    subgraph Consumer["API Consumer"]
        LAMBDA["Lambda: GetEmployee\nGetItem by PK=EMP#id SK=PROFILE\nSSM-resolved table name"]
        API["API Gateway\nREST endpoint"]
    end

    subgraph Observe["Observability"]
        CW["CloudWatch\nHRPipeline/ReconciliationMismatch\nGlue job duration + error metrics"]
        SNS["SNS: hr-pipeline-alerts\nEmail subscription\nCloudWatch alarm trigger"]
    end

    SSM -->|"resource names"| ETL
    SSM -->|"resource names"| LAMBDA

    EMP_CSV --> EMP_TBL
    DEPT_CSV --> DEPT_TBL
    MGR_CSV --> MGR_TBL

    EMP_TBL & DEPT_TBL & MGR_TBL --> LC
    LC --> J1 --> J2 --> WIN --> CALC --> DQ

    DQ -->|"all rules PASS"| TC
    DQ -->|"any rule FAILS"| QUAR
    TC --> DDB_WRITE
    TC --> ATOMIC --> S3PQ

    DDB_WRITE --> DDB_TBL
    DDB_TBL --> LAMBDA --> API

    DDB_WRITE & S3PQ --> CW --> SNS
```

### Data Model - Transform Steps

| Step | Operation | Output columns added |
|---|---|---|
| (1) lowercase | Normalise all headers | - |
| (2) JOIN departments | Left-outer on `deptid` | `departmentname · maxsalaryrange · minsalaryrange · budget` |
| (3) JOIN managers | Left-outer on `managerid` | `managername · isactive · level` |
| (4) Window | MAX salary OVER jobtitle | `highesttitlesalary` |
| (5) Derived | Arithmetic + boolean | `comparatio · requiresreview` |
| (6) DQ gate | Circuit breaker | - (quarantine bad rows) |
| (7) TitleCase | API contract remap | 24 final columns |

---

## Deep dives

| Topic | What's covered |
|---|---|
| [FinOps Strategy](docs/FINOPS.md) | Worker sizing, retry policy, Athena cost controls, why MaxRetries=0 is the right call |
| [Security](docs/SECURITY.md) | KMS encryption, SSM Zero-Trust config, IAM least-privilege statement-by-statement |
| [Data Quality & Governance](docs/DATA-QUALITY.md) | Two-tier DQ strategy, circuit breaker, what the CloudWatch alarm does and doesn't catch |
| [Athena Queries](docs/ATHENA-QUERIES.md) | Business-oriented queries with partition-aware patterns |
| [ADR 001 - Configuration Management](docs/ADR/001-configuration-management.md) | Why SSM over environment variables |

---

## Prerequisites

- AWS CLI configured (`aws configure`)
- Node.js >= 18
- Python >= 3.10
- `jq` (`brew install jq`)

---

## Deploy

```bash
bash deploy.sh
```

Steps performed automatically:
1. Install CDK CLI (if missing)
2. Install Python CDK dependencies
3. Bootstrap CDK in `us-east-1`
4. Deploy the CloudFormation stack (S3, Glue, DynamoDB, Lambda, KMS, SSM, CloudWatch, SNS)
5. Upload the 3 CSVs to the raw S3 bucket
6. Start the Glue ETL job

Monitor the Glue job (3-5 min):
```bash
aws glue get-job-run \
  --job-name $(jq -r '.HrPipelineStack.GlueJobName' outputs.json) \
  --run-id <run-id-from-deploy-output> \
  --region us-east-1 \
  --query 'JobRun.JobRunState'
```

---

## Verify

```bash
bash verify_api.sh           # employee 1001 (default)
bash verify_api.sh 1042      # any employee ID
```

Expected response:
```json
{
  "EmployeeID": "1001",
  "Name": "Patricia Martinez",
  "Department": "Sales",
  "JobTitle": "Sales AE",
  "Manager": "Jennifer Jones",
  "Salary": 121131.0,
  "CompaRatio": 0.79,
  "HighestTitleSalary": 139838.0,
  "RequiresReview": false
}
```

---

## Tests

```bash
# Lambda handler (no AWS required)
pytest tests/unit/test_handler.py -v

# E2E (requires deployed stack + completed Glue job)
pytest tests/e2e/ -v -m e2e
```

---

## Teardown

```bash
cdk destroy HrPipelineStack
```

All resources have `REMOVAL_POLICY.DESTROY`. The KMS key enters a 7-day scheduled deletion window after stack destroy.

---

## Project Structure

```
aws-glue-demo/
├── data/
│   ├── employee_data_updated.csv
│   ├── departments_data.csv
│   └── managers_data.csv
├── docs/
│   ├── ADR/
│   │   └── 001-configuration-management.md
│   ├── images/
│   │   ├── workflow.png
│   │   └── data-access.png
│   ├── FINOPS.md
│   ├── SECURITY.md
│   ├── DATA-QUALITY.md
│   └── ATHENA-QUERIES.md
├── infrastructure/
│   ├── app.py
│   ├── infrastructure_stack.py
│   └── requirements.txt
├── src/
│   ├── glue/
│   │   └── etl_job.py
│   └── lambda/
│       └── handler.py
├── tests/
│   ├── unit/
│   │   └── test_handler.py
│   └── e2e/
│       └── test_pipeline_e2e.py
├── README.md
├── VERIFICATION.md
├── deploy.sh
└── verify_api.sh
```
