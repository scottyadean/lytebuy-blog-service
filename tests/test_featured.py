""" Featured-vendor handler tests, backed by an in-memory mongo """
import json
from datetime import timedelta

import mongomock
import pytest

from src import featured, utils


@pytest.fixture(autouse=True)
def collection(monkeypatch):
    """ point the handlers at an in-memory collection """
    client = mongomock.MongoClient()
    posts_collection = client["blog"]["posts"]
    monkeypatch.setattr(utils, "get_posts_collection", lambda: posts_collection)
    monkeypatch.setattr(featured, "get_posts_collection", lambda: posts_collection)
    return posts_collection


def body(result):
    return json.loads(result["body"])


def create_event(**overrides):
    payload = {
        "vendor_id": "vendor-123",
        "subject": "Meet the maker",
        "body": "Handcrafted oak bowls, turned in a Placerville workshop.",
    }
    payload.update(overrides)
    return {"body": json.dumps(payload)}


def test_create_requires_core_fields():
    result = featured.create_featured({"body": json.dumps({"subject": "x"})}, None)
    assert result["statusCode"] == 400
    assert "vendor_id" in body(result)["error"]


def test_create_and_get_roundtrip():
    created = featured.create_featured(create_event(), None)
    assert created["statusCode"] == 201, created
    doc = body(created)
    assert doc["vendor_id"] == "vendor-123"
    assert doc["subject"] == "Meet the maker"
    assert doc["media"] == []
    assert doc["starts"] is not None  # defaulted to now
    assert doc["ends"] is None

    fetched = featured.get_featured({"pathParameters": {"feature_id": doc["id"]}}, None)
    assert fetched["statusCode"] == 200
    assert body(fetched)["subject"] == "Meet the maker"


def test_create_validates_media_items():
    bad = featured.create_featured(
        create_event(media=[{"type": "gif", "url": "http://x/y"}]), None
    )
    assert bad["statusCode"] == 400
    assert "media type" in body(bad)["error"]

    ok = featured.create_featured(
        create_event(
            media=[
                {"type": "image", "url": "https://cdn.x/a.jpg"},
                {"type": "video", "url": "https://cdn.x/b.mp4"},
            ]
        ),
        None,
    )
    assert ok["statusCode"] == 201
    assert len(body(ok)["media"]) == 2


def test_ends_before_starts_rejected():
    now = utils.utc_now()
    result = featured.create_featured(
        create_event(
            starts=(now).isoformat(),
            ends=(now - timedelta(days=1)).isoformat(),
        ),
        None,
    )
    assert result["statusCode"] == 400
    assert "ends must be on or after starts" in body(result)["error"]


def test_list_returns_only_the_active_feature():
    now = utils.utc_now()
    # Expired feature.
    featured.create_featured(
        create_event(
            subject="Old",
            starts=(now - timedelta(days=10)).isoformat(),
            ends=(now - timedelta(days=5)).isoformat(),
        ),
        None,
    )
    # Future feature (not started).
    featured.create_featured(
        create_event(subject="Future", starts=(now + timedelta(days=5)).isoformat()),
        None,
    )
    # Currently active (open-ended).
    featured.create_featured(
        create_event(subject="Active now", starts=(now - timedelta(hours=1)).isoformat()),
        None,
    )

    result = featured.list_featured({}, None)
    assert result["statusCode"] == 200
    payload = body(result)
    assert payload["count"] == 1
    assert payload["items"][0]["subject"] == "Active now"


def test_list_empty_when_nothing_active():
    now = utils.utc_now()
    featured.create_featured(
        create_event(starts=(now + timedelta(days=1)).isoformat()), None
    )
    result = featured.list_featured({}, None)
    assert body(result) == {"items": [], "count": 0}


def test_update_partial_does_not_blank_other_fields():
    created = body(featured.create_featured(create_event(), None))
    updated = featured.update_featured(
        {
            "pathParameters": {"feature_id": created["id"]},
            "body": json.dumps({"subject": "New subject"}),
        },
        None,
    )
    assert updated["statusCode"] == 200
    doc = body(updated)
    assert doc["subject"] == "New subject"
    assert doc["vendor_id"] == "vendor-123"  # untouched
    assert doc["body"] == created["body"]


def test_get_missing_is_404():
    result = featured.get_featured(
        {"pathParameters": {"feature_id": "0" * 24}}, None
    )
    assert result["statusCode"] == 404
