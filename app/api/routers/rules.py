"""Rule Engine REST API.

Endpoints (all under /api/portal/rules):

  GET    /rules                       — list all rules + meta (triggers, scopes)
  GET    /rules/{rule_id}             — single rule
  POST   /rules                       — create
  PATCH  /rules/{rule_id}             — update (deep merge)
  DELETE /rules/{rule_id}             — remove
  POST   /rules/{rule_id}/toggle      — enable/disable shortcut

  GET    /rules/audit?n=200&trigger=  — tail the audit log

Auth: any admin role (admin / superAdmin) via get_current_user.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Body, Query

from ..deps.auth import CurrentUser, get_current_user
from ...rule_engine import get_engine
from ...rule_engine.types import (
    Rule, RuleScope, SCOPE_KINDS, TRIGGERS, ACTION_TYPES,
)
from ...rule_engine.action import list_transforms, list_side_effects

logger = logging.getLogger("tudouclaw.api.rules")

router = APIRouter(prefix="/api/portal", tags=["rules"])


def _engine_or_503():
    e = get_engine()
    if e is None:
        raise HTTPException(503, "rule_engine not initialized")
    return e


def _check_admin(user: CurrentUser) -> None:
    if user.role not in ("admin", "superAdmin"):
        raise HTTPException(403, "admin role required")


@router.get("/rules")
async def list_rules(
    user: CurrentUser = Depends(get_current_user),
    scope_kind: str = Query("", description="Filter by scope kind"),
    trigger: str = Query("", description="Filter by trigger"),
):
    eng = _engine_or_503()
    all_rules = [r.to_dict() for r in eng.store.all()]
    if scope_kind:
        all_rules = [r for r in all_rules if r.get("scope", {}).get("kind") == scope_kind]
    if trigger:
        all_rules = [r for r in all_rules if r.get("trigger") == trigger]
    return {
        "rules": all_rules,
        "meta": {
            "scope_kinds": list(SCOPE_KINDS),
            "triggers": list(TRIGGERS),
            "action_types": list(ACTION_TYPES),
            "transforms": list_transforms(),
            "side_effects": list_side_effects(),
        },
    }


@router.get("/rules/audit")
async def get_audit(
    user: CurrentUser = Depends(get_current_user),
    n: int = Query(200, ge=1, le=2000),
    trigger: str = Query(""),
    rule_id: str = Query(""),
    agent_id: str = Query(""),
    decision: str = Query(""),
):
    """Tail the engine audit log. Filter by trigger / rule / agent / decision."""
    eng = _engine_or_503()
    filters = {}
    if trigger: filters["trigger"] = trigger
    if rule_id: filters["rule_id"] = rule_id
    if agent_id: filters["agent_id"] = agent_id
    if decision: filters["decision"] = decision
    return {"entries": eng.audit.tail(n=n, filters=filters or None)}


@router.get("/rules/{rule_id}")
async def get_rule(rule_id: str, user: CurrentUser = Depends(get_current_user)):
    eng = _engine_or_503()
    r = eng.store.get(rule_id)
    if r is None:
        raise HTTPException(404, f"rule not found: {rule_id}")
    return {"rule": r.to_dict()}


@router.post("/rules")
async def create_rule(
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
):
    _check_admin(user)
    eng = _engine_or_503()
    # Validate trigger + scope kind
    trigger = str(body.get("trigger") or "")
    if trigger not in TRIGGERS:
        raise HTTPException(400, f"trigger must be one of {TRIGGERS}, got {trigger!r}")
    scope_kind = (body.get("scope") or {}).get("kind") or "global"
    if scope_kind not in SCOPE_KINDS:
        raise HTTPException(400, f"scope.kind must be one of {SCOPE_KINDS}")
    # Validate every action's type
    for act in (body.get("actions") or []):
        atype = (act or {}).get("type")
        if atype not in ACTION_TYPES:
            raise HTTPException(400, f"action.type must be one of {ACTION_TYPES}, got {atype!r}")
    # Stamp creator + ensure required fields
    body.setdefault("created_by", user.user_id or "")
    body.setdefault("name", "(unnamed)")
    rule = Rule.from_dict(body)
    eng.store.add(rule, by=user.user_id or "")
    return {"rule": rule.to_dict()}


@router.patch("/rules/{rule_id}")
async def update_rule(
    rule_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
):
    _check_admin(user)
    eng = _engine_or_503()
    if "trigger" in body and body["trigger"] not in TRIGGERS:
        raise HTTPException(400, f"trigger must be one of {TRIGGERS}")
    scope = body.get("scope")
    if isinstance(scope, dict) and "kind" in scope and scope["kind"] not in SCOPE_KINDS:
        raise HTTPException(400, f"scope.kind must be one of {SCOPE_KINDS}")
    note = str(body.get("revision_note") or "")
    body.pop("revision_note", None)
    updated = eng.store.update(rule_id, body, by=user.user_id or "",
                                revision_note=note)
    if updated is None:
        raise HTTPException(404, f"rule not found: {rule_id}")
    return {"rule": updated.to_dict()}


@router.delete("/rules/{rule_id}")
async def delete_rule(
    rule_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    _check_admin(user)
    eng = _engine_or_503()
    if not eng.store.delete(rule_id, by=user.user_id or ""):
        raise HTTPException(404, f"rule not found: {rule_id}")
    return {"deleted": rule_id}


@router.post("/rules/{rule_id}/toggle")
async def toggle_rule(
    rule_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
):
    _check_admin(user)
    eng = _engine_or_503()
    enabled = bool(body.get("enabled", True))
    ok = eng.store.set_enabled(rule_id, enabled, by=user.user_id or "")
    if not ok:
        raise HTTPException(404, f"rule not found: {rule_id}")
    return {"rule_id": rule_id, "enabled": enabled}
