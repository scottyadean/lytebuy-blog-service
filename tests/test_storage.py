""" Unit tests for the S3 storage layer, backed by moto's in-process S3 """
import pytest

from src import storage
from src.utils import CONTENT_TYPE_POST, CONTENT_TYPE_PRESS_RELEASE, VIEW_COUNT_META_KEY

# The autouse s3_bucket fixture (tests/conftest.py) creates and points storage at
# this bucket. Its name is duplicated here only for the direct-inspection tests.
BUCKET = "test-blog"


# id helpers


def test_new_id_is_a_valid_unique_id():
    first = storage.new_id()
    second = storage.new_id()
    assert first != second
    assert storage.is_valid_id(first)
    assert storage.is_valid_id(second)


@pytest.mark.parametrize("bad", [None, "", "nope", "../secret", "posts/x", "0" * 24, 123])
def test_is_valid_id_rejects_non_uuids(bad):
    assert storage.is_valid_id(bad) is False


def test_is_valid_id_accepts_a_canonical_uuid():
    assert storage.is_valid_id("5f2b1c8e-9d4a-4b2c-9e0f-9a8b7c6d5e4f")


# keys


def test_build_and_parse_key_round_trip():
    item_id = storage.new_id()
    key = storage.build_key(CONTENT_TYPE_POST, item_id)
    assert key == f"posts/{item_id}.json"
    assert storage.parse_key_id(key) == item_id


def test_build_key_uses_the_content_type_prefix():
    item_id = storage.new_id()
    assert storage.build_key(CONTENT_TYPE_PRESS_RELEASE, item_id) == f"press-releases/{item_id}.json"


# the view-count increment calculation


@pytest.mark.parametrize("current,expected", [(None, 1), ("", 1), ("0", 1), ("41", 42)])
def test_next_view_count(current, expected):
    assert storage._next_view_count(current) == expected


# put / get roundtrip


def test_put_and_get_round_trip_including_metadata():
    item_id = storage.new_id()
    data = {"title": "Hello", "slug": "hello", "body": "# hi"}
    storage.put_object(CONTENT_TYPE_POST, item_id, data, view_count=7)

    loaded, views = storage.get_object(CONTENT_TYPE_POST, item_id)
    assert loaded == data
    assert views == 7


def test_get_missing_returns_none():
    data, views = storage.get_object(CONTENT_TYPE_POST, storage.new_id())
    assert data is None
    assert views is None


def test_view_count_lives_in_metadata_not_the_body(s3_bucket):
    item_id = storage.new_id()
    storage.put_object(CONTENT_TYPE_POST, item_id, {"title": "x"}, view_count=3)

    head = s3_bucket.head_object(Bucket=BUCKET, Key=storage.build_key(CONTENT_TYPE_POST, item_id))
    assert head["Metadata"][VIEW_COUNT_META_KEY] == "3"


# exists / delete


def test_exists_and_delete():
    item_id = storage.new_id()
    assert storage.exists(CONTENT_TYPE_POST, item_id) is False

    storage.put_object(CONTENT_TYPE_POST, item_id, {"title": "x"}, view_count=0)
    assert storage.exists(CONTENT_TYPE_POST, item_id) is True

    storage.delete_object(CONTENT_TYPE_POST, item_id)
    assert storage.exists(CONTENT_TYPE_POST, item_id) is False


# list / load_all


def test_list_and_load_all_are_scoped_to_the_prefix():
    post_id = storage.new_id()
    press_id = storage.new_id()
    storage.put_object(CONTENT_TYPE_POST, post_id, {"slug": "a-post"}, view_count=0)
    storage.put_object(CONTENT_TYPE_PRESS_RELEASE, press_id, {"slug": "a-release"}, view_count=0)

    post_keys = storage.list_prefix(CONTENT_TYPE_POST)
    assert post_keys == [f"posts/{post_id}.json"]

    loaded = storage.load_all(CONTENT_TYPE_POST)
    assert loaded == [(post_id, {"slug": "a-post"}, 0)]


def test_list_prefix_pages_past_one_thousand_objects(s3_bucket):
    # list_objects_v2 caps a page at 1000 keys; the continuation loop must gather
    # them all rather than stop at the first page.
    for index in range(1001):
        s3_bucket.put_object(Bucket=BUCKET, Key=f"posts/{index:04d}.json", Body=b"{}")
    assert len(storage.list_prefix(CONTENT_TYPE_POST)) == 1001


# increment_view_count


def test_increment_view_count_bumps_and_preserves_the_body():
    item_id = storage.new_id()
    data = {"title": "Hello", "body": "# untouched"}
    storage.put_object(CONTENT_TYPE_POST, item_id, data, view_count=0)

    assert storage.increment_view_count(CONTENT_TYPE_POST, item_id) == 1
    assert storage.increment_view_count(CONTENT_TYPE_POST, item_id) == 2

    loaded, views = storage.get_object(CONTENT_TYPE_POST, item_id)
    assert loaded == data  # body survived the metadata-only rewrite
    assert views == 2


def test_increment_view_count_raises_not_found_for_a_missing_object():
    with pytest.raises(storage.NotFound):
        storage.increment_view_count(CONTENT_TYPE_POST, storage.new_id())
