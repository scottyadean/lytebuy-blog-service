""" S3 storage layer for the lytebuy blog service.

Every record is one JSON object in a single bucket, keyed by content_type prefix
plus a UUID: posts/{uuid}.json, press-releases/{uuid}.json, featured/{uuid}.json.
All fields live inside the JSON body EXCEPT the view count, which is stored in
object metadata as x-amz-meta-view-count so a view bump never rewrites the body.

The handlers call these functions instead of talking to boto3 directly, which
keeps the S3 surface in one place and lets the tests point a whole module at a
mock bucket.
"""
import json
import logging
import os
import uuid

from botocore.exceptions import ClientError

from src.aws import boto_connect
from src.utils import PREFIXES, VIEW_COUNT_META_KEY

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BUCKET = os.getenv("BUCKET_NAME")
REGION = os.getenv("REGION", "us-west-2")

_JSON_CONTENT_TYPE = "application/json"
_KEY_SUFFIX = ".json"

# Cached across warm lambda invocations, mirroring the old MongoClient rationale:
# building a client per request rebuilds its connection pool every time.
_client = None


class NotFound(Exception):
    """ raised when an object key does not exist, so a handler can 404 """


def get_s3_client():
    """ get a cached boto3 s3 client

    boto_connect resolves credentials the same way everywhere: the Lambda role
    when deployed, or the developer's AWS_PROFILE / session env vars locally.
    """
    global _client
    if _client is None:
        _client = boto_connect("s3", region=REGION)
    return _client


def _require_bucket():
    """ the single place that turns a missing BUCKET_NAME into a clear error """
    if not BUCKET:
        raise RuntimeError("BUCKET_NAME is not set")
    return BUCKET


def new_id():
    """ mint a fresh record id

    One place so every create path produces the same id shape and is_valid_id
    can stay strict.
    """
    return str(uuid.uuid4())


def is_valid_id(value):
    """ true when value is a well-formed uuid safe to turn into an object key

    Replaces the old ObjectId check. Strict-uuid parsing rejects empty strings,
    path traversal ('..'), and embedded slashes for free, so a malformed path
    param can never be turned into an arbitrary S3 key.
    """
    if not value or not isinstance(value, str):
        return False
    try:
        # str(UUID(v)) round-trips only for a canonical uuid, so this also
        # rejects the "urn:" and braces forms UUID() would otherwise accept.
        return str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError, TypeError):
        return False


def build_key(content_type, item_id):
    """ the object key for a record: prefix + id + .json """
    return f"{PREFIXES[content_type]}{item_id}{_KEY_SUFFIX}"


def parse_key_id(key):
    """ recover the record id from an object key (inverse of build_key) """
    base = key.rsplit("/", 1)[-1]
    if base.endswith(_KEY_SUFFIX):
        base = base[: -len(_KEY_SUFFIX)]
    return base


def _next_view_count(current):
    """ the view-count increment, as a named calculation rather than inline

    A missing or blank stored value counts as zero so a freshly created object
    (or a bump before the first read) starts from one.
    """
    return int(current or "0") + 1


def _is_missing(err):
    """ true when a ClientError means the key/object was not found """
    code = err.response.get("Error", {}).get("Code")
    return code in ("NoSuchKey", "NotFound", "404")


def put_object(content_type, item_id, data, view_count):
    """ write a record's JSON body plus its view count metadata """
    get_s3_client().put_object(
        Bucket=_require_bucket(),
        Key=build_key(content_type, item_id),
        Body=json.dumps(data, default=str).encode("utf-8"),
        ContentType=_JSON_CONTENT_TYPE,
        Metadata={VIEW_COUNT_META_KEY: str(view_count)},
    )


def get_object(content_type, item_id):
    """ read a record, returns (data_dict, view_count) or (None, None) if absent """
    try:
        result = get_s3_client().get_object(
            Bucket=_require_bucket(), Key=build_key(content_type, item_id)
        )
    except ClientError as err:
        if _is_missing(err):
            return None, None
        raise
    data = json.loads(result["Body"].read())
    view_count = int(result.get("Metadata", {}).get(VIEW_COUNT_META_KEY, "0"))
    return data, view_count


def exists(content_type, item_id):
    """ true when a record's object is present """
    try:
        get_s3_client().head_object(
            Bucket=_require_bucket(), Key=build_key(content_type, item_id)
        )
    except ClientError as err:
        if _is_missing(err):
            return False
        raise
    return True


def list_prefix(content_type):
    """ every object key under a content type's prefix

    Pages through the list so a prefix with more than one thousand objects is
    fully enumerated rather than silently truncated.
    """
    client = get_s3_client()
    bucket = _require_bucket()
    prefix = PREFIXES[content_type]
    keys = []
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = client.list_objects_v2(**kwargs)
        for item in page.get("Contents", []):
            keys.append(item["Key"])
        if not page.get("IsTruncated"):
            break
        token = page.get("NextContinuationToken")
    return keys


def load_all(content_type):
    """ read every record under a content type, returns [(id, data, view_count)]

    The reusable "read the whole prefix" primitive behind list, the featured
    active-window query, and the slug-uniqueness check. Slug and sort keys live
    inside the JSON bodies, so there is no cheaper way to read them than to fetch
    each object.
    """
    records = []
    for key in list_prefix(content_type):
        item_id = parse_key_id(key)
        data, view_count = get_object(content_type, item_id)
        # A key listed but deleted between list and get simply drops out.
        if data is not None:
            records.append((item_id, data, view_count))
    return records


def delete_object(content_type, item_id):
    """ remove a record's object """
    get_s3_client().delete_object(
        Bucket=_require_bucket(), Key=build_key(content_type, item_id)
    )


def increment_view_count(content_type, item_id):
    """ bump a record's view count in object metadata, returns the new count

    Reads the current count from metadata and copies the object onto itself with
    the incremented value. copy_object with MetadataDirective='REPLACE' drops any
    metadata not re-sent and can reset the content type, so the full metadata dict
    and ContentType are re-supplied. Not atomic: two simultaneous bumps can lose a
    count, which is acceptable for a blog view counter.
    """
    bucket = _require_bucket()
    key = build_key(content_type, item_id)
    client = get_s3_client()
    try:
        head = client.head_object(Bucket=bucket, Key=key)
    except ClientError as err:
        if _is_missing(err):
            raise NotFound(key)
        raise

    metadata = head.get("Metadata", {})
    new_count = _next_view_count(metadata.get(VIEW_COUNT_META_KEY))
    metadata[VIEW_COUNT_META_KEY] = str(new_count)

    client.copy_object(
        Bucket=bucket,
        Key=key,
        CopySource={"Bucket": bucket, "Key": key},
        Metadata=metadata,
        MetadataDirective="REPLACE",
        ContentType=_JSON_CONTENT_TYPE,
    )
    return new_count
