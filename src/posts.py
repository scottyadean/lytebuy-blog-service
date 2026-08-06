""" Blog post and press release handlers

Posts and press releases share one collection and one document shape; the
`content_type` enum is the only thing that separates them. Each handler is
bound to a content type by the route it is registered under, so a press
release can never be created or read through a /posts route and vice versa.
"""
import logging

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

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
    get_posts_collection,
    parse_body,
    response,
    serialize_post,
    serialize_summary,
    to_object_id,
    utc_now,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)


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
        collection = get_posts_collection()
        query = {"content_type": content_type}
        # _id breaks ties: two items can land in the same millisecond, and
        # sorting on created_date alone leaves their order undefined, which
        # makes paging skip or repeat rows.
        cursor = (
            collection.find(query)
            .sort([("created_date", -1), ("_id", -1)])
            .skip(offset)
            .limit(limit)
        )
        items = [serialize_summary(document) for document in cursor]
        total = collection.count_documents(query)
    except PyMongoError:
        logger.exception("failed to list %s", content_type)
        return error(503, "could not reach the database")

    return response(200, {
        "items": items,
        "count": len(items),
        "total": total,
        "limit": limit,
        "offset": offset,
    })


def _get(event, content_type):
    """ fetch a single item and count the view """
    path_params = event.get("pathParameters") or {}
    post_id = to_object_id(path_params.get("post_id"))
    if post_id is None:
        return error(400, "post_id is not a valid id")

    try:
        # Counting the view in the same round trip as the read keeps the two
        # from drifting apart and avoids a second call on every page load.
        # $inc creates the field when a pre-counter document lacks it.
        document = get_posts_collection().find_one_and_update(
            {"_id": post_id, "content_type": content_type},
            {"$inc": {"views": 1}},
            return_document=ReturnDocument.AFTER,
        )
    except PyMongoError:
        logger.exception("failed to fetch %s", content_type)
        return error(503, "could not reach the database")

    if document is None:
        return error(404, f"{content_type} not found")

    return response(200, serialize_post(document))


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


def _create(event, content_type):
    """ create an item of one content type """
    data, parse_error = parse_body(event)
    if parse_error:
        return error(400, parse_error)

    fields, validation_error = _validate_payload(data)
    if validation_error:
        return error(400, validation_error)

    now = utc_now()
    document = {
        **fields,
        "content_type": content_type,
        "views": 0,
        "created_date": now,
        "updated_date": now,
    }

    try:
        result = get_posts_collection().insert_one(document)
    except DuplicateKeyError:
        return error(409, "slug is already in use")
    except PyMongoError:
        logger.exception("failed to insert %s", content_type)
        return error(503, "could not reach the database")

    document["_id"] = result.inserted_id
    return response(201, serialize_post(document))


def _update(event, content_type):
    """ update an item of one content type """
    path_params = event.get("pathParameters") or {}
    post_id = to_object_id(path_params.get("post_id"))
    if post_id is None:
        return error(400, "post_id is not a valid id")

    data, parse_error = parse_body(event)
    if parse_error:
        return error(400, parse_error)

    fields, validation_error = _validate_payload(data, partial=True)
    if validation_error:
        return error(400, validation_error)
    if not fields:
        return error(400, "no updatable fields were supplied")

    # content_type and views are deliberately not updatable: the route owns the
    # former, and letting a caller set the latter would make the counter a lie.
    fields["updated_date"] = utc_now()

    try:
        document = get_posts_collection().find_one_and_update(
            {"_id": post_id, "content_type": content_type},
            {"$set": fields},
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        return error(409, "slug is already in use")
    except PyMongoError:
        logger.exception("failed to update %s", content_type)
        return error(503, "could not reach the database")

    if document is None:
        return error(404, f"{content_type} not found")

    return response(200, serialize_post(document))


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
