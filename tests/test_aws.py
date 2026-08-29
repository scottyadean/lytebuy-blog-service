""" Unit tests for the shared boto_connect helper.

The three credential branches (session token, named profile, default chain) are
exercised by faking boto3.client / boto3.resource / boto3.Session so no real AWS
call is made.
"""
import boto3
import pytest

from src import aws

# Every AWS credential env var boto_connect looks at, cleared before each test so
# a developer's real shell profile never leaks into the assertions.
_AWS_ENV_VARS = (
    "AWS_SESSION_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_PROFILE",
    "AWS_DEFAULT_REGION",
    "REGION",
)


@pytest.fixture(autouse=True)
def s3_bucket():
    """ override conftest's moto fixture: these tests fake boto3, no S3 needed

    Opting out matters because moto stubs AWS credential env vars and restores
    them on teardown; clean_aws_env deleting them would break moto's restore.
    """
    yield None


@pytest.fixture(autouse=True)
def clean_aws_env(monkeypatch):
    for name in _AWS_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def spy_boto(monkeypatch):
    """ record how boto3 was called and hand back sentinel objects """
    calls = {}

    def fake_client(service, region=None, **kwargs):
        calls["client"] = {"service": service, "region": region, "kwargs": kwargs}
        return "client"

    def fake_resource(service, region=None, **kwargs):
        calls["resource"] = {"service": service, "region": region, "kwargs": kwargs}
        return "resource"

    class FakeSession:
        def __init__(self, profile_name=None, region_name=None):
            calls["session"] = {"profile_name": profile_name, "region_name": region_name}

        def client(self, service, region=None):
            calls["session_client"] = {"service": service, "region": region}
            return "session-client"

        def resource(self, service, region=None):
            calls["session_resource"] = {"service": service, "region": region}
            return "session-resource"

    monkeypatch.setattr(boto3, "client", fake_client)
    monkeypatch.setattr(boto3, "resource", fake_resource)
    monkeypatch.setattr(boto3, "Session", FakeSession)
    return calls


def test_defaults_to_the_plain_client_when_no_creds_are_set(spy_boto):
    assert aws.boto_connect("s3") == "client"
    assert spy_boto["client"]["service"] == "s3"
    assert "session" not in spy_boto


def test_region_comes_from_the_region_env_var(spy_boto, monkeypatch):
    monkeypatch.setenv("REGION", "eu-west-1")
    aws.boto_connect("s3")
    assert spy_boto["client"]["region"] == "eu-west-1"


def test_region_falls_back_to_aws_default_region(spy_boto, monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "ap-southeast-2")
    aws.boto_connect("s3")
    assert spy_boto["client"]["region"] == "ap-southeast-2"


def test_explicit_region_wins_over_the_env(spy_boto, monkeypatch):
    monkeypatch.setenv("REGION", "eu-west-1")
    aws.boto_connect("s3", region="us-east-1")
    assert spy_boto["client"]["region"] == "us-east-1"


def test_a_session_token_uses_the_static_credentials(spy_boto, monkeypatch):
    monkeypatch.setenv("AWS_SESSION_TOKEN", "tok")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")

    assert aws.boto_connect("s3", region="us-west-2") == "client"
    assert spy_boto["client"]["kwargs"] == {
        "aws_access_key_id": "key",
        "aws_secret_access_key": "secret",
        "aws_session_token": "tok",
    }
    assert "session" not in spy_boto


def test_a_profile_opens_a_named_session(spy_boto, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "lytebuy-dev")

    assert aws.boto_connect("s3", region="us-west-2") == "session-client"
    assert spy_boto["session"] == {"profile_name": "lytebuy-dev", "region_name": "us-west-2"}
    assert spy_boto["session_client"] == {"service": "s3", "region": "us-west-2"}


def test_a_session_token_wins_over_a_profile(spy_boto, monkeypatch):
    # Both set: the static-credential branch is checked first.
    monkeypatch.setenv("AWS_SESSION_TOKEN", "tok")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_PROFILE", "lytebuy-dev")

    assert aws.boto_connect("s3") == "client"
    assert "session" not in spy_boto


def test_resource_true_returns_a_resource(spy_boto):
    assert aws.boto_connect("s3", resource=True) == "resource"
    assert spy_boto["resource"]["service"] == "s3"


def test_resource_true_with_a_profile_uses_the_session_resource(spy_boto, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "lytebuy-dev")
    assert aws.boto_connect("s3", resource=True) == "session-resource"
    assert spy_boto["session_resource"]["service"] == "s3"
