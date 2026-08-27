""" Featured-vendor spotlight handlers.

A featured vendor shares the posts collection (distinguished by content_type =
featured_vendor). It holds a vendor_id (a reference into the main API's vendor
table), a subject + body(TEXT), a media[] list of {type, url}, and a start/end
run window. GET /featured returns the currently-active feature (now within
[starts, ends]); the client falls back to a recently-added vendor when it is
empty (see LB-2.4).
"""
import logging

from pymongo import ReturnDocument
from pymongo.errors import PyMongoError

from src.utils import (
    BODY_MAX,
    CONTENT_TYPE_FEATURED_VENDOR,
    SUBJECT_MAX,
    VENDOR_ID_MAX,
    clean_media,
    clean_text,
    error,
    get_posts_collection,
    parse_body,
    parse_iso_datetime,
    response,
    serialize_featured,
    to_object_id,
    utc_now,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _validate_payload(data, partial=False):
    """ validate a create/update body, returns (fields, error_message).

    partial=True (PUT) validates only keys present, so an update never blanks a
    field the caller did not mention. A create requires the core fields.
    """
    fields = {}

    def present(key):
        return not partial or key in data

    if present("vendor_id"):
        vendor_id, message = clean_text(data.get("vendor_id"), VENDOR_ID_MAX)
        if message:
            return None, f"vendor_id: {message}"
        fields["vendor_id"] = vendor_id

    if present("subject"):
        subject, message = clean_text(data.get("subject"), SUBJECT_MAX)
        if message:
            return None, f"subject: {message}"
        fields["subject"] = subject

    if present("body"):
        body, message = clean_text(data.get("body"), BODY_MAX)
        if message:
            return None, f"body: {message}"
        fields["body"] = body

    # Optional media list; an explicit null (or omission on create) is an empty list.
    if not partial or "media" in data:
        media, message = clean_media(data.get("media"))
        if message:
            return None, message
        fields["media"] = media

    # Run window. starts defaults to now on create; ends is optional (open-ended).
    if present("starts"):
        starts, message = parse_iso_datetime(data.get("starts"))
        if message:
            return None, f"starts: {message}"
        fields["starts"] = starts

    if not partial or "ends" in data:
        ends, message = parse_iso_datetime(data.get("ends"))
        if message:
            return None, f"ends: {message}"
        fields["ends"] = ends

    if fields.get("starts") and fields.get("ends") and fields["ends"] < fields["starts"]:
        return None, "ends must be on or after starts"

    return fields, None


def list_featured(event, _context):
    """ GET /featured - the currently-active feature (now within [starts, ends]).

    Returns the newest active feature so a single spotlight is deterministic even
    if several overlap. `items` is empty when nothing is active, which the client
    treats as "fall back to a recently-added vendor".
    """
    now = utc_now()
    query = {
        "content_type": CONTENT_TYPE_FEATURED_VENDOR,
        "starts": {"$lte": now},
        "$or": [{"ends": None}, {"ends": {"$gte": now}}],
    }
    try:
        collection = get_posts_collection()
        document = collection.find_one(query, sort=[("starts", -1), ("_id", -1)])
    except PyMongoError:
        logger.exception("failed to list featured vendors")
        return error(503, "could not reach the database")

    items = [serialize_featured(document)] if document else []
    return response(200, {"items": items, "count": len(items)})


def get_featured(event, _context):
    """ GET /featured/{feature_id} - one feature by id (admin/preview). """
    path_params = event.get("pathParameters") or {}
    feature_id = to_object_id(path_params.get("feature_id"))
    if feature_id is None:
        return error(400, "feature_id is not a valid id")

    try:
        document = get_posts_collection().find_one(
            {"_id": feature_id, "content_type": CONTENT_TYPE_FEATURED_VENDOR}
        )
    except PyMongoError:
        logger.exception("failed to fetch featured vendor")
        return error(503, "could not reach the database")

    if document is None:
        return error(404, "featured vendor not found")
    return response(200, serialize_featured(document))


def create_featured(event, _context):
    """ POST /featured - create a featured-vendor spotlight. """
    data, parse_error = parse_body(event)
    if parse_error:
        return error(400, parse_error)

    fields, validation_error = _validate_payload(data)
    if validation_error:
        return error(400, validation_error)

    now = utc_now()
    document = {
        **fields,
        "content_type": CONTENT_TYPE_FEATURED_VENDOR,
        # A feature runs from now unless a future start was given.
        "starts": fields.get("starts") or now,
        "created_date": now,
        "updated_date": now,
    }

    try:
        result = get_posts_collection().insert_one(document)
    except PyMongoError:
        logger.exception("failed to insert featured vendor")
        return error(503, "could not reach the database")

    document["_id"] = result.inserted_id
    return response(201, serialize_featured(document))


def update_featured(event, _context):
    """ PUT /featured/{feature_id} - update a spotlight (partial). """
    path_params = event.get("pathParameters") or {}
    feature_id = to_object_id(path_params.get("feature_id"))
    if feature_id is None:
        return error(400, "feature_id is not a valid id")

    data, parse_error = parse_body(event)
    if parse_error:
        return error(400, parse_error)

    fields, validation_error = _validate_payload(data, partial=True)
    if validation_error:
        return error(400, validation_error)
    if not fields:
        return error(400, "no updatable fields were supplied")

    fields["updated_date"] = utc_now()
    try:
        collection = get_posts_collection()
        result = collection.find_one_and_update(
            {"_id": feature_id, "content_type": CONTENT_TYPE_FEATURED_VENDOR},
            {"$set": fields},
            return_document=ReturnDocument.AFTER,
        )
    except PyMongoError:
        logger.exception("failed to update featured vendor")
        return error(503, "could not reach the database")

    if result is None:
        return error(404, "featured vendor not found")
    return response(200, serialize_featured(result))
