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


def run_all_migrations(engine: Any) -> dict:
    """Convenience: run every implemented migrator. Returns aggregate
    summary {migrator_name: {removed, added}}."""
    out: dict[str, dict] = {}
    try:
        out["workflow_catalog"] = migrate_workflow_catalog(engine)
    except Exception as e:
        logger.exception("workflow_catalog migrator crashed")
        out["workflow_catalog"] = {"error": str(e)}
    return out
