# Security

## Encryption at rest

All S3 buckets are encrypted with a Customer-Managed KMS Key (annual rotation enabled). DynamoDB uses AWS-managed KMS.

| Resource | Encryption |
|---|---|
| S3 raw bucket | SSE-KMS (pipeline key) |
| S3 Parquet bucket | SSE-KMS (pipeline key) |
| S3 assets bucket (Glue TempDir) | SSE-KMS (pipeline key) |
| DynamoDB single-table | AWS-managed KMS (`TableEncryption.AWS_MANAGED`) |

Using a customer-managed key on S3 means every read and write is gated by KMS. If the key is disabled, the data becomes inaccessible immediately - no waiting for IAM policy propagation. It also means every access is logged in CloudTrail with the caller identity, which matters for an HR dataset.

## Zero-Trust configuration

No resource name, ARN, or secret appears in source code or environment variables. The Glue job fetches all runtime config from SSM Parameter Store at startup:

| SSM Path | Consumer | Purpose |
|---|---|---|
| `/hr-pipeline/dynamodb-table-name` | Glue ETL, Lambda | DynamoDB table for sink and read API |
| `/hr-pipeline/parquet-bucket-name` | Glue ETL | S3 output path for Parquet sink |
| `/hr-pipeline/raw-bucket-name` | Ops runbooks | Raw CSV source bucket |
| `/hr-pipeline/kms-key-arn` | Audit tooling | KMS key for encryption verification |

See [ADR 001](ADR/001-configuration-management.md) for the full decision record on why SSM was chosen over environment variables.

## IAM least-privilege

No `Resource: "*"` exists in any custom IAM statement. All permissions are consolidated into a single `GlueDataAccessPolicy` ManagedPolicy so the full permission surface is auditable in one place.

| Principal | Actions | Scoped to |
|---|---|---|
| Glue job role | `s3:GetObject`, `s3:ListBucket` | `raw_bucket/raw/*` prefix only |
| Glue job role | `s3:PutObject`, `s3:DeleteObject` | `parquet_bucket/employees*` prefix |
| Glue job role | `s3:GetObject`, `s3:PutObject`, `s3:DeleteObject`, `s3:ListBucket` | `assets_bucket/*` (TempDir + script) |
| Glue job role | `dynamodb:DescribeTable`, `dynamodb:PutItem`, `dynamodb:BatchWriteItem` | `aws-glue-demo-single-table` only |
| Glue job role | `kms:GenerateDataKey*`, `kms:Decrypt` | Specific pipeline KMS key ARN |
| Glue job role | `ssm:GetParameter`, `ssm:GetParameters` | `/hr-pipeline/*` prefix |
| Glue job role | `glue:GetTable`, `glue:BatchCreatePartition`, `glue:UpdateTable` | `hr_analytics` DB + `employees` table only |
| Lambda role | `dynamodb:GetItem` | `aws-glue-demo-single-table` only |
| Lambda role | `ssm:GetParameter` | `/hr-pipeline/dynamodb-table-name` only |

The DynamoDB statement is deliberately narrow: `DescribeTable`, `PutItem`, `BatchWriteItem` only. The CDK `grant_write_data()` helper would have added `UpdateItem`, `DeleteItem`, `GetItem`, `Scan`, `Query`, and `ConditionCheckItem` - none of which the Glue DynamoDB connector needs. Scoping it down required understanding what the connector actually calls, which the VERIFICATION.md fix log covers.
