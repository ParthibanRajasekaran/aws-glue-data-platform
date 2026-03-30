import base64
import json
import os

import aws_cdk as cdk
from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_athena as athena,
    aws_cloudwatch as cloudwatch,
    aws_cloudwatch_actions as cloudwatch_actions,
    aws_dynamodb as dynamodb,
    aws_glue as glue,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_s3 as s3,
    aws_s3_deployment as s3deploy,
    aws_sns as sns,
    aws_ssm as ssm,
)
import aws_cdk.aws_glue_alpha as glue_alpha
from constructs import Construct


class InfrastructureStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # ── KMS Key — must be declared before S3/DynamoDB ────────────────────────
        encryption_key = kms.Key(
            self,
            "PipelineEncryptionKey",
            enable_key_rotation=True,
            removal_policy=RemovalPolicy.DESTROY,
            description="CMK for HR pipeline S3 + DynamoDB encryption at rest",
        )

        # ── S3 Buckets ──────────────────────────────────────────────────────────
        raw_bucket = s3.Bucket(
            self,
            "RawDataBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=encryption_key,
        )

        parquet_bucket = s3.Bucket(
            self,
            "AthenaParquetBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=encryption_key,
        )

        assets_bucket = s3.Bucket(
            self,
            "AssetsBucket",
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
            encryption=s3.BucketEncryption.KMS,
            encryption_key=encryption_key,
        )

        # ── Glue Script Deployment — explicit scripts/ prefix ────────────────────
        # Deploys src/glue/etl_job.py to s3://[assets-bucket]/scripts/etl_job.py.
        # Using BucketDeployment instead of Code.from_asset() makes the script
        # location predictable and auditable — no CDK-generated hash in the key.
        glue_script_deployment = s3deploy.BucketDeployment(
            self,
            "GlueScriptDeployment",
            sources=[s3deploy.Source.asset(
                os.path.join(os.path.dirname(__file__), "..", "src", "glue")
            )],
            destination_bucket=assets_bucket,
            destination_key_prefix="scripts",
        )

        # ── DynamoDB Single-Table ────────────────────────────────────────────────
        table = dynamodb.Table(
            self,
            "SingleTable",
            table_name="aws-glue-demo-single-table",
            partition_key=dynamodb.Attribute(
                name="PK", type=dynamodb.AttributeType.STRING
            ),
            sort_key=dynamodb.Attribute(
                name="SK", type=dynamodb.AttributeType.STRING
            ),
            billing_mode=dynamodb.BillingMode.PAY_PER_REQUEST,
            removal_policy=RemovalPolicy.DESTROY,
            encryption=dynamodb.TableEncryption.CUSTOMER_MANAGED,
            encryption_key=encryption_key,
        )

        # ── SSM Parameter Store — resource discovery (Phase 4) ───────────────────
        # The Glue ETL script reads these at runtime; no ARNs/names in source code.
        ssm.StringParameter(
            self, "SsmKmsKeyArn",
            parameter_name="/hr-pipeline/kms-key-arn",
            string_value=encryption_key.key_arn,
            description="CMK ARN for HR pipeline encryption",
        )
        ssm.StringParameter(
            self, "SsmRawBucketName",
            parameter_name="/hr-pipeline/raw-bucket-name",
            string_value=raw_bucket.bucket_name,
            description="Raw CSV input bucket name",
        )
        ssm.StringParameter(
            self, "SsmParquetBucketName",
            parameter_name="/hr-pipeline/parquet-bucket-name",
            string_value=parquet_bucket.bucket_name,
            description="Parquet output bucket name",
        )
        ssm.StringParameter(
            self, "SsmDynamoTableName",
            parameter_name="/hr-pipeline/dynamodb-table-name",
            string_value=table.table_name,
            description="DynamoDB single-table name",
        )

        # ── IAM Role for Glue ETL Job ────────────────────────────────────────────
        glue_role = iam.Role(
            self,
            "GlueJobRole",
            assumed_by=iam.ServicePrincipal("glue.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AWSGlueServiceRole"
                )
            ],
        )

        # ── Data-access policy — single auditable ManagedPolicy ─────────────────
        # Consolidates all data permissions into one construct, replacing the
        # previous scattered add_to_policy calls and broad grant_write_data().
        iam.ManagedPolicy(
            self, "GlueDataAccessPolicy",
            description="Scoped data-access permissions for the HR ETL Glue job",
            roles=[glue_role],
            statements=[
                # s3:ListBucket retained: from_catalog() calls ListObjectsV2 to
                # enumerate files at the prefix before issuing GetObject per file.
                # Removing it causes AccessDenied before any read is attempted.
                iam.PolicyStatement(
                    sid="S3RawRead",
                    actions=["s3:GetObject", "s3:ListBucket"],
                    resources=[
                        raw_bucket.bucket_arn,
                        f"{raw_bucket.bucket_arn}/raw/*",
                    ],
                ),
                # employees* covers both data files and the Hadoop folder marker object
                iam.PolicyStatement(
                    sid="S3ParquetWrite",
                    actions=["s3:PutObject", "s3:DeleteObject"],
                    resources=[f"{parquet_bucket.bucket_arn}/employees*"],
                ),
                # Full CRUD: Glue writes TempDir shuffle spill, bookmarks, and script temp here
                iam.PolicyStatement(
                    sid="S3AssetsTempDir",
                    actions=["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"],
                    resources=[
                        assets_bucket.bucket_arn,
                        f"{assets_bucket.bucket_arn}/*",
                    ],
                ),
                # DescribeTable: Glue DynamoDB connector validates key schema pre-write.
                # PutItem + BatchWriteItem: connector write mode (batch path).
                # Replaces grant_write_data() which also granted UpdateItem, DeleteItem,
                # GetItem, Scan, Query, ConditionCheckItem — none used by the ETL job.
                iam.PolicyStatement(
                    sid="DynamoDBScopedWrite",
                    actions=[
                        "dynamodb:DescribeTable",
                        "dynamodb:PutItem",
                        "dynamodb:BatchWriteItem",
                    ],
                    resources=[table.table_arn],
                ),
                iam.PolicyStatement(
                    sid="KmsEncryptDecrypt",
                    actions=["kms:GenerateDataKey*", "kms:Decrypt"],
                    resources=[encryption_key.key_arn],
                ),
                iam.PolicyStatement(
                    sid="SsmReadConfig",
                    actions=["ssm:GetParameter", "ssm:GetParameters"],
                    resources=[
                        f"arn:aws:ssm:{cdk.Aws.REGION}:{cdk.Aws.ACCOUNT_ID}:parameter/hr-pipeline/*"
                    ],
                ),
                # glue:GetTable added: getSink reads the catalog schema before writing
                # partitions to verify output schema matches registered column definitions.
                # Without it, getSink raises AccessDeniedException before any write.
                iam.PolicyStatement(
                    sid="GlueCatalogPartitions",
                    actions=["glue:GetTable", "glue:BatchCreatePartition", "glue:UpdateTable"],
                    resources=[
                        f"arn:aws:glue:{cdk.Aws.REGION}:{cdk.Aws.ACCOUNT_ID}:catalog",
                        f"arn:aws:glue:{cdk.Aws.REGION}:{cdk.Aws.ACCOUNT_ID}:database/hr_analytics",
                        f"arn:aws:glue:{cdk.Aws.REGION}:{cdk.Aws.ACCOUNT_ID}:table/hr_analytics/employees",
                    ],
                ),
            ],
        )

        # ── Glue ETL Job ─────────────────────────────────────────────────────────
        # Script is read from the explicit scripts/ prefix deployed above.
        # Code.from_bucket references the known S3 path rather than a CDK hash.
        glue_job = glue_alpha.PySparkEtlJob(
            self,
            "HrEtlJob",
            script=glue_alpha.Code.from_bucket(
                assets_bucket,
                "scripts/etl_job.py",
            ),
            glue_version=glue_alpha.GlueVersion.V4_0,
            worker_type=glue_alpha.WorkerType.G_1X,
            number_of_workers=2,
            max_retries=0,
            timeout=Duration.minutes(5),
            default_arguments={
                # Bucket/table config now fetched from SSM at runtime — not passed here
                "--TempDir": f"s3://{assets_bucket.bucket_name}/temp/",
                "--job-language": "python",
                "--enable-continuous-cloudwatch-log": "true",
                # Phase 4: Incremental processing + observability
                "--job-bookmark-option":           "job-bookmark-enable",
                "--enable-metrics":                "",
                "--enable-observability-metrics":  "true",
                "--enable-glue-datacatalog":       "",
            },
            role=glue_role,
        )

        # Script must be in S3 before the job definition is created
        glue_job.node.add_dependency(glue_script_deployment)

        # MaxConcurrentRuns=1 — prevents race conditions on the DynamoDB single-table
        cfn_glue_job = glue_job.node.default_child
        cfn_glue_job.execution_property = glue.CfnJob.ExecutionPropertyProperty(
            max_concurrent_runs=1
        )

        # ── CloudWatch Log Retention ─────────────────────────────────────────────
        # /aws-glue/jobs/logs-v2 and /aws-glue/jobs/error are auto-created by
        # Glue on first run and already exist — CDK only manages /output.
        logs.LogGroup(
            self,
            "GlueLogOutput",
            log_group_name="/aws-glue/jobs/output",
            retention=logs.RetentionDays.ONE_DAY,
            removal_policy=RemovalPolicy.DESTROY,
        )

        # ── Glue Data Catalog ─────────────────────────────────────────────────────
        cfn_database = glue.CfnDatabase(
            self,
            "HrAnalyticsDatabase",
            catalog_id=cdk.Aws.ACCOUNT_ID,
            database_input=glue.CfnDatabase.DatabaseInputProperty(
                name="hr_analytics",
                description="HR analytics database — Phase 4 ETL pipeline",
            ),
        )

        # Helper to build a CSV CfnTable (avoids repeating the serde block)
        def _csv_table(construct_id, table_name, description, location, columns):
            t = glue.CfnTable(
                self,
                construct_id,
                catalog_id=cdk.Aws.ACCOUNT_ID,
                database_name="hr_analytics",
                table_input=glue.CfnTable.TableInputProperty(
                    name=table_name,
                    description=description,
                    table_type="EXTERNAL_TABLE",
                    parameters={
                        "classification": "csv",
                        "skip.header.line.count": "1",
                    },
                    storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                        # Trailing slash — Athena treats location as a prefix, not a file
                        location=location,
                        input_format="org.apache.hadoop.mapred.TextInputFormat",
                        output_format="org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                        serde_info=glue.CfnTable.SerdeInfoProperty(
                            serialization_library="org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe",
                            parameters={"field.delim": ","},
                        ),
                        columns=columns,
                    ),
                ),
            )
            t.add_dependency(cfn_database)
            return t

        # Raw source tables — schema matches the hardcoded StructType in etl_job.py
        raw_employees_table = _csv_table(
            "RawEmployeesTable",
            "raw_employees",
            "Raw employee CSV data — source for ETL",
            f"s3://{raw_bucket.bucket_name}/raw/employees/",
            [
                glue.CfnTable.ColumnProperty(name="EmployeeID",       type="int"),
                glue.CfnTable.ColumnProperty(name="FirstName",        type="string"),
                glue.CfnTable.ColumnProperty(name="LastName",         type="string"),
                glue.CfnTable.ColumnProperty(name="Email",            type="string"),
                glue.CfnTable.ColumnProperty(name="DeptID",           type="int"),
                glue.CfnTable.ColumnProperty(name="Department",       type="string"),
                glue.CfnTable.ColumnProperty(name="JobTitle",         type="string"),
                glue.CfnTable.ColumnProperty(name="Salary",           type="int"),
                glue.CfnTable.ColumnProperty(name="HireDate",         type="string"),
                glue.CfnTable.ColumnProperty(name="City",             type="string"),
                glue.CfnTable.ColumnProperty(name="State",            type="string"),
                glue.CfnTable.ColumnProperty(name="EmploymentStatus", type="string"),
                glue.CfnTable.ColumnProperty(name="ManagerID",        type="int"),
                glue.CfnTable.ColumnProperty(name="Manager",          type="string"),
            ],
        )

        _csv_table(
            "RawManagersTable",
            "raw_managers",
            "Raw managers CSV data — lookup table for ETL",
            f"s3://{raw_bucket.bucket_name}/raw/managers/",
            [
                glue.CfnTable.ColumnProperty(name="ManagerID",    type="int"),
                glue.CfnTable.ColumnProperty(name="ManagerName",  type="string"),
                glue.CfnTable.ColumnProperty(name="Department",   type="string"),
                glue.CfnTable.ColumnProperty(name="ManagerEmail", type="string"),
                glue.CfnTable.ColumnProperty(name="IsActive",     type="string"),
                glue.CfnTable.ColumnProperty(name="Level",        type="string"),
            ],
        )

        _csv_table(
            "RawDepartmentsTable",
            "raw_departments",
            "Raw departments CSV data — lookup table for ETL",
            f"s3://{raw_bucket.bucket_name}/raw/departments/",
            [
                glue.CfnTable.ColumnProperty(name="DeptID",           type="int"),
                glue.CfnTable.ColumnProperty(name="DepartmentName",   type="string"),
                glue.CfnTable.ColumnProperty(name="Budget",           type="int"),
                glue.CfnTable.ColumnProperty(name="MinSalaryRange",   type="int"),
                glue.CfnTable.ColumnProperty(name="MaxSalaryRange",   type="int"),
                glue.CfnTable.ColumnProperty(name="IsRemoteFriendly", type="string"),
            ],
        )

        # Enriched Parquet output table (Hive-partitioned)
        cfn_table = glue.CfnTable(
            self,
            "HrEmployeesTable",
            catalog_id=cdk.Aws.ACCOUNT_ID,
            database_name="hr_analytics",
            table_input=glue.CfnTable.TableInputProperty(
                name="employees",
                description="HR employee enriched data — Phase 4 ETL output",
                table_type="EXTERNAL_TABLE",
                parameters={
                    "classification": "parquet",
                    "parquet.compression": "SNAPPY",
                },
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location=f"s3://{parquet_bucket.bucket_name}/employees/",
                    input_format="org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
                    output_format="org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library="org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe",
                        parameters={"serialization.format": "1"},
                    ),
                    columns=[
                        glue.CfnTable.ColumnProperty(name="EmployeeID",         type="int"),
                        glue.CfnTable.ColumnProperty(name="FirstName",          type="string"),
                        glue.CfnTable.ColumnProperty(name="LastName",           type="string"),
                        glue.CfnTable.ColumnProperty(name="Email",              type="string"),
                        glue.CfnTable.ColumnProperty(name="DeptID",             type="string"),
                        glue.CfnTable.ColumnProperty(name="Department",         type="string"),
                        glue.CfnTable.ColumnProperty(name="JobTitle",           type="string"),
                        glue.CfnTable.ColumnProperty(name="Salary",             type="double"),
                        glue.CfnTable.ColumnProperty(name="HireDate",           type="date"),
                        glue.CfnTable.ColumnProperty(name="City",               type="string"),
                        glue.CfnTable.ColumnProperty(name="State",              type="string"),
                        glue.CfnTable.ColumnProperty(name="EmploymentStatus",   type="string"),
                        glue.CfnTable.ColumnProperty(name="ManagerID",          type="string"),
                        glue.CfnTable.ColumnProperty(name="Manager",            type="string"),
                        glue.CfnTable.ColumnProperty(name="DepartmentName",     type="string"),
                        glue.CfnTable.ColumnProperty(name="MaxSalaryRange",     type="double"),
                        glue.CfnTable.ColumnProperty(name="MinSalaryRange",     type="double"),
                        glue.CfnTable.ColumnProperty(name="Budget",             type="bigint"),
                        glue.CfnTable.ColumnProperty(name="ManagerName",        type="string"),
                        glue.CfnTable.ColumnProperty(name="IsActive",           type="string"),
                        glue.CfnTable.ColumnProperty(name="Level",              type="string"),
                        glue.CfnTable.ColumnProperty(name="HighestTitleSalary", type="double"),
                        glue.CfnTable.ColumnProperty(name="CompaRatio",         type="double"),
                        glue.CfnTable.ColumnProperty(name="RequiresReview",     type="boolean"),
                    ],
                ),
                partition_keys=[
                    glue.CfnTable.ColumnProperty(name="year",  type="int"),
                    glue.CfnTable.ColumnProperty(name="month", type="int"),
                    glue.CfnTable.ColumnProperty(name="dept",  type="string"),
                ],
            ),
        )
        cfn_table.add_dependency(cfn_database)

        # ── Athena Reconciliation View (Phase 4) ──────────────────────────────────
        # Compares row counts between raw CSV source and enriched Parquet output.
        # Stored as a VIRTUAL_VIEW in the Glue Catalog — queryable from Athena.
        _view_sql = (
            "SELECT 'raw_employees' AS data_source, COUNT(*) AS row_count "
            "FROM hr_analytics.raw_employees "
            "UNION ALL "
            "SELECT 'employees_parquet' AS data_source, COUNT(*) AS row_count "
            "FROM hr_analytics.employees"
        )
        _view_def = json.dumps(
            {
                "originalSql": _view_sql,
                "catalog": "awsdatacatalog",
                "schema": "hr_analytics",
                "columns": [
                    {"name": "data_source", "type": "varchar"},
                    {"name": "row_count",   "type": "bigint"},
                ],
                "owner": "admin",
                "runAsInvoker": False,
            },
            separators=(",", ":"),
        )
        _view_encoded = base64.b64encode(_view_def.encode("utf-8")).decode("utf-8")

        cfn_view = glue.CfnTable(
            self,
            "ReconciliationView",
            catalog_id=cdk.Aws.ACCOUNT_ID,
            database_name="hr_analytics",
            table_input=glue.CfnTable.TableInputProperty(
                name="v_etl_reconciliation",
                description="Reconciliation view: raw CSV count vs enriched Parquet count",
                table_type="VIRTUAL_VIEW",
                view_original_text=f"/* Presto View: {_view_encoded} */",
                view_expanded_text="/* Presto View */",
                parameters={"presto_view": "true"},
                storage_descriptor=glue.CfnTable.StorageDescriptorProperty(
                    location="",
                    input_format="",
                    output_format="",
                    serde_info=glue.CfnTable.SerdeInfoProperty(
                        serialization_library="org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe",
                    ),
                    columns=[
                        glue.CfnTable.ColumnProperty(name="data_source", type="string"),
                        glue.CfnTable.ColumnProperty(name="row_count",   type="bigint"),
                    ],
                ),
            ),
        )
        # View must be created after both its source tables
        cfn_view.add_dependency(cfn_table)
        cfn_view.add_dependency(raw_employees_table)

        # ── Athena Workgroup (Phase 4) ────────────────────────────────────────────
        # Dedicated workgroup with CloudWatch metrics to track per-query cost.
        # Results land in assets_bucket/athena-results/ — encrypted by bucket SSE-KMS.
        athena.CfnWorkGroup(
            self,
            "HrAnalyticsWorkgroup",
            name="hr_analytics_wg",
            description="Dedicated workgroup for HR analytics — CloudWatch metrics enabled",
            work_group_configuration=athena.CfnWorkGroup.WorkGroupConfigurationProperty(
                result_configuration=athena.CfnWorkGroup.ResultConfigurationProperty(
                    output_location=f"s3://{assets_bucket.bucket_name}/athena-results/",
                ),
                publish_cloud_watch_metrics_enabled=True,
                enforce_work_group_configuration=True,
                bytes_scanned_cutoff_per_query=104857600,  # 100 MB hard cap — prevents runaway scan cost
            ),
            recursive_delete_option=True,
        )

        # ── Lambda API Handler ────────────────────────────────────────────────────
        lambda_fn = lambda_.Function(
            self,
            "HrApiHandler",
            runtime=lambda_.Runtime.PYTHON_3_12,
            handler="handler.handler",
            code=lambda_.Code.from_asset(
                os.path.join(
                    os.path.dirname(__file__), "..", "src", "lambda"
                )
            ),
            timeout=Duration.seconds(30),
        )
        table.grant_read_data(lambda_fn)
        lambda_fn.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ssm:GetParameter"],
                resources=[
                    f"arn:aws:ssm:{cdk.Aws.REGION}:{cdk.Aws.ACCOUNT_ID}"
                    f":parameter/hr-pipeline/dynamodb-table-name"
                ],
            )
        )

        # ── CloudWatch Dashboard ──────────────────────────────────────────────────
        dashboard = cloudwatch.Dashboard(
            self,
            "HrPipelineDashboard",
            dashboard_name="hr-pipeline-observability",
        )
        dashboard.add_widgets(
            cloudwatch.GraphWidget(
                title="Glue ETL — Succeeded vs Failed Tasks",
                left=[
                    cloudwatch.Metric(
                        namespace="Glue",
                        metric_name="glue.driver.aggregate.numSucceededTasks",
                        dimensions_map={"JobName": glue_job.job_name},
                        label="Succeeded Tasks",
                        statistic="Sum",
                        period=Duration.minutes(5),
                    ),
                ],
                right=[
                    cloudwatch.Metric(
                        namespace="Glue",
                        metric_name="glue.driver.aggregate.numFailedTasks",
                        dimensions_map={"JobName": glue_job.job_name},
                        label="Failed Tasks",
                        statistic="Sum",
                        period=Duration.minutes(5),
                    ),
                ],
            ),
            cloudwatch.GraphWidget(
                title="Athena — Bytes Scanned per Query",
                left=[
                    cloudwatch.Metric(
                        namespace="AWS/Athena",
                        metric_name="DataScannedInBytes",
                        dimensions_map={"WorkGroup": "hr_analytics_wg"},
                        label="Bytes Scanned",
                        statistic="Sum",
                        period=Duration.minutes(5),
                    ),
                ],
            ),
        )

        # ── Alerting — SNS topic + CloudWatch Alarm ──────────────────────────────
        # The alarm fires within 1 minute of the first failed Glue task so that
        # on-call is paged before a user notices missing data.
        alerts_topic = sns.Topic(
            self,
            "HrPipelineAlerts",
            topic_name="hr-pipeline-alerts",
            display_name="HR Pipeline Alerts",
        )
        # Without this resource policy CloudWatch cannot publish to the topic.
        # The service principal must be explicitly granted sns:Publish.
        alerts_topic.add_to_resource_policy(
            iam.PolicyStatement(
                effect=iam.Effect.ALLOW,
                principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com")],
                actions=["sns:Publish"],
                resources=[alerts_topic.topic_arn],
            )
        )

        glue_failure_alarm = cloudwatch.Alarm(
            self,
            "GlueJobFailureAlarm",
            alarm_name="hr-pipeline-glue-job-failed",
            alarm_description=(
                "Glue HR ETL job has at least one failed task. "
                "Check /aws-glue/jobs/error log group for the root cause."
            ),
            metric=cloudwatch.Metric(
                namespace="Glue",
                metric_name="glue.driver.aggregate.numFailedTasks",
                dimensions_map={"JobName": glue_job.job_name},
                statistic="Sum",
                period=Duration.minutes(1),
            ),
            threshold=0,
            comparison_operator=cloudwatch.ComparisonOperator.GREATER_THAN_THRESHOLD,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
        )
        glue_failure_alarm.add_alarm_action(
            cloudwatch_actions.SnsAction(alerts_topic)
        )

        # ── Stack Outputs ─────────────────────────────────────────────────────────
        cdk.CfnOutput(self, "RawBucketName",     value=raw_bucket.bucket_name)
        cdk.CfnOutput(self, "ParquetBucketName", value=parquet_bucket.bucket_name)
        cdk.CfnOutput(self, "AssetsBucketName",  value=assets_bucket.bucket_name)
        cdk.CfnOutput(self, "GlueJobName",       value=glue_job.job_name)
        cdk.CfnOutput(self, "LambdaFunctionName",value=lambda_fn.function_name)
        cdk.CfnOutput(self, "DynamoTableName",   value=table.table_name)
        cdk.CfnOutput(self, "KmsKeyArn",         value=encryption_key.key_arn)
        cdk.CfnOutput(self, "AlertsTopicArn",   value=alerts_topic.topic_arn)
