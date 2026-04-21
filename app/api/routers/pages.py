"""Page router — serves legacy portal HTML templates via Jinja2.

Routes:
  GET /           → portal (if authenticated) or login
  GET /login      → login page
  GET /index.html → alias for /
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "templates")
templates = Jinja2Templates(directory=_TEMPLATE_DIR)

logger = logging.getLogger("tudouclaw.api.pages")
router = APIRouter(tags=["pages"])

# Static JS files we fingerprint with mtime so the browser picks up
# new versions without a manual hard-refresh. Paths are relative to
# the app/server/static tree (mounted at /static). Missing files are
# tolerated (mtime=0) so tests / early-boot don't explode.
_CACHE_BUSTED_JS = (
    ("bundle_v", "app/server/static/js/portal_bundle.js"),
    ("v2_v",     "app/server/static/js/portal_v2.js"),
)


def _asset_versions() -> dict[str, int]:
    """Return {template-var-name: int(mtime)} for the cache-busted
    static files. Cheap to recompute per-request; files are local and
    os.stat is microseconds.
    """
    out: dict[str, int] = {}
    # Resolve paths relative to the repo root (two levels up from here).
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    for var_name, rel_path in _CACHE_BUSTED_JS:
        try:
            st = os.stat(os.path.join(repo_root, rel_path))
            out[var_name] = int(st.st_mtime)
        except OSError:
            out[var_name] = 0
    return out


def _is_authenticated(request: Request) -> bool:
    """Validate the session cookie or JWT — not just check existence."""
    # JWT Bearer token
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        try:
            from ..deps.auth import decode_token
            decode_token(auth_header[7:])
            return True
        except Exception:
            return False

    # Session cookie — actually validate it
    session_id = request.cookies.get("td_sess", "")
    if session_id:
        try:
            from ...auth import get_auth
            auth = get_auth()
            session = auth.validate_session(session_id)
            return bool(session)
        except Exception:
            return False

    return False


@router.get("/", response_class=HTMLResponse)
@router.get("/index.html", response_class=HTMLResponse)
async def index(request: Request):
    if _is_authenticated(request):
        # Inject mtime-based versions so each edit to portal_bundle.js
        # invalidates the browser cache automatically — no more "I
        # updated the code but Chrome still shows the old version".
        return templates.TemplateResponse(
            request, "portal.html", _asset_versions(),
        )
    # Clear stale cookie and show login
    response = templates.TemplateResponse(request, "login.html")
    if request.cookies.get("td_sess"):
        response.delete_cookie("td_sess")
    return response


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    response = templates.TemplateResponse(request, "login.html")
    if request.cookies.get("td_sess"):
        response.delete_cookie("td_sess")
    return response


@router.get("/v2", response_class=HTMLResponse)
@router.get("/v2/", response_class=HTMLResponse)
async def v2_spa(request: Request):
    """Serve the V2 SPA shell. All routing inside happens via URL hash
    (``#/v2/tasks/{id}`` etc.) — see app/static/v2/app.js."""
    if not _is_authenticated(request):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(request, "v2.html")
