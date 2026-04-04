"""Main application entry point for AWS Glue Demo Project"""

import argparse
import sys

from .aws_client import AWSClient


def main():
    """Main application function"""
    parser = argparse.ArgumentParser(description="AWS Glue Demo CLI")

    # S3 operations
    parser.add_argument("--list-buckets", action="store_true", help="List all S3 buckets")
    parser.add_argument("--create-bucket", type=str, help="Create a new S3 bucket")
    parser.add_argument(
        "--upload-file", nargs=2, metavar=("BUCKET", "FILE"), help="Upload file to S3 bucket"
    )

    # Glue operations
    parser.add_argument("--list-glue-jobs", action="store_true", help="List all Glue jobs")
    parser.add_argument("--list-databases", action="store_true", help="List all Glue databases")
    parser.add_argument("--create-database", type=str, help="Create a new Glue database")
    parser.add_argument("--list-tables", type=str, help="List tables in a Glue database")
    parser.add_argument(
        "--create-job",
        nargs=3,
        metavar=("NAME", "ROLE_ARN", "SCRIPT_LOCATION"),
        help="Create a new Glue job",
    )
    parser.add_argument("--start-job", type=str, help="Start a Glue job")
    parser.add_argument(
        "--job-status", nargs=2, metavar=("JOB_NAME", "RUN_ID"), help="Get status of a Glue job run"
    )

    # EC2 operations (legacy support)
    parser.add_argument("--list-instances", action="store_true", help="List all EC2 instances")

    args = parser.parse_args()

    try:
        client = AWSClient()

        # S3 Operations
        if args.list_buckets:
            print("S3 Buckets:")
            buckets = client.list_s3_buckets()
            for bucket in buckets:
                print(f"  - {bucket}")

        elif args.create_bucket:
            success = client.create_s3_bucket(args.create_bucket)
            if success:
                print(f"Bucket '{args.create_bucket}' created successfully")
            else:
                print(f"Failed to create bucket '{args.create_bucket}'")
                sys.exit(1)

        elif args.upload_file:
            bucket_name, file_path = args.upload_file
            success = client.upload_file_to_s3(bucket_name, file_path)
            if success:
                print("File uploaded successfully")
            else:
                print("Failed to upload file")
                sys.exit(1)

        # Glue Operations
        elif args.list_glue_jobs:
            print("AWS Glue Jobs:")
            jobs = client.list_glue_jobs()
            for job in jobs:
                print(f"  - {job}")

        elif args.list_databases:
            print("AWS Glue Databases:")
            databases = client.list_glue_databases()
            for db in databases:
                print(f"  - {db}")

        elif args.create_database:
            success = client.create_glue_database(
                args.create_database, "Demo database created via CLI"
            )
            if success:
                print(f"Database '{args.create_database}' created successfully")
            else:
                print(f"Failed to create database '{args.create_database}'")
                sys.exit(1)

        elif args.list_tables:
            print(f"Tables in database '{args.list_tables}':")
            tables = client.list_glue_tables(args.list_tables)
            for table in tables:
                print(f"  - {table}")

        elif args.create_job:
            job_name, role_arn, script_location = args.create_job
            success = client.create_glue_job(
                job_name=job_name,
                role_arn=role_arn,
                script_location=script_location,
                description="Demo Glue job created via CLI",
            )
            if success:
                print(f"Glue job '{job_name}' created successfully")
            else:
                print(f"Failed to create Glue job '{job_name}'")
                sys.exit(1)

        elif args.start_job:
            run_id = client.start_glue_job(args.start_job)
            if run_id:
                print(f"Job started. Run ID: {run_id}")
                print(f"Monitor status with: --job-status {args.start_job} {run_id}")
            else:
                print(f"Failed to start job '{args.start_job}'")
                sys.exit(1)

        elif args.job_status:
            job_name, run_id = args.job_status
            status = client.get_job_run_status(job_name, run_id)
            if status:
                print("Job Run Status:")
                print(f"  State: {status['JobRunState']}")
                print(f"  Started: {status['StartedOn']}")
                if status["CompletedOn"]:
                    print(f"  Completed: {status['CompletedOn']}")
                if status["ErrorMessage"]:
                    print(f"  Error: {status['ErrorMessage']}")
            else:
                print("Failed to get job status")
                sys.exit(1)

        # EC2 Operations (legacy)
        elif args.list_instances:
            print("EC2 Instances:")
            instances = client.list_ec2_instances()
            for instance in instances:
                print(
                    f"  - {instance['InstanceId']}"
                    f" ({instance['InstanceType']})"
                    f" - {instance['State']}"
                )

        else:
            print("AWS Glue Demo CLI")
            print("\nCommon Glue workflows:")
            print("1. List databases: --list-databases")
            print("2. Create database: --create-database my_demo_db")
            print("3. List jobs: --list-glue-jobs")
            print(
                "4. Create job: --create-job my-job arn:aws:iam::account:role/GlueRole s3://bucket/script.py"
            )
            print("5. Start job: --start-job my-job")
            print("6. Check status: --job-status my-job run_id")
            print("\nUse --help for all options")

    except ValueError as e:
        print(f"Configuration error: {e}")
        print("Please ensure your AWS credentials are properly configured in .env file")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
