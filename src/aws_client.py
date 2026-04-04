"""AWS Client wrapper for common AWS operations with focus on Glue"""

import os
from typing import Any, Dict, List, Optional

import boto3

from config.aws_config import aws_config


class AWSClient:
    """Wrapper class for AWS services using boto3 with emphasis on Glue operations"""

    def __init__(self):
        if not aws_config.validate_config():
            raise ValueError("AWS credentials not properly configured")

        self.session = boto3.Session(**aws_config.get_boto3_config())
        self._s3_client = None
        self._glue_client = None
        self._ec2_client = None

    @property
    def s3(self):
        """Get S3 client"""
        if self._s3_client is None:
            self._s3_client = self.session.client("s3")
        return self._s3_client

    @property
    def glue(self):
        """Get Glue client"""
        if self._glue_client is None:
            self._glue_client = self.session.client("glue")
        return self._glue_client

    @property
    def ec2(self):
        """Get EC2 client"""
        if self._ec2_client is None:
            self._ec2_client = self.session.client("ec2")
        return self._ec2_client

    def list_s3_buckets(self) -> List[str]:
        """List all S3 buckets"""
        try:
            response = self.s3.list_buckets()
            return [bucket["Name"] for bucket in response["Buckets"]]
        except Exception as e:
            print(f"Error listing S3 buckets: {e}")
            return []

    def create_s3_bucket(self, bucket_name: str, region: Optional[str] = None) -> bool:
        """Create an S3 bucket"""
        try:
            if region is None:
                region = aws_config.region

            if region == "us-east-1":
                # us-east-1 doesn't support LocationConstraint
                self.s3.create_bucket(Bucket=bucket_name)
            else:
                self.s3.create_bucket(
                    Bucket=bucket_name, CreateBucketConfiguration={"LocationConstraint": region}
                )
            print(f"Bucket '{bucket_name}' created successfully")
            return True
        except Exception as e:
            print(f"Error creating bucket: {e}")
            return False

    def upload_file_to_s3(
        self, bucket_name: str, file_path: str, object_name: Optional[str] = None
    ) -> bool:
        """Upload a file to S3 bucket"""
        if object_name is None:
            object_name = os.path.basename(file_path)

        try:
            self.s3.upload_file(file_path, bucket_name, object_name)
            print(f"File '{file_path}' uploaded to '{bucket_name}/{object_name}'")
            return True
        except Exception as e:
            print(f"Error uploading file: {e}")
            return False

    def list_ec2_instances(self) -> List[Dict[str, Any]]:
        """List all EC2 instances"""
        try:
            response = self.ec2.describe_instances()
            instances = []
            for reservation in response["Reservations"]:
                for instance in reservation["Instances"]:
                    instances.append(
                        {
                            "InstanceId": instance["InstanceId"],
                            "InstanceType": instance["InstanceType"],
                            "State": instance["State"]["Name"],
                            "LaunchTime": instance["LaunchTime"],
                        }
                    )
            return instances
        except Exception as e:
            print(f"Error listing EC2 instances: {e}")
            return []

    # AWS Glue specific methods

    def list_glue_jobs(self) -> List[str]:
        """List all Glue jobs"""
        try:
            response = self.glue.list_jobs(MaxResults=100)
            return response.get("JobNames", [])
        except Exception as e:
            print(f"Error listing Glue jobs: {e}")
            return []

    def create_glue_job(
        self,
        job_name: str,
        role_arn: str,
        script_location: str,
        description: str = "",
    ) -> bool:
        """Create a new Glue job"""
        try:
            self.glue.create_job(
                Name=job_name,
                Description=description,
                Role=role_arn,
                ExecutionProperty={"MaxConcurrentRuns": 1},
                Command={
                    "Name": "glueetl",
                    "ScriptLocation": script_location,
                    "PythonVersion": "3",
                },
                Timeout=60,
                GlueVersion="4.0",
                NumberOfWorkers=2,
                WorkerType="G.1X",
            )
            print(f"Glue job '{job_name}' created successfully")
            return True
        except Exception as e:
            print(f"Error creating Glue job: {e}")
            return False

    def start_glue_job(
        self, job_name: str, arguments: Optional[Dict[str, str]] = None
    ) -> Optional[str]:
        """Start a Glue job"""
        try:
            response = self.glue.start_job_run(JobName=job_name, Arguments=arguments or {})
            job_run_id = response.get("JobRunId")
            print(f"Glue job '{job_name}' started with run ID: {job_run_id}")
            return job_run_id
        except Exception as e:
            print(f"Error starting Glue job: {e}")
            return None

    def get_job_run_status(self, job_name: str, run_id: str) -> Optional[Dict[str, Any]]:
        """Get the status of a Glue job run"""
        try:
            response = self.glue.get_job_run(JobName=job_name, RunId=run_id)
            return {
                "JobRunState": response.get("JobRun", {}).get("JobRunState"),
                "StartedOn": response.get("JobRun", {}).get("StartedOn"),
                "CompletedOn": response.get("JobRun", {}).get("CompletedOn"),
                "ErrorMessage": response.get("JobRun", {}).get("ErrorMessage"),
            }
        except Exception as e:
            print(f"Error getting job run status: {e}")
            return None

    def list_glue_databases(self) -> List[str]:
        """List all Glue databases"""
        try:
            response = self.glue.get_databases(MaxResults=100)
            return [db["Name"] for db in response.get("DatabaseList", [])]
        except Exception as e:
            print(f"Error listing Glue databases: {e}")
            return []

    def create_glue_database(self, database_name: str, description: str = "") -> bool:
        """Create a new Glue database"""
        try:
            self.glue.create_database(
                DatabaseInput={"Name": database_name, "Description": description}
            )
            print(f"Glue database '{database_name}' created successfully")
            return True
        except Exception as e:
            print(f"Error creating Glue database: {e}")
            return False

    def list_glue_tables(self, database_name: str) -> List[str]:
        """List all tables in a Glue database"""
        try:
            response = self.glue.get_tables(DatabaseName=database_name, MaxResults=100)
            return [table["Name"] for table in response.get("TableList", [])]
        except Exception as e:
            print(f"Error listing Glue tables: {e}")
            return []
