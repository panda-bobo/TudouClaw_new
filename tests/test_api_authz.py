"""Authz regression guard.

Every route in ``app.api.routers.*`` must either:
  (a) use ``Depends(get_current_user)`` — standard auth, or
  (b) use ``Depends(_sse_auth_dep)`` — SSE auth (JWT via header/cookie
      OR ``?access_token=`` query param), or
  (c) be on the public ALLOWLIST below, with a written justification.

Adding a new route without one of these will fail this test, forcing
the author to either hook into auth or add an ALLOWLIST entry with a
reason other reviewers can challenge.
"""
from __future__ import annotations

import ast
import os
import pathlib

# Paths that are intentionally unauthenticated — justified in comments.
# Format: (router_filename, METHOD, path)
PUBLIC_ALLOWLIST: set[tuple[str, str, str]] = {
    # Login is the bootstrap: no way to authenticate before obtaining
    # credentials.
    ("auth.py", "POST", "/login"),

    # Logout only clears the client's cookie/local token — not
    # security-sensitive on the server side. (An attacker who could
    # reach this endpoint could just throw away their own token.)
    ("auth.py", "POST", "/logout"),

    # Kubernetes-style healthcheck. MUST be reachable before auth is
    # provisioned, so bootstrap / liveness probes can hit it.
    ("health.py", "GET", "/api/health"),

    # SPA shell pages validate via the cookie in ``_is_authenticated``
    # themselves, then redirect to /login when missing. They never
    # return protected data — the client-side JS pulls that via the
    # authenticated API.
    ("pages.py", "GET", "/"),
    ("pages.py", "GET", "/index.html"),
    ("pages.py", "GET", "/login"),
    ("pages.py", "GET", "/v2"),
    ("pages.py", "GET", "/v2/"),

    # Inbound webhooks authenticate via ``signing_secret`` inside the
    # handler (channel.py::handle_inbound verifies the signature).
    # Cookie/JWT auth doesn't apply because the caller is a 3rd-party
    # service (DingTalk / Feishu / etc.), not an end user.
    ("channels.py", "POST", "/channels/{channel_id}/webhook"),

    # Legacy inline-artifact serve used by V1 chat bubbles. Agent id +
    # artifact id are opaque and non-enumerable; marked TODO in the
    # source to add auth in a follow-up pass. Tracked separately.
    ("attachment.py", "GET", "/api/agent_state/artifact/{agent_id}/{artifact_id}"),
}


_AUTH_DEPS = ("get_current_user", "_sse_auth_dep")


def _collect_routes_without_auth() -> list[tuple[str, str, str, str, int]]:
    """Walk every router file and return routes that have no auth dep.

    Output rows: (filename, METHOD, path, function_name, lineno)
    """
    routers_dir = pathlib.Path(__file__).resolve().parent.parent / "app" / "api" / "routers"
    out: list[tuple[str, str, str, str, int]] = []
    for fn in sorted(os.listdir(routers_dir)):
        if not fn.endswith(".py") or fn == "__init__.py":
            continue
        path = routers_dir / fn
        src = path.read_text(encoding="utf-8")
        tree = ast.parse(src)
        lines = src.splitlines()

        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            # Collect EVERY route decorator on this function (stacked
            # decorators like ``@router.get("/")`` + ``@router.get("/x")``
            # must both be audited).
            routes: list[tuple[str, str]] = []
            for d in node.decorator_list:
                if isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute):
                    verb = d.func.attr
                    if verb not in ("get", "post", "put", "patch", "delete",
                                    "head", "options"):
                        continue
                    if d.args and isinstance(d.args[0], ast.Constant):
                        routes.append((verb.upper(), d.args[0].value))
            if not routes:
                continue

            # Check the full function body for an auth dep reference.
            lineno = node.lineno
            end = getattr(node, "end_lineno", lineno + 60)
            body_text = "\n".join(lines[lineno - 1:end])
            if any(dep in body_text for dep in _AUTH_DEPS):
                continue

            for method, path in routes:
                out.append((fn, method, path, node.name, lineno))
    return out


def test_no_unauthorised_routes_outside_allowlist():
    unprotected = _collect_routes_without_auth()
    stray: list[tuple[str, str, str, str, int]] = []
    for fn, method, path, name, lineno in unprotected:
        if (fn, method, path) in PUBLIC_ALLOWLIST:
            continue
        stray.append((fn, method, path, name, lineno))

    if stray:
        details = "\n".join(
            f"  {fn}:{lineno}  {method:6s} {path}  ({name})" for fn, method, path, name, lineno in stray
        )
        raise AssertionError(
            f"Found {len(stray)} route(s) without `get_current_user`/`_sse_auth_dep`:\n"
            f"{details}\n\n"
            "If this is intentional, add the route to PUBLIC_ALLOWLIST in "
            "tests/test_api_authz.py with a written justification. If not, "
            "add `user: CurrentUser = Depends(get_current_user)` to the handler."
        )


def test_allowlist_entries_still_exist():
    """If someone deletes a public endpoint, remove its allowlist entry
    too so the list stays tight."""
    unprotected = {(fn, m, p) for fn, m, p, *_ in _collect_routes_without_auth()}
    missing = [entry for entry in PUBLIC_ALLOWLIST if entry not in unprotected]
    if missing:
        raise AssertionError(
            f"PUBLIC_ALLOWLIST contains entries that no longer match any "
            f"route — remove them:\n  " +
            "\n  ".join(f"{m} {p} ({fn})" for fn, m, p in missing)
        )
