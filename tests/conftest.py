"""Shared pytest fixtures and mark registrations."""

from unittest.mock import patch

import boto3
import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "unit: marks tests as unit tests (no AWS calls)")
    config.addinivalue_line(
        "markers",
        "integration: marks tests as integration tests (requires live AWS)",
    )


@pytest.fixture
def config():
    """Return a Config instance with STS mocked so no real AWS call is made."""
    with patch("boto3.Session") as mock_session_cls:
        mock_session = mock_session_cls.return_value
        mock_sts = mock_session.client.return_value
        mock_sts.get_caller_identity.return_value = {"Account": "123456789012"}

        from src.config import Config

        cfg = Config(aws_profile="glue-learner", aws_region="us-east-1")

    return cfg


@pytest.fixture
def boto3_session():
    """Return a real boto3 Session pointing at us-east-1 with the glue-learner profile."""
    return boto3.Session(profile_name="glue-learner", region_name="us-east-1")
