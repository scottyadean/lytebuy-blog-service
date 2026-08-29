""" Blog post and press release handlers

Posts and press releases share one document shape; the `content_type` enum is the
only thing that separates them, and each is stored under its own S3 key prefix.
Each handler is bound to a content type by the route it is registered under, so a
press release can never be created or read through a /posts route and vice versa -
the prefix in the key makes cross-type access impossible, not just forbidden.

Records are JSON objects in S3. Every field lives in the object body except the
view counter, which lives in object metadata; a single-item GET no longer counts a
view. Instead the host app calls POST /{type}/{id}/views after the page loads.
"""
import logging

from botocore.exceptions import BotoCoreError, ClientError

from src import storage
from src.utils import (
    AUTHOR_MAX,
    BODY_MAX,
    CONTENT_TYPE_POST,
    CONTENT_TYPE_PRESS_RELEASE,
    DEFAULT_LIMIT,
    EXCERPT_MAX,
    IMAGE_URL_MAX,
    MAX_LIMIT,
    TITLE_MAX,
    clean_optional_text,
    clean_slug,
    clean_tags,
    clean_text,
    error,
    format_timestamp,
    parse_body,
    response,
    serialize_post,
    serialize_summary,
    utc_now,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# S3 errors surface as one of these; a handler turns them into a 503 the same way
# the mongo-era code turned a PyMongoError into one.
STORAGE_ERRORS = (ClientError, BotoCoreError)


def _to_document(item_id, data, views):
    """ assemble the mongo-shaped record the serializers expect

    The serializers read `_id` and `views`; the S3 body carries neither (id is the
    key, views is metadata), so they are merged back in here. Keeping this shape
    means the serializers - and therefore the api responses - never changed.
    """
    return {**data, "_id": item_id, "views": views}


def _list(event, content_type):
    """ list published items of one content type, newest first """
    params = event.get("queryStringParameters") or {}

    try:
        limit = int(params.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        return error(400, "limit must be an integer")
    if limit < 1:
        return error(400, "limit must be at least 1")
    limit = min(limit, MAX_LIMIT)

    try:
        offset = int(params.get("offset", 0))
    except (TypeError, ValueError):
        return error(400, "offset must be an integer")
    if offset < 0:
        return error(400, "offset must be zero or greater")

    try:
        records = storage.load_all(content_type)
    except STORAGE_ERRORS:
        logger.exception("failed to list %s", content_type)
        return error(503, "could not reach the storage backend")

    # Newest first. created_date is a millisecond-precision utc iso string, so a
    # string sort is chronological; the id breaks ties for two items written in
    # the same millisecond, matching the old (created_date, _id) index order.
    records.sort(key=lambda record: (record[1].get("created_date") or "", record[0]), reverse=True)

    total = len(records)
    page = records[offset:offset + limit]
    items = [serialize_summary(_to_document(item_id, data, views)) for item_id, data, views in page]

    return response(200, {
        "items": items,
        "count": len(items),
        "total": total,
        "limit": limit,
        "offset": offset,
    })


def _get(event, content_type):
    """ fetch a single item

    No longer counts a view: the read path is decoupled from the counter, which
    the host app bumps via POST /{type}/{id}/views after the page loads.
    """
    path_params = event.get("pathParameters") or {}
    item_id = path_params.get("post_id")
    if not storage.is_valid_id(item_id):
        return error(400, "post_id is not a valid id")

    try:
        data, views = storage.get_object(content_type, item_id)
    except STORAGE_ERRORS:
        logger.exception("failed to fetch %s", content_type)
        return error(503, "could not reach the storage backend")

    if data is None:
        return error(404, f"{content_type} not found")

    return response(200, serialize_post(_to_document(item_id, data, views)))


def _validate_payload(data, partial=False):
    """ validate a create or update body, returns (fields, error_message)

    With partial=True only the keys actually present are validated and
    returned, so a PUT never blanks a field the caller did not mention.
    """
    fields = {}

    def present(key):
        return not partial or key in data

    if present("title"):
        title, message = clean_text(data.get("title"), TITLE_MAX)
        if message:
            return None, f"title: {message}"
        fields["title"] = title

    if present("slug"):
        slug, message = clean_slug(data.get("slug"))
        if message:
            return None, f"slug: {message}"
        fields["slug"] = slug

    if present("body"):
        body, message = clean_text(data.get("body"), BODY_MAX)
        if message:
            return None, f"body: {message}"
        fields["body"] = body

    if present("author"):
        author, message = clean_text(data.get("author"), AUTHOR_MAX)
        if message:
            return None, f"author: {message}"
        fields["author"] = author

    # Optional fields: an explicit null clears them.
    if not partial or "excerpt" in data:
        excerpt, message = clean_optional_text(data.get("excerpt"), EXCERPT_MAX)
        if message:
            return None, f"excerpt: {message}"
        fields["excerpt"] = excerpt

    if not partial or "cover_image_url" in data:
        cover, message = clean_optional_text(data.get("cover_image_url"), IMAGE_URL_MAX)
        if message:
            return None, f"cover_image_url: {message}"
        fields["cover_image_url"] = cover

    if not partial or "tags" in data:
        tags, message = clean_tags(data.get("tags"))
        if message:
            return None, f"tags: {message}"
        fields["tags"] = tags

    return fields, None


def _reject_duplicate_slug(content_type, slug, exclude_id=None):
    """ return a 409 response if another record already uses slug, else None

    The mongo unique index is gone, so uniqueness is enforced on write by reading
    the prefix. exclude_id lets an update keep its own slug. There is a tiny race
    window between two simultaneous creates of the same slug, accepted for an
    admin-driven blog.
    """
    for item_id, data, _views in storage.load_all(content_type):
        if item_id == exclude_id:
            continue
        if data.get("slug") == slug:
            return error(409, "slug is already in use")
    return None


def _create(event, content_type):
    """ create an item of one content type """
    data, parse_error = parse_body(event)
    if parse_error:
        return error(400, parse_error)

    fields, validation_error = _validate_payload(data)
    if validation_error:
        return error(400, validation_error)

    try:
        duplicate = _reject_duplicate_slug(content_type, fields["slug"])
        if duplicate:
            return duplicate

        now = format_timestamp(utc_now())
        record = {
            **fields,
            "content_type": content_type,
            "created_date": now,
            "updated_date": now,
        }
        item_id = storage.new_id()
        # views is not in the body; it starts at 0 in object metadata.
        storage.put_object(content_type, item_id, record, view_count=0)
    except STORAGE_ERRORS:
        logger.exception("failed to insert %s", content_type)
        return error(503, "could not reach the storage backend")

    return response(201, serialize_post(_to_document(item_id, record, 0)))


def _update(event, content_type):
    """ update an item of one content type """
    path_params = event.get("pathParameters") or {}
    item_id = path_params.get("post_id")
    if not storage.is_valid_id(item_id):
        return error(400, "post_id is not a valid id")

    data, parse_error = parse_body(event)
    if parse_error:
        return error(400, parse_error)

    fields, validation_error = _validate_payload(data, partial=True)
    if validation_error:
        return error(400, validation_error)
    if not fields:
        return error(400, "no updatable fields were supplied")

    try:
        existing, views = storage.get_object(content_type, item_id)
        if existing is None:
            return error(404, f"{content_type} not found")

        if "slug" in fields and fields["slug"] != existing.get("slug"):
            duplicate = _reject_duplicate_slug(content_type, fields["slug"], exclude_id=item_id)
            if duplicate:
                return duplicate

        # content_type and views are deliberately not updatable: the route owns
        # the former, and views lives in metadata so a body rewrite never touches
        # it. Re-put with the existing count to preserve it.
        record = {**existing, **fields, "updated_date": format_timestamp(utc_now())}
        storage.put_object(content_type, item_id, record, view_count=views)
    except STORAGE_ERRORS:
        logger.exception("failed to update %s", content_type)
        return error(503, "could not reach the storage backend")

    return response(200, serialize_post(_to_document(item_id, record, views)))


def _bump_views(event, content_type):
    """ increment a record's view counter, returns the new count

    Public and separate from the read so the host app can call it after a page
    loads without slowing the read or rewriting the article body.
    """
    path_params = event.get("pathParameters") or {}
    item_id = path_params.get("post_id")
    if not storage.is_valid_id(item_id):
        return error(400, "post_id is not a valid id")

    try:
        new_count = storage.increment_view_count(content_type, item_id)
    except storage.NotFound:
        return error(404, f"{content_type} not found")
    except STORAGE_ERRORS:
        logger.exception("failed to bump views for %s", content_type)
        return error(503, "could not reach the storage backend")

    return response(200, {"views": new_count})


# Posts

def list_posts(event, _context):
    """ GET /posts - public """
    return _list(event, CONTENT_TYPE_POST)


def get_post(event, _context):
    """ GET /posts/{post_id} """
    return _get(event, CONTENT_TYPE_POST)


def create_post(event, _context):
    """ POST /posts """
    return _create(event, CONTENT_TYPE_POST)


def update_post(event, _context):
    """ PUT /posts/{post_id} """
    return _update(event, CONTENT_TYPE_POST)


def bump_post_views(event, _context):
    """ POST /posts/{post_id}/views - public """
    return _bump_views(event, CONTENT_TYPE_POST)


# Press releases

def list_press_releases(event, _context):
    """ GET /press-releases - public """
    return _list(event, CONTENT_TYPE_PRESS_RELEASE)


def get_press_release(event, _context):
    """ GET /press-releases/{post_id} """
    return _get(event, CONTENT_TYPE_PRESS_RELEASE)


def create_press_release(event, _context):
    """ POST /press-releases """
    return _create(event, CONTENT_TYPE_PRESS_RELEASE)


def update_press_release(event, _context):
    """ PUT /press-releases/{post_id} """
    return _update(event, CONTENT_TYPE_PRESS_RELEASE)


def bump_press_release_views(event, _context):
    """ POST /press-releases/{post_id}/views - public """
    return _bump_views(event, CONTENT_TYPE_PRESS_RELEASE)
