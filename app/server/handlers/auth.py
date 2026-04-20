"""


╔════════════════════════════════════════════════════════════════════════╗
║  ⚠️  DEPRECATED — LEGACY stdlib handler (extracted from                ║
║     portal_routes_post.py). Active only when TUDOU_USE_STDLIB=1 is     ║
║     set at launch. FastAPI (app/api/routers/*) is authoritative.       ║
║     Do NOT add new routes here.                                        ║
╚════════════════════════════════════════════════════════════════════════╝
Auth route handlers extracted from portal_routes_post.py.

Handles authentication endpoints:
  - POST /api/auth/reset-token  (pre-auth, localhost only)
  - POST /api/auth/login        (pre-auth)
  - POST /api/auth/logout       (pre-auth)
  - POST /api/auth/tokens       (post-auth, admin only)
"""
import json
import logging
import os

from ..portal_auth import get_client_ip, get_session_cookie
from ...defaults import LOCAL_ADDRESSES

logger = logging.getLogger("tudou.portal")


# ---------------------------------------------------------------------------
# Pre-auth endpoints (called before the auth check)
# ---------------------------------------------------------------------------

def try_handle_public(handler, path: str, hub, body: dict, auth) -> bool:
    """Handle public (pre-auth) authentication endpoints.

    Returns True if the path was handled, False otherwise.
    """

    # ---- POST /api/auth/reset-token ----
    if path == "/api/auth/reset-token":
        client_ip = get_client_ip(handler)
        if client_ip not in LOCAL_ADDRESSES:
            pass  # Allow all for now, but log it
        import secrets as _secrets
        raw = _secrets.token_hex(24)
        auth._create_token_obj("admin", "admin", raw)
        # Save to file
        token_file = os.path.join(auth._data_dir, ".admin_token")
        try:
            with open(token_file, "w") as f:
                f.write(raw)
            os.chmod(token_file, 0o600)
        except OSError:
            pass
        logger.warning("Token reset by %s — new admin token created", client_ip)
        handler._json({"ok": True, "token": raw})
        return True

    # ---- POST /api/auth/login ----
    if path == "/api/auth/login":
        ip = get_client_ip(handler)

        # Check for admin login (username/password)
        username = body.get("username", "").strip()
        password = body.get("password", "").strip()
        if username and password:
            session = auth.login_admin(username, password, ip=ip)
            if session:
                handler._json({
                    "ok": True,
                    "session_id": session.session_id,
                    "role": session.role,
                    "username": session.name,
                    "admin_user_id": session.admin_user_id,
                })
                auth.audit("login", actor=session.name, role=session.role,
                           ip=ip, success=True)
            else:
                handler._json({"error": "Invalid admin credentials"}, 401)
                auth.audit("login", actor=username, role="",
                           ip=ip, success=False)
            return True

        # Token login (existing behaviour)
        raw_token = body.get("token", "").strip()
        token_obj = auth.validate_token(raw_token)
        if token_obj:
            session = auth.create_session(token_obj, ip=ip)
            # Return session_id in body so JS can set cookie manually
            # (Chrome blocks Set-Cookie on HTTP non-localhost sites)
            handler._json({
                "ok": True,
                "session_id": session.session_id,
                "role": session.role,
            })
            auth.audit("login", actor=token_obj.name, role=token_obj.role,
                       ip=ip, success=True)
        else:
            handler._json({"error": "Invalid token"}, 401)
            auth.audit("login", actor="unknown", role="",
                       ip=ip, success=False)
        return True

    # ---- POST /api/auth/logout ----
    if path == "/api/auth/logout":
        session_id = get_session_cookie(handler)
        auth.invalidate_session(session_id)
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        # Clear cookie with same security attributes as set_session_cookie
        is_secure = os.environ.get("TUDOU_SECURE_COOKIES", "").lower() in (
            "true", "1", "yes",
        )
        secure_flag = "; Secure" if is_secure else ""
        handler.send_header(
            "Set-Cookie",
            f"td_sess=; Path=/; Max-Age=0; SameSite=Lax; HttpOnly{secure_flag}",
        )
        handler.send_header("Content-Length", "14")
        handler.end_headers()
        handler.wfile.write(b'{"ok": true}')
        return True

    return False


# ---------------------------------------------------------------------------
# Post-auth endpoints (called after the auth check)
# ---------------------------------------------------------------------------

def try_handle(handler, path: str, hub, body: dict, auth,
               actor_name: str, user_role: str) -> bool:
    """Handle authenticated auth endpoints.

    Returns True if the path was handled, False otherwise.
    """

    # ---- POST /api/auth/tokens ----
    if path == "/api/auth/tokens":
        token_name = body.get("name", "").strip()
        token_role = body.get("role", "viewer")
        token_admin_uid = body.get("admin_user_id", "")
        if not token_name:
            handler._json({"error": "Token name required"}, 400)
            return True
        token_obj = auth.create_token(token_name, token_role,
                                      admin_user_id=token_admin_uid)
        auth.audit("create_token", actor=actor_name, role=user_role,
                   target=token_name, ip=get_client_ip(handler))
        handler._json({
            "token": token_obj.to_dict(),
            "raw_token": token_obj._raw_token,
            "name": token_name,
            "role": token_role,
        })
        return True

    return False
