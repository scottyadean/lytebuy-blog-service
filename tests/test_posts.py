""" Handler tests backed by moto's in-process S3 (see tests/conftest.py) """
import json

import pytest

from src import posts, router, storage

# A valid-but-absent uuid for 404 tests, and a clearly-malformed one for 400s.
MISSING_ID = "5f2b1c8e-9d4a-4b2c-9e0f-9a8b7c6d5e4f"
BAD_ID = "nope"


def body(result):
    return json.loads(result["body"])


def create(title="Main street wins", slug="main-street-wins", **overrides):
    payload = {
        "title": title,
        "slug": slug,
        "body": "Why local beats the marketplace giants.",
        "author": "Scott D",
        **overrides,
    }
    return posts.create_post({"body": json.dumps(payload)}, None)


def create_press(title="Lytebuy launches", slug="lytebuy-launches", **overrides):
    payload = {
        "title": title,
        "slug": slug,
        "body": "For immediate release.",
        "author": "Lytebuy",
        **overrides,
    }
    return posts.create_press_release({"body": json.dumps(payload)}, None)


# create


def test_create_returns_the_stored_post():
    result = create()

    assert result["statusCode"] == 201
    payload = body(result)
    assert payload["title"] == "Main street wins"
    assert payload["slug"] == "main-street-wins"
    assert payload["content_type"] == "post"
    assert payload["views"] == 0
    assert payload["created_date"]
    assert payload["updated_date"]
    assert payload["id"]
    assert len(storage.list_prefix("post")) == 1


def test_create_trims_and_lowercases():
    payload = body(create(title="  Spaced Out  ", slug="Mixed-Case"))
    assert payload["title"] == "Spaced Out"
    assert payload["slug"] == "mixed-case"


def test_create_accepts_optional_fields():
    payload = body(create(excerpt="A short teaser", tags=["local", "commerce"],
                          cover_image_url="https://example.com/a.jpg"))
    assert payload["excerpt"] == "A short teaser"
    assert payload["tags"] == ["local", "commerce"]
    assert payload["cover_image_url"] == "https://example.com/a.jpg"


def test_create_defaults_optional_fields_when_absent():
    payload = body(create())
    assert payload["excerpt"] is None
    assert payload["cover_image_url"] is None
    assert payload["tags"] == []


@pytest.mark.parametrize("field", ["title", "slug", "body", "author"])
def test_create_requires_each_core_field(field):
    payload = {
        "title": "T", "slug": "a-slug", "body": "B", "author": "A",
    }
    del payload[field]
    result = posts.create_post({"body": json.dumps(payload)}, None)
    assert result["statusCode"] == 400
    assert field in body(result)["error"]


@pytest.mark.parametrize(
    "slug", ["has spaces", "trailing-", "-leading", "double--hyphen", "under_score"]
)
def test_create_rejects_malformed_slugs(slug):
    result = create(slug=slug)
    assert result["statusCode"] == 400
    assert "slug" in body(result)["error"]


def test_create_rejects_a_duplicate_slug():
    assert create(slug="repeat-me")["statusCode"] == 201
    result = create(slug="repeat-me")
    assert result["statusCode"] == 409


def test_create_rejects_a_missing_body():
    result = posts.create_post({}, None)
    assert result["statusCode"] == 400


def test_create_rejects_malformed_json():
    result = posts.create_post({"body": "not json"}, None)
    assert result["statusCode"] == 400


# list


def test_list_returns_newest_first():
    create(title="First", slug="first")
    create(title="Second", slug="second")
    create(title="Third", slug="third")

    payload = body(posts.list_posts({}, None))
    assert [item["title"] for item in payload["items"]] == ["Third", "Second", "First"]
    assert payload["count"] == 3
    assert payload["total"] == 3


def test_list_omits_the_article_body():
    create()
    item = body(posts.list_posts({}, None))["items"][0]
    assert "body" not in item
    assert item["title"]


def test_list_paginates_with_limit_and_offset():
    for index in range(5):
        create(title=f"Post {index}", slug=f"post-{index}")

    event = {"queryStringParameters": {"limit": "2", "offset": "2"}}
    payload = body(posts.list_posts(event, None))

    assert [item["title"] for item in payload["items"]] == ["Post 2", "Post 1"]
    assert payload["limit"] == 2
    assert payload["offset"] == 2
    # total is the unpaged count, so the site can render pagination controls
    assert payload["total"] == 5


