# FinOps Strategy

Cost was a first-class constraint from day one. Every infrastructure decision below has a measurable price tag attached. A pipeline that works but runs unchecked is a pipeline that surprises you on your AWS bill.

| Decision | Rationale | Savings vs. default |
|---|---|---|
| **G.1X workers (2×)** | Minimum viable worker size for batch PySpark ETL. G.2X doubles DPU cost with no throughput benefit at 1,000-row scale. | ~$0.11/run |
| **`MaxRetries = 0`** | Runaway retries on a misconfigured job silently double or triple DPU spend. Fail fast; alert; fix. | Up to 2x DPU savings |
| **`Timeout = 5 min`** | Hard kill at 5 minutes prevents a hung Spark stage from accumulating DPU-hours indefinitely. | Unbounded protection |
| **Athena 100 MB scan cap** | `BytesScannedCutoffPerQuery = 104857600` on `hr_analytics_wg`. Ad-hoc queries against the wrong table cannot scan the full dataset. | Cap prevents billing surprises |
| **Hive partitioning (year/month/dept)** | Athena prunes partitions at query time. A query scoped to one department and one month reads ~1/72 of the dataset. | Up to 98% Athena cost reduction |
| **Static Glue Catalog (no Crawler)** | Crawlers run on a schedule, consume DPUs, and cost ~$0.44/hour. Schema authority lives in CDK `CfnTable` definitions - free at synth time. | ~$0.44/crawl eliminated |
| **CloudWatch Alarm (~$0.10/month)** | Early detection prevents a silent failure from persisting for days and requiring a costly backfill run. | Pays for itself on the first incident |

The most counterintuitive decision here is `MaxRetries = 0`. The instinct is to add retries for resilience. But for a batch ETL job with a DQ gate, a retry on bad data doesn't fix anything - it just burns DPUs twice and delays the alert. Fail fast, fix the data, rerun deliberately.
