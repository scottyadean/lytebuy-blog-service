""" Shared helpers for the lytebuy blog handlers

Auth is not handled here. API Gateway enforces the x-api-key header from the
usage plan on every route marked `private: true` in serverless.yml, so a
request that reaches a handler has already been authorised.
"""
import json
import logging
import os
from datetime import datetime, timezone

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import MongoClient
from pymongo.server_api import ServerApi

DB_NAME = os.getenv("DB_NAME", "lytebuy-development")


# Round brackets, not braces: AWS Param Store rejects any value containing
# "{{}}", reading it as a nested parameter reference.
DB_NAME_PLACEHOLDER = "((db_name))"


def resolve_uri(uri, db_name):
    """ substitute the ((db_name)) placeholder in a connection string

    The Atlas connection string is shared across stages and carries the
    database name in its path. Templating it means one stored secret works for
    every stage and the stage's own DB_NAME decides which database it actually
    opens, so a production deploy can never be pointed at the development data
    by a stale copied URI.
    """
    if not uri:
        return uri
    return uri.replace(DB_NAME_PLACEHOLDER, db_name)


URI = resolve_uri(os.getenv("DB_URL"), DB_NAME)
COLLECTION = "posts"
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "*")

# Content types. One collection, one shape; the enum is what separates an
# editorial post from a press release, so both share every read/write path.
CONTENT_TYPE_POST = "post"
CONTENT_TYPE_PRESS_RELEASE = "press_release"
# A featured-vendor spotlight: shares the collection with posts, distinguished by
# content_type. Carries a vendor_id (a reference into the main API's vendor table),
# a subject/body, a media list, and a start/end run window.
CONTENT_TYPE_FEATURED_VENDOR = "featured_vendor"
CONTENT_TYPES = (CONTENT_TYPE_POST, CONTENT_TYPE_PRESS_RELEASE)

TITLE_MAX = 200
SLUG_MAX = 220
EXCERPT_MAX = 400
BODY_MAX = 100_000
AUTHOR_MAX = 120
IMAGE_URL_MAX = 2000
TAG_MAX = 40
TAGS_MAX = 12

# Featured-vendor limits.
SUBJECT_MAX = 200
VENDOR_ID_MAX = 64
MEDIA_MAX = 20  # max media items on one feature
MEDIA_TYPES = ("image", "video")

DEFAULT_LIMIT = 100
MAX_LIMIT = 100

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Cached across warm lambda invocations. Building a MongoClient per request
# opens a new connection pool every time and exhausts the Atlas connection
# limit under any real traffic.
_client = None
_indexes_ready = False


def get_client():
    """ get a cached mongo client """
    global _client
    if _client is None:
        if not URI:
            raise RuntimeError("DB_URL is not set")
        _client = MongoClient(
            URI,
            server_api=ServerApi("1"),
            serverSelectionTimeoutMS=5000,
            connectTimeoutMS=5000,
        )
    return _client


def get_db():
    """ get the blog database """
    return get_client()[DB_NAME]


def get_posts_collection():
    """ get the posts collection, shared by posts and press releases """
    collection = get_db()[COLLECTION]
    _ensure_indexes(collection)
    return collection


def _ensure_indexes(collection):
    """ create the collection's indexes once per warm container

    create_index is idempotent, but it is still a round trip, so the flag keeps
    it off the hot path for every request after the first. The unique index is
    what makes the DuplicateKeyError handling in the write handlers real: without
    it, two posts could share a slug and the marketing site's /blog/<slug> route
    would resolve to whichever one mongo returned first.
    """
    global _indexes_ready
    if _indexes_ready:
        return
    # Compound so a post and a press release may share a slug; they live under
    # different routes on the site and never collide.
    collection.create_index([("content_type", 1), ("slug", 1)], unique=True, name="uq_type_slug")
    # Matches the list query's filter + sort exactly.
    collection.create_index(
        [("content_type", 1), ("created_date", -1), ("_id", -1)], name="ix_type_created"
    )
    _indexes_ready = True


def default_headers():
    """ standard crud api response headers """
    return {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
        "Access-Control-Allow-Credentials": True,
        "Access-Control-Allow-Methods": "OPTIONS,POST,GET,PUT,DELETE",
        "Access-Control-Allow-Headers": "Content-Type,X-Amz-Date,Authorization,X-Api-Key,"
                                        "X-Api-Token,X-Amz-Security-Token,X-Amz-User-Agent",
    }


def response(status_code, body):
    """ build an api gateway proxy response """
    return {
        "statusCode": status_code,
        "headers": default_headers(),
        "body": json.dumps(body, default=str),
    }


def error(status_code, message):
    """ build an error response """
    return response(status_code, {"error": message})


def parse_body(event):
    """ parse a json request body, returns (data, error_message) """
    raw = event.get("body")
    if not raw:
        return None, "request body is required"
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None, "request body must be valid json"
    if not isinstance(data, dict):
        return None, "request body must be a json object"
    return data, None