def test_list_caps_the_limit_at_one_hundred():
    create()
    event = {"queryStringParameters": {"limit": "5000"}}
    assert body(posts.list_posts(event, None))["limit"] == 100


def test_list_defaults_to_one_hundred():
    create()
    assert body(posts.list_posts({}, None))["limit"] == 100


@pytest.mark.parametrize("params,expected", [
    ({"limit": "abc"}, "limit must be an integer"),
    ({"limit": "0"}, "limit must be at least 1"),
    ({"offset": "abc"}, "offset must be an integer"),
    ({"offset": "-1"}, "offset must be zero or greater"),
])
def test_list_rejects_bad_pagination(params, expected):
    result = posts.list_posts({"queryStringParameters": params}, None)
    assert result["statusCode"] == 400
    assert body(result)["error"] == expected


def test_list_is_empty_when_nothing_exists():
    payload = body(posts.list_posts({}, None))
    assert payload["items"] == []
    assert payload["total"] == 0


# get and the view counter


def test_get_returns_the_full_post_and_does_not_count_a_view():
    # The read is decoupled from the counter now; a plain GET never bumps it.
    post_id = body(create())["id"]

    payload = body(posts.get_post({"pathParameters": {"post_id": post_id}}, None))
    assert payload["body"] == "Why local beats the marketplace giants."
    assert payload["views"] == 0

    payload = body(posts.get_post({"pathParameters": {"post_id": post_id}}, None))
    assert payload["views"] == 0


def test_bump_views_increments_the_counter():
    post_id = body(create())["id"]

    first = body(posts.bump_post_views({"pathParameters": {"post_id": post_id}}, None))
    assert first == {"views": 1}
    second = body(posts.bump_post_views({"pathParameters": {"post_id": post_id}}, None))
    assert second == {"views": 2}

    # A later read reflects the bumped count.
    assert body(posts.get_post({"pathParameters": {"post_id": post_id}}, None))["views"] == 2


def test_list_reflects_bumped_views():
    post_id = body(create())["id"]
    posts.bump_post_views({"pathParameters": {"post_id": post_id}}, None)

    assert body(posts.list_posts({}, None))["items"][0]["views"] == 1


def test_bump_views_rejects_a_malformed_id():
    result = posts.bump_post_views({"pathParameters": {"post_id": BAD_ID}}, None)
    assert result["statusCode"] == 400


def test_bump_views_404s_for_an_unknown_id():
    result = posts.bump_post_views({"pathParameters": {"post_id": MISSING_ID}}, None)
    assert result["statusCode"] == 404


def test_a_post_view_cannot_be_bumped_through_the_press_release_route():
    post_id = body(create())["id"]
    result = posts.bump_press_release_views({"pathParameters": {"post_id": post_id}}, None)
    assert result["statusCode"] == 404


def test_get_rejects_a_malformed_id():
    result = posts.get_post({"pathParameters": {"post_id": BAD_ID}}, None)
    assert result["statusCode"] == 400


def test_get_404s_for_an_unknown_id():
    result = posts.get_post({"pathParameters": {"post_id": MISSING_ID}}, None)
    assert result["statusCode"] == 404


# update


def test_update_changes_only_the_supplied_fields():
    created = body(create(excerpt="original teaser"))
    post_id = created["id"]

    result = posts.update_post(
        {"pathParameters": {"post_id": post_id}, "body": json.dumps({"title": "Renamed"})},
        None,
    )

    assert result["statusCode"] == 200
    payload = body(result)
    assert payload["title"] == "Renamed"
    # untouched fields survive a partial update
    assert payload["excerpt"] == "original teaser"
    assert payload["slug"] == created["slug"]
    assert payload["author"] == created["author"]


def test_update_can_clear_an_optional_field():
    post_id = body(create(excerpt="original teaser"))["id"]

    payload = body(posts.update_post(
        {"pathParameters": {"post_id": post_id}, "body": json.dumps({"excerpt": None})},
        None,
    ))
    assert payload["excerpt"] is None


