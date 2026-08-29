""" Shared helpers for the lytebuy blog handlers

Auth is not handled here. API Gateway enforces the x-api-key header from the
usage plan on every route marked `private: true` in serverless.yml, so a
request that reaches a handler has already been authorised.

Storage lives in src/storage.py (S3). This module holds the storage-agnostic
pieces: validation, serialization, response building, and shared constants.
"""
import json
import logging
import os
from datetime import datetime, timezone

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

# content_type enum -> S3 key prefix. The route path segment ("posts",
# "press-releases", "featured") and the content_type enum differ, so the mapping
# is explicit rather than derived. Every record lives at PREFIXES[type] + id + ".json".
PREFIXES = {
    CONTENT_TYPE_POST: "posts/",
    CONTENT_TYPE_PRESS_RELEASE: "press-releases/",
    CONTENT_TYPE_FEATURED_VENDOR: "featured/",
}

# The view counter lives in S3 object metadata, not the JSON body, so bumping it
# never rewrites the article. Stored as x-amz-meta-view-count; S3 lowercases and
# returns it under this bare key.
VIEW_COUNT_META_KEY = "view-count"

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


def format_timestamp(value):
    """ render a stored datetime as an unambiguous utc iso string

    Dates are stored in S3 as ISO strings, so a string passes straight through.
    A datetime is normalised to utc first: left naive, the browser's Date parser
    reads a naive iso string as local time and shifts every post by the viewer's
    utc offset.
    """
    if not isinstance(value, datetime):
        return value
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
    """ convert a stored record into the api representation

    The record is the JSON object body merged with its id and view count:
    {**data, "_id": id, "views": n}. Building that shape lets the serializer
    stay identical to the mongo-era one, so the api response never changed.
    """
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
    """ convert a featured-vendor record into the api representation """
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
