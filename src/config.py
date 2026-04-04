"""Configuration dataclass for the AWS Glue Employee ETL pipeline."""

import os
from dataclasses import dataclass, field

import boto3


def _get_region() -> str:
    region = os.getenv("AWS_REGION", "eu-west-2")
    if region == "":
        raise ValueError("AWS_REGION environment variable is set but empty")
    return region


@dataclass
class Config:
    aws_profile: str = field(default_factory=lambda: os.getenv("AWS_PROFILE", "glue-learner"))
    aws_region: str = field(default_factory=lambda: _get_region())
    aws_account_id: str = field(default_factory=lambda: "")  # populated post-init

    # Constants
    S3_RAW_PREFIX: str = "raw/employees/"
    S3_SCRIPTS_PREFIX: str = "scripts/"
    GLUE_DATABASE_NAME: str = "employee_db"
    GLUE_CRAWLER_NAME: str = "employee-csv-crawler"
    GLUE_TABLE_NAME: str = "employees"
    GLUE_IAM_ROLE_NAME: str = "AWSGlueRole-EmployeeETL"
    DYNAMODB_TABLE_NAME: str = "Employees"
    LOCAL_DATA_FILE: str = "data/employee_data.csv"

    def __post_init__(self):
        # Fail fast if AWS_REGION is empty string
        if os.getenv("AWS_REGION") == "":
            raise ValueError("AWS_REGION environment variable is set but empty")
        # Resolve account ID via STS
        session = boto3.Session(profile_name=self.aws_profile, region_name=self.aws_region)
        sts = session.client("sts")
        self.aws_account_id = sts.get_caller_identity()["Account"]

    @property
    def S3_BUCKET_NAME(self) -> str:
        return f"employee-data-glue-{self.aws_account_id}"
