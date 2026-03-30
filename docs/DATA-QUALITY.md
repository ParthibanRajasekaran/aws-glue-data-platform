# Data Quality & Governance

## Two-tier quality strategy

A single DQ gate at the job level isn't enough - it tells you the dataset is clean in aggregate but lets individual bad rows through. This pipeline runs two checks in sequence.

### Tier 1 - Rule-level gate (fail-fast)

`EvaluateDataQuality` runs three rules against the fully enriched dataset before any write reaches either sink. A single failure aborts the job immediately - no partial data lands in DynamoDB or Parquet.

```
IsComplete "employeeid"
ColumnValues "salary" > 0
IsUnique "employeeid"
```

This runs *after* the joins and derived column calculations, which means it validates the enriched output, not the raw input. A null `employeeid` that slipped through the source data gets caught here before it poisons the DynamoDB table.

### Tier 2 - Row-level circuit breaker

Even when the aggregate rules pass, individual rows can carry bad data that doesn't affect the rule totals (for example, a dataset of 1,000 rows where 2 have null salary - `IsComplete` passes, but those 2 rows should never reach production).

Rows with `employeeid IS NULL`, `salary IS NULL`, or `salary <= 0` are quarantined:
- Logged to CloudWatch (`/aws-glue/jobs/output`) with count and a sample
- Excluded from both the DynamoDB write and the Parquet sink
- Clean rows proceed normally without job failure

The circuit breaker confirmed effective in production: 0 records with `Salary <= 0` found across 1,000 DynamoDB items in the CLI verification scan.

## Observability signals

| Signal | Implementation |
|---|---|
| **CloudWatch Dashboard** `hr-pipeline-observability` | Glue succeeded/failed task counts + Athena bytes scanned per query |
| **CloudWatch Alarm** `hr-pipeline-glue-job-failed` | Fires within 1 minute when `numFailedTasks > 0` |
| **SNS Topic** `hr-pipeline-alerts` | Alarm action target - subscribe an email or PagerDuty endpoint |
| **Glue DQ Results** | Published to the Glue Data Quality console under context `hr_etl_dq` |
| **CloudWatch Logs** `/aws-glue/jobs/output` | Circuit breaker quarantine logs (1-day retention) |

## What the alarm actually catches

The CloudWatch alarm fires on `numFailedTasks > 0` - a Spark-level metric. This covers:
- PySpark exceptions (including DQ rule failures which raise `Exception` and cause a task failure)
- Out-of-memory errors on executor nodes
- Job timeout (5-minute hard limit)

It does **not** catch a job that completes `SUCCEEDED` but wrote 0 rows due to a Job Bookmark filtering all source files as already-processed. That scenario requires a separate metric or a post-run row count check.
