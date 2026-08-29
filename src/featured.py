""" Featured-vendor spotlight handlers.

A featured vendor is a JSON object under the featured/ prefix in S3. It holds a
vendor_id (a reference into the main API's vendor table), a subject + body(TEXT),
a media[] list of {type, url}, and a start/end run window. GET /featured returns
the currently-active feature (now within [starts, ends]); the client falls back
to a recently-added vendor when it is empty (see LB-2.4).

Featured records carry no view counter, so there is no view-bump endpoint here.
"""
import logging

from botocore.exceptions import BotoCoreError, ClientError

from src import storage
from src.utils import (
    BODY_MAX,
    CONTENT_TYPE_FEATURED_VENDOR,
    SUBJECT_MAX,
    VENDOR_ID_MAX,
    clean_media,
    clean_text,
    error,
    format_timestamp,
    parse_iso_datetime,
    parse_body,
    response,
    serialize_featured,
    utc_now,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

STORAGE_ERRORS = (ClientError, BotoCoreError)


def _to_document(item_id, data):
    """ assemble the record shape serialize_featured expects (id merged in) """
    return {**data, "_id": item_id}


def _is_active(data, now):
    """ true when now falls within a feature's [starts, ends] run window

    Replaces the mongo query {starts <= now, $or:[ends null, ends >= now]}. starts
    and ends are stored as iso strings, so they are re-parsed to compare against a
    real datetime.
    """
    starts, _ = parse_iso_datetime(data.get("starts"))
    ends, _ = parse_iso_datetime(data.get("ends"))
    if starts is None or starts > now:
        return False
    return ends is None or ends >= now


def _validate_payload(data, partial=False):
    """ validate a create/update body, returns (fields, error_message).

    partial=True (PUT) validates only keys present, so an update never blanks a
    field the caller did not mention. A create requires the core fields. Datetime
    fields are returned as iso strings ready to store in the JSON body.
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
    try:
        records = storage.load_all(CONTENT_TYPE_FEATURED_VENDOR)
    except STORAGE_ERRORS:
        logger.exception("failed to list featured vendors")
        return error(503, "could not reach the storage backend")

    active = [(item_id, data) for item_id, data, _views in records if _is_active(data, now)]
    # Newest start first; id breaks ties, matching the old sort=[(starts,-1),(_id,-1)].
    active.sort(key=lambda record: (record[1].get("starts") or "", record[0]), reverse=True)

    items = [serialize_featured(_to_document(*active[0]))] if active else []
    return response(200, {"items": items, "count": len(items)})


def get_featured(event, _context):
    """ GET /featured/{feature_id} - one feature by id (admin/preview). """
    path_params = event.get("pathParameters") or {}
    feature_id = path_params.get("feature_id")
    if not storage.is_valid_id(feature_id):
        return error(400, "feature_id is not a valid id")

    try:
        data, _views = storage.get_object(CONTENT_TYPE_FEATURED_VENDOR, feature_id)
    except STORAGE_ERRORS:
        logger.exception("failed to fetch featured vendor")
        return error(503, "could not reach the storage backend")

    if data is None:
        return error(404, "featured vendor not found")
    return response(200, serialize_featured(_to_document(feature_id, data)))


def create_featured(event, _context):
    """ POST /featured - create a featured-vendor spotlight. """
    data, parse_error = parse_body(event)
    if parse_error:
        return error(400, parse_error)

    fields, validation_error = _validate_payload(data)
    if validation_error:
        return error(400, validation_error)

    now = utc_now()
    # A feature runs from now unless a future start was given. Dates go into the
    # body as iso strings.
    record = {
        **fields,
        "content_type": CONTENT_TYPE_FEATURED_VENDOR,
        "starts": format_timestamp(fields.get("starts") or now),
        "ends": format_timestamp(fields.get("ends")),
        "created_date": format_timestamp(now),
        "updated_date": format_timestamp(now),
    }

    try:
        feature_id = storage.new_id()
        storage.put_object(CONTENT_TYPE_FEATURED_VENDOR, feature_id, record, view_count=0)
    except STORAGE_ERRORS:
        logger.exception("failed to insert featured vendor")
        return error(503, "could not reach the storage backend")

    return response(201, serialize_featured(_to_document(feature_id, record)))


def update_featured(event, _context):
    """ PUT /featured/{feature_id} - update a spotlight (partial). """
    path_params = event.get("pathParameters") or {}
    feature_id = path_params.get("feature_id")
    if not storage.is_valid_id(feature_id):
        return error(400, "feature_id is not a valid id")

    data, parse_error = parse_body(event)
    if parse_error:
        return error(400, parse_error)

    fields, validation_error = _validate_payload(data, partial=True)
    if validation_error:
        return error(400, validation_error)
    if not fields:
        return error(400, "no updatable fields were supplied")

    try:
        existing, views = storage.get_object(CONTENT_TYPE_FEATURED_VENDOR, feature_id)
        if existing is None:
            return error(404, "featured vendor not found")

        # Datetime fields validate to datetimes; render them back to iso strings
        # before merging so the stored body stays all-strings.
        merged = dict(fields)
        for key in ("starts", "ends"):
            if key in merged:
                merged[key] = format_timestamp(merged[key])
        record = {**existing, **merged, "updated_date": format_timestamp(utc_now())}
        storage.put_object(CONTENT_TYPE_FEATURED_VENDOR, feature_id, record, view_count=views)
    except STORAGE_ERRORS:
        logger.exception("failed to update featured vendor")
        return error(503, "could not reach the storage backend")

    return response(200, serialize_featured(_to_document(feature_id, record)))
