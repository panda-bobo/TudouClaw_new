"""JWT-based authentication dependency for FastAPI.

Supports two auth modes (backward compatible):
  1. JWT Bearer token in Authorization header (new, preferred)
  2. Session cookie td_sess (legacy, for migration period)
"""
from __future__ import annotations

import os
import time
import hmac
import hashlib
import logging
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

logger = logging.getLogger("tudouclaw.api.auth")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

JWT_SECRET = os.environ.get("TUDOU_JWT_SECRET", "")
JWT_ALGORITHM = "HS256"
JWT_EXPIRE_SECONDS = int(os.environ.get("TUDOU_JWT_EXPIRE", "86400"))  # 24h

# Auto-generate secret on first run if not set
if not JWT_SECRET:
    JWT_SECRET = hashlib.sha256(
        os.environ.get("TUDOU_ADMIN_SECRET", "tudouclaw-dev-secret").encode()
    ).hexdigest()

_bearer_scheme = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------

def create_access_token(
    user_id: str,
    role: str = "admin",
    extra: dict | None = None,
) -> str:
    """Create a JWT access token."""
    now = time.time()
    payload = {
        "sub": user_id,
        "role": role,
        "iat": int(now),
        "exp": int(now) + JWT_EXPIRE_SECONDS,
    }
    if extra:
        payload.update(extra)
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    """Decode and validate a JWT token. Raises on failure."""
    return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------

class CurrentUser:
    """Represents an authenticated user."""
    __slots__ = ("user_id", "role", "claims")

    def __init__(self, user_id: str, role: str = "admin", claims: dict | None = None):
        self.user_id = user_id
        self.role = role
        self.claims = claims or {}

    @property
    def is_super_admin(self) -> bool:
        return self.role == "superAdmin"


async def get_current_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> CurrentUser:
    """Extract and validate user from JWT or legacy session cookie.

    Priority:
      1. Authorization: Bearer <jwt>
      2. Cookie: td_sess=<session_id>  (legacy bridge to old auth system)
    """
    # --- Try JWT Bearer first ---
    if credentials and credentials.credentials:
        try:
            payload = decode_token(credentials.credentials)
            return CurrentUser(
                user_id=payload.get("sub", ""),
                role=payload.get("role", "admin"),
                claims=payload,
            )
        except jwt.ExpiredSignatureError:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token expired",
                headers={"WWW-Authenticate": "Bearer"},
            )
        except jwt.InvalidTokenError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=f"Invalid token: {e}",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # --- Fallback: legacy session cookie ---
    session_id = request.cookies.get("td_sess")
    if session_id:
        try:
            from ...auth import get_auth
            auth = get_auth()
            session = auth.validate_session(session_id)
            if session:
                # Resolve the real admin role from admin_mgr (session.role is a
                # coarse Role enum — "admin"/"operator" — and loses the
                # superAdmin distinction). Fall back to session.role otherwise.
                effective_role = getattr(session, "role", "admin")
                admin_uid = getattr(session, "admin_user_id", "")
                if admin_uid:
                    try:
                        admin = auth.admin_mgr.get_admin(admin_uid)
                        if admin and getattr(admin, "role", ""):
                            effective_role = admin.role
                    except Exception:
                        pass
                # Bridge: create a CurrentUser from the WebSession
                return CurrentUser(
                    user_id=admin_uid or getattr(session, "name", "legacy"),
                    role=effective_role,
                    claims={"session": True, "session_id": session_id},
                )
        except Exception as e:
            logger.debug("Legacy session validation failed: %s", e)

    # --- Fallback: old-style API token in header ---
    api_token = request.headers.get("X-API-Token") or request.query_params.get("token")
    if api_token:
        try:
            from ...auth import get_auth
            auth = get_auth()
            if auth.validate_token(api_token):
                return CurrentUser(user_id="token_user", role="admin")
        except Exception as e:
            logger.debug("API token validation failed: %s", e)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def get_optional_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_bearer_scheme),
) -> Optional[CurrentUser]:
    """Like get_current_user but returns None instead of 401."""
    try:
        return await get_current_user(request, credentials)
    except HTTPException:
        return None
