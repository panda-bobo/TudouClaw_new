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
    # ── Concurrency caps (existing) ──
    if path in ("canvas.max_parallel_nodes", "delegate.max_parallel_children"):
        # bool is a subclass of int in Python, so check it first
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(400, f"{path} must be an integer")
        if not (1 <= value <= 32):
            raise HTTPException(400, f"{path} must be in 1..32 (got {value})")
        return

    # ── Rate limiter ──
    if path == "rate_limit.enabled":
        if not isinstance(value, bool):
            raise HTTPException(400, f"{path} must be boolean")
        return
    if path == "rate_limit.max_requests":
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(400, f"{path} must be an integer")
        if not (1 <= value <= 10000):
            raise HTTPException(400, f"{path} must be in 1..10000")
        return
    if path == "rate_limit.window_seconds":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise HTTPException(400, f"{path} must be a number")
        if not (0.1 <= float(value) <= 3600):
            raise HTTPException(400, f"{path} must be in 0.1..3600")
        return

    # ── Agent guardrails ──
    if path in ("agent_guardrails.glob_soft_warn_per_hour",
                "agent_guardrails.glob_hard_deny_per_hour",
                "agent_guardrails.tool_budget_per_turn"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(400, f"{path} must be an integer")
        if not (1 <= value <= 1000):
            raise HTTPException(400, f"{path} must be in 1..1000")
        return
    if path == "agent_guardrails.deliverable_strictness":
        if value not in ("strict", "lenient"):
            raise HTTPException(400, f"{path} must be 'strict' or 'lenient'")
        return

    # ── UI polling cadence ──
    if path in ("ui_polling.heartbeat_seconds",
                "ui_polling.plans_busy_seconds",
                "ui_polling.runtime_stats_busy_seconds"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise HTTPException(400, f"{path} must be an integer (seconds)")
        if not (1 <= value <= 600):
            raise HTTPException(400, f"{path} must be in 1..600")
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
    # System settings are admin-tunable (matches branding/global-config
    # convention). Originally gated to superAdmin, but the live JWT
    # role surfaced via /api/auth/login carries "admin" even for the
    # default install superAdmin (legacy mapping). Both admin and
    # superAdmin can change runtime knobs; plain "user" stays out.
    if user.role not in ("admin", "superAdmin"):
        raise HTTPException(403, "admin role required to change system settings")
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
