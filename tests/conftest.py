""" Shared test fixtures.

The handlers call storage.* directly, so a test just needs a live mock bucket
with storage.BUCKET pointed at it - no per-module double-patching like the old
mongomock setup.
"""
import boto3
import moto
import pytest

from src import storage

BUCKET = "test-blog"
REGION = "us-west-2"


@pytest.fixture(autouse=True)
def s3_bucket(monkeypatch):
    """ a fresh mock bucket the storage module is pointed at for each test """
    with moto.mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        monkeypatch.setattr(storage, "BUCKET", BUCKET)
        # Force get_s3_client to build a client inside the moto context.
        monkeypatch.setattr(storage, "_client", None)
        yield client
