# ADR 001 - Configuration Management: SSM Parameter Store over Environment Variables

| Field | Value |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-03-29 |
| **Author** | Parthiban Rajasekaran |
| **Reviewers** | QA Audit (Windsurf) |

---

## Context

During Phase 4 of the HR Analytics ETL pipeline, the Glue job and Lambda handler both required runtime knowledge of three resource identifiers:

- The DynamoDB table name
- The Parquet S3 bucket name
- The KMS key ARN

The initial Phase 2/3 implementation injected these values as environment variables at CDK synth time (`environment={"DYNAMO_TABLE": table.table_name}`). While convenient during early development, this approach had three structural problems:

1. **Infrastructure lifecycle coupling.** The table name was baked into the Lambda deployment package configuration at `cdk deploy` time. Any rename or re-create of the DynamoDB table required a Lambda redeployment - not because the _code_ changed, but because the _name_ changed.

2. **Zero-Trust violation.** Environment variables are visible in the AWS Lambda console, the CloudFormation template, and any snapshot of the deployment artefact. This violates the principle that secrets and operational config should be fetched at runtime from a controlled store with auditable access.

3. **Audit flagged.** An independent QA audit (Windsurf) identified the `DYNAMO_TABLE` environment variable as a contract violation. The finding prompted this decision.

---

## Decision

We chose **AWS SSM Parameter Store** (Standard tier, plaintext) as the single source of truth for all runtime resource configuration under the `/hr-pipeline/` path prefix.

At runtime:
- The **Glue job** calls `boto3.client("ssm").get_parameters(Names=[...])` at the top of `etl_job.py`, before any Spark operations. The resolved values are stored in module-level constants (`PARQUET_BASE`, `DYNAMO_TABLE`).
- The **Lambda handler** calls `boto3.client("ssm").get_parameter(Name=...)` at module load time (outside the handler function), so the SSM call runs once per cold start and is skipped on warm invocations.

Both callers wrap the SSM fetch in a `try/except` block that raises a `RuntimeError` with a clear diagnostic if the parameter is absent or the IAM role lacks `ssm:GetParameter`.

IAM is scoped to the exact parameter path:

```
arn:aws:ssm:<region>:<account>:parameter/hr-pipeline/*   (Glue role)
arn:aws:ssm:<region>:<account>:parameter/hr-pipeline/dynamodb-table-name   (Lambda role)
```

No broader SSM access is granted to either principal.

### Alternatives considered

| Option | Why rejected |
|---|---|
| **AWS Secrets Manager** | Overkill for non-secret operational config; costs $0.40/secret/month vs $0.00 for SSM Standard. |
| **Keep environment variables** | Fails Zero-Trust contract; redeploy required for config-only changes. |
| **CDK context / cdk.json** | Synth-time only; values still embedded in the CloudFormation template. |
| **Hardcoded resource names** | Non-starter; breaks in multi-account / multi-stage deployments. |

---

## Consequences

### Positive

- **Decoupled lifecycles.** Changing the DynamoDB table name (e.g., blue/green swap) requires only an SSM `put-parameter` - no Lambda or Glue redeployment.
- **Auditable access.** Every SSM `GetParameter` call is logged in AWS CloudTrail, giving a full audit trail of which principal read which config value and when.
- **Zero-Trust compliant.** No resource names appear in source code, environment variables, or deployment artefacts.
- **Single source of truth.** The CDK stack writes the parameters; callers read them. There is no other authoritative location.

### Negative / Trade-offs

- **Slightly higher cold-start latency.** The Lambda handler makes one SSM API call per cold start (~15–40 ms on a warm VPC-less Lambda). This is negligible for the HR query workload (no SLA < 1 s).
- **Additional IAM surface.** Each principal needs an explicit `ssm:GetParameter` grant. This is a deliberate, reviewable permission - not a drawback.
- **SSM availability dependency.** If the SSM regional endpoint is unavailable, the Lambda cold start and Glue job start will fail. In practice, SSM SLA (99.9%) is higher than the pipeline's own SLA.

---

## Parameter Inventory

| SSM Path | Written by | Read by | Purpose |
|---|---|---|---|
| `/hr-pipeline/kms-key-arn` | CDK deploy | Audit tooling | KMS key ARN for encryption verification |
| `/hr-pipeline/raw-bucket-name` | CDK deploy | Ops runbooks | S3 bucket for raw CSV source data |
| `/hr-pipeline/parquet-bucket-name` | CDK deploy | Glue ETL job | S3 output path for Parquet sink |
| `/hr-pipeline/dynamodb-table-name` | CDK deploy | Glue ETL job, Lambda | DynamoDB table for DynamoDB sink and read API |

---

## References

- [AWS SSM Parameter Store pricing](https://aws.amazon.com/systems-manager/pricing/)
- [AWS Well-Architected: Security Pillar - Secrets Management](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/protecting-secrets.html)
- QA Audit finding: Zero-Trust Violation in `infrastructure_stack.py` (remediated in commit `703542d`)
