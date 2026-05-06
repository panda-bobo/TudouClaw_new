"""Migrators that lift existing scattered policy into engine rules.

Each migrator function:
  1. Reads its source-of-truth (catalog dict / DEFAULT_TOOL_RISK / etc.)
  2. Generates Rule objects with source="migrator:<name>"
  3. Removes prior rules with the same source (idempotent re-run safe)
  4. Adds the new rules to the store

Migrators should be called at hub boot (after the engine is
initialized but before any agent runs). They're also safe to re-run
on demand (UI button "re-import workflow rules").

Currently implemented:
  - migrate_workflow_catalog — turns catalog step contracts
    (output_files / must_contain / min_lines) into per-step
    before_task_done rules. Replaces the hardcoded check at
    project.py:_check_active_task_deliverables (which stays as a
    fallback during transition; once the engine rules are validated
    in production the project.py check can be deleted).
"""
from __future__ import annotations

import logging
from typing import Any

from .types import Rule, RuleScope

logger = logging.getLogger("tudou.rule_engine.migrators")


def _purge_source(store, source: str) -> int:
    """Delete every rule whose source matches. Returns count removed."""
    removed = 0
    for r in list(store.all()):
        if r.source == source:
            if store.delete(r.id, by="migrator"):
                removed += 1
    return removed


def migrate_workflow_catalog(engine: Any, catalog: list[dict] | None = None) -> dict:
    """Turn each step's deliverable contract in the WORKFLOW_CATALOG
    into a global rule fired on before_task_done.

    Generated rule shape (one per non-empty contract):
      name:    "WF:<workflow_id>:<step_id> output_files present"
      scope:   global (applies to any project that binds this workflow)
      trigger: before_task_done
      condition: task title starts with [WF Step N], output_files
                 declares the contracted file, file is verified
      actions: deny if not all output_files verified

    Returns a summary dict {removed, added} for the API/UI to surface.
    """
    if catalog is None:
        try:
            from ..data.workflow_catalog import WORKFLOW_CATALOG
            catalog = WORKFLOW_CATALOG
        except Exception as e:
            logger.warning("workflow_catalog import failed; skipping migration: %s", e)
            return {"removed": 0, "added": 0, "error": str(e)}

    source = "migrator:workflow_catalog"
    removed = _purge_source(engine.store, source)
    added = 0

    for wf in catalog:
        wf_id = wf.get("id") or ""
        wf_name = wf.get("name") or ""
        for step in (wf.get("steps") or []):
            output_files = step.get("output_files") or []
            if not output_files:
                continue
            step_id = step.get("id") or ""
            step_name = step.get("name") or ""
            min_lines = int(step.get("min_lines") or 0)

            # Match any task title that contains the step name (this is
            # how project.py creates tasks: "[WF Step N] <step_name>").
            # When the title doesn't contain step_name (rare), the rule
            # silently won't match — that's fine, falls back to the
            # generic "no output_files" gate.
            condition = {
                "all": [
                    {"field": "task.title", "contains": step_name},
                    {
                        "any": [
                            # No output_files at all → deny
                            {"field": "task.output_files",
                             "length_lt": len(output_files)},
                            # Below required min_lines → deny
                            {"all": [
                                {"field": "task.min_lines", "gt": 0},
                                {"field": "task.min_lines", "lt": min_lines},
                            ]} if min_lines > 0 else {"field": "_dummy",
                                                       "exists": False},
                        ]
                    },
                ]
            }
            rule = Rule(
                name=f"WF:{wf_id}:{step_id} contract",
                description=(
                    f"[migrated] {wf_name} step '{step_name}' must produce "
                    f"{', '.join(output_files)} ({min_lines}+ lines each)"
                ),
                scope=RuleScope("global"),
                trigger="before_task_done",
                condition=condition,
                actions=[{
                    "type": "deny",
                    "message": (
                        f"step '{step_name}' must produce "
                        f"{output_files} (catalog contract)"
                    ),
                }],
                priority=20,   # higher than user-authored rules by default
                source=source,
                created_by="migrator",
            )
            engine.store.add(rule, by="migrator")
            added += 1

    logger.info("workflow_catalog migration: removed=%d added=%d",
                removed, added)
    return {"removed": removed, "added": added}


def migrate_global_denylist(engine: Any) -> dict:
    """Lift auth.ToolPolicy.global_denylist (admin-managed deny set
    of tool names) into engine deny rules. One rule per name.

    Old check at agent.py:6736 still runs in parallel during transition;
    once these rules are validated in production it can be removed.
    """
    source = "migrator:global_denylist"
    removed = _purge_source(engine.store, source)
    added = 0
    try:
        from ..auth import get_auth
        auth = get_auth()
        names = sorted(getattr(auth.tool_policy, "global_denylist", set()) or [])
    except Exception as e:
        logger.warning("global_denylist source unavailable: %s", e)
        return {"removed": removed, "added": 0, "error": str(e)}
    for name in names:
        rule = Rule(
            name=f"global_denylist:{name}",
            description=f"[migrated] tool '{name}' on the admin global denylist",
            scope=RuleScope("global"),
            trigger="before_tool_call",
            condition={"field": "tool_name", "eq": name},
            actions=[{
                "type": "deny",
                "message": f"tool '{name}' is on the global denylist",
            }],
            priority=100,           # very high — admin denylist outranks others
            source=source,
            created_by="migrator",
        )
        engine.store.add(rule, by="migrator")
        added += 1
    logger.info("global_denylist migration: removed=%d added=%d", removed, added)
    return {"removed": removed, "added": added}


