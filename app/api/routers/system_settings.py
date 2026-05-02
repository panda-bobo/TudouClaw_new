"""System settings router — admin-editable runtime config (concurrency
caps, defaults, etc.). Backed by app.system_settings.SystemSettingsStore.

GET returns {settings, defaults} so the UI can offer "Reset to defaults"
intelligently. PATCH takes {path, value} for single-key updates.

Path allow-list: validators only accept paths that exist in DEFAULTS —
prevents arbitrary key bloat in the persisted file. Range checks
specifically for the parallel-cap knobs.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Body

from ..deps.auth import CurrentUser, get_current_user
from ...system_settings import DEFAULTS, get_store

logger = logging.getLogger("tudou.api.system_settings")

router = APIRouter(prefix="/api/portal", tags=["system-settings"])


def _walk_defaults(path: str) -> tuple[bool, Any]:
    """Walk DEFAULTS using dotted-path. Returns (path_exists, default_value)."""
    cursor: Any = DEFAULTS
    for part in path.split("."):
        if isinstance(cursor, dict) and part in cursor:
            cursor = cursor[part]
        else:
            return False, None
    return True, cursor


def _validate_value(path: str, value: Any) -> None:
    """Per-path validation. Raises HTTPException(400) on bad input."""
    if path in ("canvas.max_parallel_nodes", "delegate.max_parallel_children"):
        # bool is a subclass of int in Python, so check it first
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(400, f"{path} must be an integer")
        if not (1 <= value <= 32):
            raise HTTPException(400, f"{path} must be in 1..32 (got {value})")
        return
    # Unknown path: reject (caught by caller's _walk_defaults check too,
    # but explicit here for safety)
    raise HTTPException(400, f"unknown / not allowed path: {path}")


@router.get("/system-settings")
async def get_system_settings(user: CurrentUser = Depends(get_current_user)):
    store = get_store()
    if store is None:
        raise HTTPException(503, "system_settings store not initialized")
    return {"settings": store.all(), "defaults": DEFAULTS}


@router.patch("/system-settings")
async def patch_system_settings(
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
):
    if user.role != "superAdmin":
        raise HTTPException(403, "only superAdmin can change system settings")
    path = str(body.get("path") or "").strip()
    value = body.get("value")
    if not path:
        raise HTTPException(400, "missing 'path'")
    exists, _ = _walk_defaults(path)
    if not exists:
        raise HTTPException(400, f"unknown / not allowed path: {path}")
    _validate_value(path, value)

    store = get_store()
    if store is None:
        raise HTTPException(503, "system_settings store not initialized")
    store.set(path, value)
    return {"settings": store.all(), "defaults": DEFAULTS}
