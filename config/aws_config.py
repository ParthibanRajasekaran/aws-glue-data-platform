import os

from dotenv import load_dotenv

load_dotenv()


class AWSConfig:
    """AWS configuration settings"""

    def __init__(self):
        self.access_key_id = os.getenv("AWS_ACCESS_KEY_ID")
        self.secret_access_key = os.getenv("AWS_SECRET_ACCESS_KEY")
        self.region = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
        self.session_token = os.getenv("AWS_SESSION_TOKEN")

    def get_boto3_config(self) -> dict:
        """Return boto3 configuration dictionary"""
        config = {
            "region_name": self.region,
        }

        if self.access_key_id and self.secret_access_key:
            config["aws_access_key_id"] = self.access_key_id
            config["aws_secret_access_key"] = self.secret_access_key

        if self.session_token:
            config["aws_session_token"] = self.session_token

        return config

    def validate_config(self) -> bool:
        """Validate that required AWS credentials are present"""
        return bool(self.access_key_id and self.secret_access_key)


# Global config instance
aws_config = AWSConfig()
