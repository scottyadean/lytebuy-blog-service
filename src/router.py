"""Central HTTP router for lytebuy-blog-service.

A single Lambda entry point dispatches every request based on (METHOD, path).
Handler modules are imported lazily on first hit so cold starts only pay the
import cost for routes that are actually invoked.

The route map is the source of truth for what the service exposes. Any new
HTTP endpoint should be added here and reflected in README.md.

Auth is in-code (utils.require_api_key reads the x-api-key header) rather than
API Gateway's `private: true`, so `sls offline` behaves exactly like a deployed
stage and the public list routes can sit on the same catch-all function.
"""
import importlib
import json
import os
from typing import Callable, Dict, Optional, Tuple

from src.utils import allowed_origin_for, default_headers

# Strip this prefix from inbound paths before route matching. Set when the
# API Gateway custom-domain basePath isn't being stripped at the edge.
_BASE_PATH = (os.environ.get("ROUTE_BASE_PATH") or "").rstrip("/")


# (METHOD, path template) -> (module_path, handler_name)
# A {name} segment matches one path segment and lands in event["pathParameters"].
ROUTES: Dict[Tuple[str, str], Tuple[str, str]] = {
    # health - public
    ("GET", "/health"): ("src.health", "health"),

    # posts
    ("GET", "/posts"): ("src.posts", "list_posts"),                 # public
    ("GET", "/posts/{post_id}"): ("src.posts", "get_post"),          # public: no longer bumps views
    ("POST", "/posts"): ("src.posts", "create_post"),
    ("PUT", "/posts/{post_id}"): ("src.posts", "update_post"),
    # public view-bump: the host app calls this after the post page loads
    ("POST", "/posts/{post_id}/views"): ("src.posts", "bump_post_views"),

    # press releases - same model, different content type
    ("GET", "/press-releases"): ("src.posts", "list_press_releases"),   # public
    ("GET", "/press-releases/{post_id}"): ("src.posts", "get_press_release"),  # public
    ("POST", "/press-releases"): ("src.posts", "create_press_release"),
    ("PUT", "/press-releases/{post_id}"): ("src.posts", "update_press_release"),
    ("POST", "/press-releases/{post_id}/views"): ("src.posts", "bump_press_release_views"),

    # featured vendor spotlight - same collection, content_type = featured_vendor
    ("GET", "/featured"): ("src.featured", "list_featured"),            # public: active feature
    ("GET", "/featured/{feature_id}"): ("src.featured", "get_featured"),
    ("POST", "/featured"): ("src.featured", "create_featured"),
    ("PUT", "/featured/{feature_id}"): ("src.featured", "update_featured"),
}


_HANDLER_CACHE: Dict[Tuple[str, str], Callable] = {}

# Pre-split the templates once at import so matching does not re-split on every
# request. Static routes are kept separate and always win over templated ones.
_STATIC_ROUTES = {key: value for key, value in ROUTES.items() if "{" not in key[1]}
_TEMPLATE_ROUTES = [
    (method, tuple(template.strip("/").split("/")), target)
    for (method, template), target in ROUTES.items()
    if "{" in template
]


def main(event, context):
    """Lambda entry point. Resolves (METHOD, path) and dispatches."""
    method = (event.get("httpMethod") or "").upper()
    raw_path = event.get("path") or ""
    path = raw_path.rstrip("/") or "/"

    if _BASE_PATH:
        if path == _BASE_PATH:
            path = "/"
        elif path.startswith(_BASE_PATH + "/"):
            path = path[len(_BASE_PATH):]

    # The caller's origin, needed to name them back in the CORS header. Header
    # casing is not guaranteed through API Gateway, so both spellings are read.
    headers = event.get("headers") or {}
    request_origin = headers.get("origin") or headers.get("Origin")

    # CORS preflight - respond before auth/handler dispatch
    if method == "OPTIONS":
        return _with_cors(_response(204, ""), request_origin)

    resolved = _resolve(method, path)
    if resolved is None:
        return _with_cors(
            _response(404, {"error": f"Route not found: {method} {path}"}), request_origin
        )

    target, path_params = resolved
    # The catch-all gives us {"proxy": "posts/<id>"}, so the router is the only
    # thing that can tell a handler what its path params actually were.
    event["pathParameters"] = {**(event.get("pathParameters") or {}), **path_params}

    try:
        handler = _load(*target)
    except (ImportError, AttributeError) as err:
        return _with_cors(
            _response(500, {"error": f"Failed to load handler: {err}"}), request_origin
        )

    # STAMPED HERE, not in each handler. Every handler builds its response
    # through utils.response(), which has no access to the event - so the one
    # place that sees both the request and the response is this return.
    return _with_cors(handler(event, context), request_origin)


def _with_cors(result, request_origin):
    """Name the caller's origin in a response that is already built.

    A browser requires Access-Control-Allow-Origin to match the asking origin
    exactly - a fixed value serves exactly one site, which is why the app at
    app.lytebuy.com was blocked while lytebuy.com worked.
    """
    if not isinstance(result, dict):
        return result
    result.setdefault("headers", {})
    result["headers"]["Access-Control-Allow-Origin"] = allowed_origin_for(request_origin)
    return result


def _resolve(method: str, path: str) -> Optional[Tuple[Tuple[str, str], Dict[str, str]]]:
    """Find the handler for a request, returning it with any path params."""
    static = _STATIC_ROUTES.get((method, path))
    if static is not None:
        return static, {}

    segments = tuple(path.strip("/").split("/"))
    for route_method, template_segments, target in _TEMPLATE_ROUTES:
        if route_method != method or len(template_segments) != len(segments):
            continue
        params: Dict[str, str] = {}
        for template_segment, segment in zip(template_segments, segments):
            if template_segment.startswith("{") and template_segment.endswith("}"):
                params[template_segment[1:-1]] = segment
            elif template_segment != segment:
                break
        else:
            return target, params
    return None


def _load(module_path: str, handler_name: str) -> Callable:
    """Import the handler module on first use and cache the resolved callable."""
    cache_key = (module_path, handler_name)
    cached = _HANDLER_CACHE.get(cache_key)
    if cached is not None:
        return cached
    module = importlib.import_module(module_path)
    handler = getattr(module, handler_name)
    _HANDLER_CACHE[cache_key] = handler
    return handler


def _response(status: int, body) -> dict:
    """Build a raw API Gateway response. Used for router-level errors only."""
    return {
        "statusCode": status,
        "headers": default_headers(),
        "body": body if isinstance(body, str) else json.dumps(body),
    }