def test_update_does_not_reset_the_view_counter():
    post_id = body(create())["id"]
    posts.bump_post_views({"pathParameters": {"post_id": post_id}}, None)

    payload = body(posts.update_post(
        {"pathParameters": {"post_id": post_id},
         "body": json.dumps({"title": "Renamed", "views": 999})},
        None,
    ))
    # views lives in object metadata, so rewriting the body preserves it; and it
    # is not a caller-writable field, so the 999 is ignored.
    assert payload["views"] == 1


def test_update_rejects_an_empty_payload():
    post_id = body(create())["id"]
    result = posts.update_post(
        {"pathParameters": {"post_id": post_id}, "body": json.dumps({})}, None
    )
    assert result["statusCode"] == 400


def test_update_rejects_a_duplicate_slug():
    create(slug="taken")
    post_id = body(create(slug="mine"))["id"]

    result = posts.update_post(
        {"pathParameters": {"post_id": post_id}, "body": json.dumps({"slug": "taken"})},
        None,
    )
    assert result["statusCode"] == 409


def test_update_404s_for_an_unknown_id():
    result = posts.update_post(
        {"pathParameters": {"post_id": MISSING_ID},
         "body": json.dumps({"title": "Renamed"})},
        None,
    )
    assert result["statusCode"] == 404


# press releases share the model but never the content type


def test_press_releases_use_the_same_shape():
    payload = body(create_press())
    assert payload["content_type"] == "press_release"
    assert payload["views"] == 0
    assert payload["title"] == "Lytebuy launches"


def test_lists_do_not_leak_across_content_types():
    create(slug="a-post")
    create_press(slug="a-release")

    post_items = body(posts.list_posts({}, None))["items"]
    press_items = body(posts.list_press_releases({}, None))["items"]

    assert [item["slug"] for item in post_items] == ["a-post"]
    assert [item["slug"] for item in press_items] == ["a-release"]


def test_a_post_cannot_be_read_through_the_press_release_route():
    post_id = body(create())["id"]
    result = posts.get_press_release({"pathParameters": {"post_id": post_id}}, None)
    assert result["statusCode"] == 404


def test_a_post_cannot_be_updated_through_the_press_release_route():
    post_id = body(create())["id"]
    result = posts.update_press_release(
        {"pathParameters": {"post_id": post_id}, "body": json.dumps({"title": "Hijacked"})},
        None,
    )
    assert result["statusCode"] == 404


def test_the_same_slug_is_allowed_across_content_types():
    assert create(slug="shared-slug")["statusCode"] == 201
    assert create_press(slug="shared-slug")["statusCode"] == 201


# router


def test_router_dispatches_a_static_route():
    result = router.main({"httpMethod": "GET", "path": "/posts"}, None)
    assert result["statusCode"] == 200


def test_router_extracts_path_params():
    post_id = body(create())["id"]
    result = router.main({"httpMethod": "GET", "path": f"/posts/{post_id}"}, None)
    assert result["statusCode"] == 200
    assert body(result)["id"] == post_id


def test_router_tolerates_a_trailing_slash():
    result = router.main({"httpMethod": "GET", "path": "/posts/"}, None)
    assert result["statusCode"] == 200


def test_router_dispatches_the_view_bump_without_colliding_with_get():
    post_id = body(create())["id"]
    # /posts/{id}/views (3 segments) must not be swallowed by /posts/{id} (2).
    result = router.main({"httpMethod": "POST", "path": f"/posts/{post_id}/views"}, None)
    assert result["statusCode"] == 200
    assert body(result) == {"views": 1}


def test_router_404s_on_an_unknown_path():
    result = router.main({"httpMethod": "GET", "path": "/nope"}, None)
    assert result["statusCode"] == 404


def test_router_404s_on_a_wrong_method():
    result = router.main({"httpMethod": "DELETE", "path": "/posts"}, None)
    assert result["statusCode"] == 404


def test_router_answers_cors_preflight():
    result = router.main({"httpMethod": "OPTIONS", "path": "/posts"}, None)
    assert result["statusCode"] == 204


def test_router_strips_a_configured_base_path(monkeypatch):
    # A custom-domain basePath that the edge does not strip would otherwise
    # make every inbound path miss the route map.
    monkeypatch.setattr(router, "_BASE_PATH", "/blog")
    result = router.main({"httpMethod": "GET", "path": "/blog/posts"}, None)
    assert result["statusCode"] == 200
