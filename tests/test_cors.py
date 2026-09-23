"""CORS: the header must name the ASKING origin (LB blog).

The deployed Lambda sent a fixed "https://lytebuy.com" to everybody, so the app
at https://app.lytebuy.com was blocked - a browser requires the header to match
the requesting origin exactly, and a fixed value serves exactly one site.
"""

import importlib

import pytest

from src import router
from src.utils import allowed_origin_for


def event(method="GET", path="/featured", origin=None):
    headers = {"origin": origin} if origin else {}
    return {
        "httpMethod": method,
        "path": path,
        "headers": headers,
        "requestContext": {"http": {"method": method, "path": path}},
    }


class TestAllowedOriginFor:
    def test_echoes_an_allowed_origin(self):
        assert allowed_origin_for("https://app.lytebuy.com") == "https://app.lytebuy.com"

    def test_echoes_the_marketing_site_too(self):
        # BOTH must work - the bug was that only one could.
        assert allowed_origin_for("https://lytebuy.com") == "https://lytebuy.com"

    def test_refuses_an_unknown_origin(self):
        # Never echoed back, so a browser on that origin is blocked.
        assert allowed_origin_for("https://evil.example") != "https://evil.example"

    def test_falls_back_for_a_non_browser_caller(self):
        # curl, a health check, server-to-server: no Origin at all.
        assert allowed_origin_for(None)

    def test_never_returns_a_star_while_credentials_are_allowed(self):
        # "*" and Access-Control-Allow-Credentials: true are illegal together,
        # so a browser would reject every response.
        assert allowed_origin_for("https://evil.example") != "*"


class TestRouterStampsTheHeader:
    def test_a_preflight_names_the_caller(self):
        result = router.main(event("OPTIONS", origin="https://app.lytebuy.com"), None)
        assert result["headers"]["Access-Control-Allow-Origin"] == "https://app.lytebuy.com"

    def test_a_404_still_carries_cors(self):
        # Without it the browser reports a CORS failure instead of the 404,
        # which is a much harder thing to debug.
        result = router.main(event("GET", "/nope", origin="https://app.lytebuy.com"), None)
        assert result["statusCode"] == 404
        assert result["headers"]["Access-Control-Allow-Origin"] == "https://app.lytebuy.com"

    def test_an_unknown_origin_is_not_echoed(self):
        result = router.main(event("OPTIONS", origin="https://evil.example"), None)
        assert result["headers"]["Access-Control-Allow-Origin"] != "https://evil.example"
