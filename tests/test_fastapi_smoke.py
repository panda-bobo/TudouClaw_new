"""FastAPI smoke: hit every route and assert the status code is sensible.

Strategy
========
1. For each route registered on ``app``, build a dummy URL by filling
   path params with "smoke-test" or "1". Skip websocket, static, and
   routes that require a file upload.
2. Issue the request WITHOUT an auth token and assert the response is
   one of: 401 (auth guard), 403 (permissions), 404 (id not found),
   405 (method mismatch), 422 (body/query validation).
   Specifically it must NOT be 500 (unhandled) or something crashing
   the app.
3. Issue the request WITH an admin Bearer token for routes that accept
   it; assert 200 / 404 / 422 rather than 401 — this verifies auth
   accepts the superAdmin token and the handler runs.
4. A handful of "public" routes (``/api/health``, ``/api/docs``,
   ``/api/openapi.json``) must return 200 without auth.

This is a SMOKE test, not a functional one. It doesn't verify business
semantics; it verifies the router is wired, auth enforces correctly,
and nothing explodes at import or first request.
"""
from __future__ import annotations

import os

import pytest


# Isolate data dir so we don't touch the user's real profile.
os.environ.setdefault("TUDOU_CLAW_DATA_DIR", "/tmp/tudou_fastapi_smoke_data")
os.makedirs(os.environ["TUDOU_CLAW_DATA_DIR"], exist_ok=True)
os.environ.setdefault("TUDOU_ADMIN_SECRET", "smoketest-secret")


from fastapi.testclient import TestClient  # noqa: E402
from app.api.main import app  # noqa: E402


# ── Routes we KNOW are public (return 200 without auth) ────────────────
PUBLIC = {
    "/api/health",
    "/api/docs",
    "/api/redoc",
    "/api/openapi.json",
    "/docs/oauth2-redirect",
    # Auth endpoints are of course public — they're how you log in / out.
    "/api/auth/login",
    "/api/auth/logout",
    # Page routes serve HTML; they render a login redirect client-side.
    "/", "/index.html", "/login", "/v2", "/v2/",
    # Inbound webhooks authenticate via the channel's own token.
    "/api/portal/channels/{channel_id}/webhook",
}

# ── Routes to skip entirely (websockets, file uploads, streaming) ─────
# SSE endpoints hang in TestClient because streaming never closes; skip.
SKIP_PREFIX = (
    "/api/portal/agent/",      # many of these are SSE / streaming
    "/api/v2/tasks/",          # SSE task events
    "/api/v2/agents/",         # some are SSE
)
SKIP_SUFFIX = (
    "/events",
    "/stream",
    "/sse",
    "/attachments",            # multipart upload
    "/attachment",
)


def _fill_path(path: str) -> str:
    """Replace FastAPI path params like ``{agent_id}`` with a stub."""
    import re
    return re.sub(r"\{[^}]+\}", "smoketest", path)


def _expected_no_auth(status: int) -> bool:
    """What statuses count as "auth guard did its job"?"""
    return status in (401, 403, 404, 405, 422)


def _expected_with_auth(status: int) -> bool:
    """With a valid admin token the handler ran: success, not-found, or
    validation error are all acceptable."""
    return status in (200, 201, 202, 204, 400, 404, 405, 409, 422, 501)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c
    # Reset globals the FastAPI lifespan populated so later unit tests
    # that monkeypatch individual singletons aren't polluted by our
    # state (e.g. test_v2_tier_routing expects an empty LLMTierRouter).
    try:
        import app.llm_tier_routing as _lt
        _lt._router = None
    except Exception:
        pass
    try:
        import app.api.deps.hub as _hdep
        _hdep._hub_instance = None
    except Exception:
        pass


@pytest.fixture(scope="module")
def admin_jwt(client):
    """Pull the superAdmin JWT minted by lifespan."""
    from app.api.deps.auth import create_access_token
    return create_access_token(
        user_id="admin",
        role="superAdmin",
        extra={"token_login": True},
    )


def _iter_testable_routes():
    """Yield (method, path) for every testable route on ``app``."""
    for r in app.routes:
        methods = getattr(r, "methods", None) or set()
        path = getattr(r, "path", "")
        if not path or not methods:
            continue
        if not path.startswith("/"):
            continue
        # Skip uninteresting / infra routes
        if path.startswith("/static"):
            continue
        if path.startswith("/workspace/shared"):
            continue
        # Skip routes we know hang or require multipart
        if path.startswith(SKIP_PREFIX) and path.endswith(SKIP_SUFFIX):
            continue
        for m in methods:
            if m == "HEAD":
                continue
            yield m, path


def test_public_routes_200(client):
    """Health/docs must be reachable without auth."""
    for p in ("/api/health", "/api/openapi.json"):
        r = client.get(p)
        assert r.status_code == 200, f"{p} expected 200, got {r.status_code}"


def test_every_route_auth_guard(client):
    """Every non-public route rejects unauthenticated requests cleanly."""
    failures: list[str] = []
    tested = 0
    for method, path in _iter_testable_routes():
        if path in PUBLIC:
            continue
        url = _fill_path(path)
        # Minimal body — handlers that require body get 422, which we accept.
        try:
            r = client.request(method, url, json={})
        except Exception as e:
            failures.append(f"{method} {url} raised {type(e).__name__}: {e}")
            continue
        tested += 1
        if not _expected_no_auth(r.status_code):
            failures.append(
                f"{method} {url} -> {r.status_code} "
                f"(expected one of 401/403/404/405/422)"
            )
    assert not failures, (
        f"{len(failures)} routes misbehaved without auth (of {tested} tested):\n  "
        + "\n  ".join(failures[:40])
    )


# Routes that are INTENTIONALLY admin-only and legitimately 403 a
# plain superAdmin smoketest token (e.g. node-to-hub sync endpoints
# require a shared_secret header, not a user JWT).
ADMIN_403_OK = {
    # Inter-node sync: uses shared-secret auth not Bearer JWT.
    "/api/hub/agents",
    "/api/hub/register",
    "/api/hub/sync",
    "/api/hub/heartbeat",
    "/api/hub/message",
    "/api/hub/broadcast",
    "/api/hub/refresh",
    "/api/hub/deliver",
    "/api/hub/dispatch-config",
    "/api/hub/batch-dispatch-config",
    "/api/hub/apply-config",
    "/api/hub/confirm-config",
    "/api/hub/apply-node-config",
    "/api/hub/orchestrate",
}


def test_every_route_with_admin(client, admin_jwt):
    """With a superAdmin JWT the handler runs — not 401/403."""
    headers = {"Authorization": f"Bearer {admin_jwt}"}
    failures: list[str] = []
    tested = 0
    for method, path in _iter_testable_routes():
        if path in PUBLIC:
            continue
        # Skip hub/node WS endpoints (can hang)
        if "/ws" in path:
            continue
        url = _fill_path(path)
        try:
            r = client.request(method, url, headers=headers, json={})
        except Exception as e:
            failures.append(f"{method} {url} raised {type(e).__name__}: {e}")
            continue
        tested += 1
        # Must NOT be 401/403 (auth should accept the token)
        # and must NOT be 500 (handler crashed unexpectedly).
        if r.status_code == 403 and path in ADMIN_403_OK:
            continue  # expected: these require shared-secret, not JWT
        if r.status_code in (401, 403, 500):
            body = (r.text or "")[:120].replace("\n", " ")
            failures.append(f"{method} {url} -> {r.status_code}  body={body!r}")
    assert not failures, (
        f"{len(failures)} routes failed under admin auth (of {tested} tested):\n  "
        + "\n  ".join(failures[:40])
    )
