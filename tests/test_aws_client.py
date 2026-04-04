"""Test cases for AWS Client"""

from unittest.mock import Mock, patch

import pytest

from src.aws_client import AWSClient


class TestAWSClient:
    """Test AWS Client functionality"""

    @patch("src.aws_client.aws_config")
    def test_init_with_valid_config(self, mock_config):
        """Test initialization with valid AWS config"""
        mock_config.validate_config.return_value = True
        mock_config.get_boto3_config.return_value = {"region_name": "us-east-1"}

        with patch("boto3.Session"):
            client = AWSClient()
            assert client is not None

    @patch("src.aws_client.aws_config")
    def test_init_with_invalid_config(self, mock_config):
        """Test initialization fails with invalid AWS config"""
        mock_config.validate_config.return_value = False

        with pytest.raises(ValueError, match="AWS credentials not properly configured"):
            AWSClient()

    @patch("src.aws_client.aws_config")
    def test_list_s3_buckets(self, mock_config):
        """Test listing S3 buckets"""
        mock_config.validate_config.return_value = True
        mock_config.get_boto3_config.return_value = {"region_name": "us-east-1"}

        with patch("boto3.Session") as mock_session:
            mock_s3_client = Mock()
            mock_s3_client.list_buckets.return_value = {
                "Buckets": [{"Name": "test-bucket-1"}, {"Name": "test-bucket-2"}]
            }

            mock_session.return_value.client.return_value = mock_s3_client

            client = AWSClient()
            buckets = client.list_s3_buckets()

            assert len(buckets) == 2
            assert "test-bucket-1" in buckets
            assert "test-bucket-2" in buckets