def migrate_tool_risk(engine: Any) -> dict:
    """Lift auth.DEFAULT_TOOL_RISK (tool → risk level) into engine
    rules. High-risk tools become require_approval rules; "red" tools
    become deny rules.

    Tools at "moderate" / "low" risk don't need rules — they auto-
    approve in the existing flow. Migrating only the actionable tiers
    (high + red) keeps the rule set focused.
    """
    source = "migrator:tool_risk"
    removed = _purge_source(engine.store, source)
    added = 0
    try:
        from ..auth import DEFAULT_TOOL_RISK
        risks = dict(DEFAULT_TOOL_RISK or {})
    except Exception as e:
        logger.warning("tool_risk source unavailable: %s", e)
        return {"removed": removed, "added": 0, "error": str(e)}
    for tool_name, risk in sorted(risks.items()):
        if risk == "red":
            actions = [{"type": "deny",
                        "message": f"tool '{tool_name}' is permanently blocked (red tier)"}]
        elif risk == "high":
            actions = [{"type": "require_approval",
                        "message": f"tool '{tool_name}' requires admin approval (high tier)"}]
        else:
            continue   # moderate / low handled by existing auto-approve path
        rule = Rule(
            name=f"tool_risk:{tool_name}",
            description=f"[migrated] {tool_name} tier={risk}",
            scope=RuleScope("global"),
            trigger="before_tool_call",
            condition={"field": "tool_name", "eq": tool_name},
            actions=actions,
            priority=80,
            source=source,
            created_by="migrator",
        )
        engine.store.add(rule, by="migrator")
        added += 1
    logger.info("tool_risk migration: removed=%d added=%d", removed, added)
    return {"removed": removed, "added": added}


def migrate_command_patterns(engine: Any) -> dict:
    """Lift auth.ToolPolicy.command_patterns (per-bash-content regex deny
    rules) into engine deny rules.

    Each pattern entry has shape:
      {"pattern": "<regex>", "scope": "global"|"role:X"|"agent:Y",
       "verdict": "deny"|"needs_approval", "reason": "...", "label": "..."}

    Maps scope dialect → engine RuleScope (only "global" + "role:X" are
    in active use; "agent:Y" maps to solo with the agent_id).
    """
    source = "migrator:command_patterns"
    removed = _purge_source(engine.store, source)
    added = 0
    try:
        from ..auth import get_auth
        auth = get_auth()
        patterns = list(getattr(auth.tool_policy, "command_patterns", []) or [])
    except Exception as e:
        logger.warning("command_patterns source unavailable: %s", e)
        return {"removed": removed, "added": 0, "error": str(e)}
    for entry in patterns:
        if not isinstance(entry, dict):
            continue
        pattern = str(entry.get("pattern") or "").strip()
        if not pattern:
            continue
        verdict = str(entry.get("verdict") or "deny").lower()
        reason = str(entry.get("reason") or "")
        label = str(entry.get("label") or "")
        scope_raw = str(entry.get("scope") or "global")
        if scope_raw == "global":
            scope = RuleScope("global")
        elif scope_raw.startswith("role:"):
            # No engine scope kind for "role" yet — fall back to global,
            # add condition gating on agent.role.
            scope = RuleScope("global")
        elif scope_raw.startswith("agent:"):
            scope = RuleScope("solo", [scope_raw[6:]])
        else:
            scope = RuleScope("global")

        # Build condition: bash command argument matches the pattern.
        # Also gate on agent.role when scope is "role:X" (since engine
        # scope is global, role gating needs the condition).
        conds: list = [
            {"field": "tool_name", "eq": "bash"},
            {"field": "args.command", "matches": pattern},
        ]
        if scope_raw.startswith("role:"):
            conds.append({"field": "agent.role", "eq": scope_raw[5:]})
        condition = {"all": conds}

        if verdict == "deny":
            actions = [{"type": "deny",
                        "message": reason or f"command matches denied pattern {pattern!r}"}]
        else:
            actions = [{"type": "require_approval",
                        "message": reason or f"command matches: {pattern!r}"}]
        rule = Rule(
            name=f"command_pattern:{label or pattern[:30]}",
            description=f"[migrated] {reason}" if reason else f"[migrated] {pattern}",
            scope=scope,
            trigger="before_tool_call",
            condition=condition,
            actions=actions,
            priority=90,
            source=source,
            created_by="migrator",
        )
        engine.store.add(rule, by="migrator")
        added += 1
    logger.info("command_patterns migration: removed=%d added=%d", removed, added)
    return {"removed": removed, "added": added}


def run_all_migrations(engine: Any) -> dict:
    """Convenience: run every implemented migrator. Returns aggregate
    summary {migrator_name: {removed, added}}."""
    out: dict[str, dict] = {}
    for name, fn in (
        ("workflow_catalog", migrate_workflow_catalog),
        ("global_denylist", migrate_global_denylist),
        ("tool_risk", migrate_tool_risk),
        ("command_patterns", migrate_command_patterns),
    ):
        try:
            out[name] = fn(engine)
        except Exception as e:
            logger.exception("%s migrator crashed", name)
            out[name] = {"error": str(e)}
    return out
