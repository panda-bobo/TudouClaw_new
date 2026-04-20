"""Authentication router — login, token management, admin info."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from ..deps.hub import get_hub
from ..deps.auth import (
    CurrentUser, get_current_user, create_access_token, JWT_EXPIRE_SECONDS,
)
from ..schemas.auth import LoginRequest, LoginResponse, TokenCreateRequest

logger = logging.getLogger("tudouclaw.api.auth")

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Login (public — no auth required)
# ---------------------------------------------------------------------------

@router.post("/login", response_model=LoginResponse)
async def login(body: LoginRequest, response: Response):
    """Authenticate via username+password or legacy token.

    Returns a JWT access_token for subsequent API calls.
    Also sets a legacy td_sess cookie for backward compat.
    """
    from ...auth import get_auth
    auth = get_auth()

    user_id = ""
    role = "admin"

    session_id = ""

    # Mode 1: username + password (admin login)
    if body.username and body.password:
        session = auth.login_admin(body.username, body.password)
        if not session:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid username or password",
            )
        user_id = session.admin_user_id or body.username
        role = session.role or "admin"
        session_id = session.session_id

    # Mode 2: legacy API token
    elif body.token:
        token_obj = auth.validate_token(body.token)
        if not token_obj:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token",
            )
        user_id = getattr(token_obj, "admin_user_id", "") or "token_user"
        role = getattr(token_obj, "role", "admin")
        # Create a session for legacy compat — create_session expects APIToken
        s = auth.create_session(token_obj)
        session_id = s.session_id

    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide username+password or token",
        )

    # Issue JWT
    access_token = create_access_token(user_id, role)

    # Set legacy cookie for backward compat during migration
    if session_id:
        response.set_cookie(
            key="td_sess",
            value=session_id,
            httponly=True,
            samesite="lax",
            max_age=86400,
        )

    return LoginResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=JWT_EXPIRE_SECONDS,
        user_id=user_id,
        role=role,
        session_id=session_id or None,
    )


@router.post("/logout")
async def logout(response: Response):
    """Clear session cookie — path/samesite must match set_cookie."""
    response.delete_cookie("td_sess", path="/", samesite="lax")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Token management (authenticated)
# ---------------------------------------------------------------------------

@router.get("/tokens")
async def list_tokens(
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """List all API tokens — matches legacy portal_routes_get."""
    from ...auth import get_auth
    auth = get_auth()
    tokens = auth.list_tokens() if hasattr(auth, "list_tokens") else []
    # Enrich with admin display name
    for t in tokens:
        aid = t.get("admin_user_id", "")
        if aid:
            adm = auth.admin_mgr.get_admin(aid) if hasattr(auth, "admin_mgr") else None
            t["admin_display_name"] = adm.display_name if adm else aid
        else:
            t["admin_display_name"] = ""
    return {"tokens": tokens}


@router.post("/tokens")
async def create_token(
    body: TokenCreateRequest,
    user: CurrentUser = Depends(get_current_user),
):
    """Create a new API token."""
    from ...auth import get_auth
    auth = get_auth()
    token = auth.create_token(body.name, body.role, body.admin_user_id)
    return {"ok": True, "token": token}


@router.delete("/tokens/{token_id}")
async def revoke_token(
    token_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    """Revoke an API token."""
    from ...auth import get_auth
    auth = get_auth()
    auth.revoke_token(token_id)
    return {"ok": True}


@router.post("/reset-token")
async def reset_admin_token(
    user: CurrentUser = Depends(get_current_user),
):
    """Reset the admin token — generates a new one.

    Super-admin only. Without auth, anyone reaching the server could
    revoke the real operator's access by spamming this endpoint.
    """
    if not user.is_super_admin:
        raise HTTPException(status_code=403, detail="super-admin only")
    from ...auth import get_auth
    import os, secrets
    auth = get_auth()
    # Parity with legacy portal_routes_post handler: mint a fresh admin
    # token, create a token object for it, and persist to .admin_token.
    raw = secrets.token_hex(24)
    try:
        auth._create_token_obj("admin", "admin", raw)
        token_file = os.path.join(auth._data_dir, ".admin_token")
        with open(token_file, "w") as f:
            f.write(raw)
        os.chmod(token_file, 0o600)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Token reset failed: {e}")
    return {"ok": True, "token": raw}


# ---------------------------------------------------------------------------
# Admin info
# ---------------------------------------------------------------------------

@router.get("/me")
async def admin_me(user: CurrentUser = Depends(get_current_user)):
    """Get current admin user info."""
    return {
        "user_id": user.user_id,
        "role": user.role,
        "is_super_admin": user.is_super_admin,
    }