def clean_text(value, max_length):
    """ coerce a submitted field to a trimmed string, returns (value, error_message) """
    if value is None:
        return None, "field is required"
    if not isinstance(value, str):
        return None, "field must be a string"
    value = value.strip()
    if not value:
        return None, "field cannot be empty"
    if len(value) > max_length:
        return None, f"field cannot be longer than {max_length} characters"
    return value, None


def clean_optional_text(value, max_length):
    """ same as clean_text but an explicit null clears the field """
    if value is None:
        return None, None
    return clean_text(value, max_length)


def clean_tags(value):
    """ validate a list of tag strings, returns (tags, error_message) """
    if value is None:
        return [], None
    if not isinstance(value, list):
        return None, "field must be a list of strings"
    if len(value) > TAGS_MAX:
        return None, f"field cannot hold more than {TAGS_MAX} tags"
    tags = []
    for item in value:
        tag, message = clean_text(item, TAG_MAX)
        if message:
            return None, message
        tags.append(tag)
    return tags, None


def clean_slug(value):
    """ validate a url slug, returns (slug, error_message) """
    slug, message = clean_text(value, SLUG_MAX)
    if message:
        return None, message
    slug = slug.lower()
    # Anything outside this set either breaks the marketing site's routing or
    # silently changes once a browser normalises the URL.
    if not all(char.isalnum() or char == "-" for char in slug):
        return None, "field may only contain letters, numbers and hyphens"
    if slug.startswith("-") or slug.endswith("-") or "--" in slug:
        return None, "field may not start, end or double up on hyphens"
    return slug, None


def clean_media(value):
    """ validate a list of {type, url} media items, returns (media, error_message).

    None or an omitted field yields an empty list. Each item's type must be one
    of MEDIA_TYPES (image/video) and url a non-empty string within IMAGE_URL_MAX.
    """
    if value is None:
        return [], None
    if not isinstance(value, list):
        return None, "field must be a list of media items"
    if len(value) > MEDIA_MAX:
        return None, f"field cannot hold more than {MEDIA_MAX} media items"
    media = []
    for item in value:
        if not isinstance(item, dict):
            return None, "each media item must be an object"
        mtype = item.get("type")
        if mtype not in MEDIA_TYPES:
            return None, f"media type must be one of {', '.join(MEDIA_TYPES)}"
        url, message = clean_text(item.get("url"), IMAGE_URL_MAX)
        if message:
            return None, f"media url: {message}"
        media.append({"type": mtype, "url": url})
    return media, None


def parse_iso_datetime(value):
    """ parse an ISO 8601 string to a tz-aware utc datetime, returns (dt, error).

    None yields (None, None) so callers can treat the field as optional/clearable.
    A naive string is assumed to be utc.
    """
    if value is None:
        return None, None
    if not isinstance(value, str):
        return None, "must be an ISO 8601 date string"
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None, "must be a valid ISO 8601 date"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc), None


def to_object_id(value):
    """ convert a path param to an ObjectId, returns None when malformed """
    # ObjectId(None) mints a brand new random id rather than raising, so a
    # missing path param has to be rejected before it reaches the constructor.
    if not value:
        return None
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        return None


def format_timestamp(value):
    """ render a stored datetime as an unambiguous utc iso string """
    if not isinstance(value, datetime):
        return value
    # Mongo stores datetimes as utc but hands them back naive. Left alone, the
    # browser's Date parser reads a naive iso string as local time and shifts
    # every post by the viewer's utc offset.
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def utc_now():
    """ current time as a timezone aware utc datetime """
    # Truncated to milliseconds to match what BSON can actually store, so the
    # timestamp returned by a create matches the one a later read returns.
    now = datetime.now(timezone.utc)
    return now.replace(microsecond=(now.microsecond // 1000) * 1000)


def serialize_post(document):
    """ convert a mongo document into the api representation """
    return {
        "id": str(document["_id"]),
        "content_type": document.get("content_type"),
        "title": document.get("title"),
        "slug": document.get("slug"),
        "excerpt": document.get("excerpt"),
        "body": document.get("body"),
        "author": document.get("author"),
        "cover_image_url": document.get("cover_image_url"),
        "tags": document.get("tags") or [],
        # Posts written before the counter existed have no views field at all.
        "views": document.get("views") or 0,
        "created_date": format_timestamp(document.get("created_date")),
        "updated_date": format_timestamp(document.get("updated_date")),
    }


def serialize_summary(document):
    """ list representation, without the full article body """
    summary = serialize_post(document)
    summary.pop("body", None)
    return summary


def serialize_featured(document):
    """ convert a featured-vendor document into the api representation """
    return {
        "id": str(document["_id"]),
        "content_type": document.get("content_type"),
        "vendor_id": document.get("vendor_id"),
        "subject": document.get("subject"),
        "body": document.get("body"),
        "media": document.get("media") or [],
        "starts": format_timestamp(document.get("starts")),
        "ends": format_timestamp(document.get("ends")),
        "created_date": format_timestamp(document.get("created_date")),
        "updated_date": format_timestamp(document.get("updated_date")),
    }
