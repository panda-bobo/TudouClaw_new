"""
Centralised agent data-cleanup.

When a V1 agent is deleted, data scattered across many subsystems
becomes orphaned — memory, file manifests, MCP bindings, skill grants,
routing tables, approvals, agent-to-agent messages, V2 tasks, and so on.
This module knows about all of them and exposes a single entry point:

    purge_agent(agent_id) -> dict[str, int]

Returns a per-subsystem count of rows/entries removed so callers (REST
handlers, migration scripts) can show the user exactly what was cleaned.

Design notes:
    * We do NOT raise on subsystem-level failures. Each subsystem is
      best-effort; a broken DB row should not block the rest of the
      cleanup. Failures are logged and recorded in the returned dict
      as a negative count (``-1``) for visibility.
    * Only delete side effects — don't UPDATE things. If a row simply
      *mentions* the agent but the row itself belongs to something else
      (e.g. ``agent_messages`` mentioning the agent), we delete those too;
      their existence without the agent would be dangling.
    * Skip cleanup for empty / ``None`` agent_id.
"""
from __future__ import annotations

import logging
import os
import shutil
from typing import Any

logger = logging.getLogger("tudouclaw.cleanup")


def purge_agent(agent_id: str) -> dict[str, int]:
    """Remove every piece of data tied to ``agent_id``.

    Returns a dict mapping subsystem → rows_removed. A value of ``-1``
    means that subsystem raised during cleanup (see logs for details).
    Does NOT itself delete the ``agents`` row — callers (V1
    ``remove_agent`` / V2 endpoint) remain authoritative for that.
    """
    agent_id = (agent_id or "").strip()
    if not agent_id:
        return {}

    report: dict[str, int] = {}

    report["db_tables"]     = _purge_db(agent_id)
    report["mcp_bindings"]  = _purge_mcp(agent_id)
    report["skill_grants"]  = _purge_skills(agent_id)
    report["v2_tasks"]      = _purge_v2(agent_id)
    report["workspace"]     = _purge_workspace(agent_id)

    logger.info("purge_agent %r: %s", agent_id, report)
    return report


# ── V1 SQLite tables ──────────────────────────────────────────────────


# Tables that have an ``agent_id`` column (direct reference).
_AGENT_ID_TABLES: tuple[str, ...] = (
    "agent_routes",
    "memory_episodic",
    "memory_semantic",
    "memory_config",
    "file_manifests",
    "approvals",
)

# Tables that reference the agent via other columns (from/to).
# Format: (table_name, ((col1, val), (col2, val), ...))
_AGENT_REF_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("agent_messages", ("from_agent", "to_agent")),
    ("delegations",    ("from_agent", "to_agent")),
)


def _purge_db(agent_id: str) -> int:
    """Delete agent-related rows from every V1 SQLite table.

    Returns total rows removed across all tables, or ``-1`` on failure.
    """
    try:
        from .infra.database import get_database
        db = get_database()
    except Exception as e:  # noqa: BLE001
        logger.warning("cleanup: database not available: %s", e)
        return -1

    conn = getattr(db, "_conn", None)
    if conn is None:
        logger.warning("cleanup: database connection missing")
        return -1

    total = 0
    try:
        cur = conn.cursor()
        for table in _AGENT_ID_TABLES:
            try:
                r = cur.execute(
                    f"DELETE FROM {table} WHERE agent_id = ?", (agent_id,),
                )
                total += r.rowcount or 0
            except Exception as e:  # noqa: BLE001
                logger.warning("cleanup: %s delete failed: %s", table, e)
        for table, cols in _AGENT_REF_TABLES:
            clause = " OR ".join(f"{c} = ?" for c in cols)
            try:
                r = cur.execute(
                    f"DELETE FROM {table} WHERE {clause}",
                    tuple(agent_id for _ in cols),
                )
                total += r.rowcount or 0
            except Exception as e:  # noqa: BLE001
                logger.warning("cleanup: %s delete failed: %s", table, e)
        conn.commit()
    except Exception as e:  # noqa: BLE001
        logger.warning("cleanup: db purge failed: %s", e)
        return -1
    return total


# ── V1 MCP manager in-memory + JSON persistence ───────────────────────


def _purge_mcp(agent_id: str) -> int:
    """Drop the agent's bindings + env overrides from every MCP node_config.

    Returns total entries removed (bindings + per-mcp env override sets).
    """
    try:
        from .mcp import manager as _mgr
    except ImportError:
        return 0

    removed = 0
    try:
        mgr = _mgr.get_mcp_manager()
    except Exception as e:  # noqa: BLE001
        logger.warning("cleanup: mcp manager unavailable: %s", e)
        return -1

    for node_cfg in list(getattr(mgr, "node_configs", {}).values()):
        try:
            bindings = getattr(node_cfg, "agent_bindings", None)
            if isinstance(bindings, dict) and agent_id in bindings:
                removed += len(bindings[agent_id] or [])
                del bindings[agent_id]
            overrides = getattr(node_cfg, "agent_env_overrides", None)
            if isinstance(overrides, dict) and agent_id in overrides:
                removed += len(overrides[agent_id] or {})
                del overrides[agent_id]
            node_cfg.updated_at = __import__("time").time()
        except Exception as e:  # noqa: BLE001
            logger.warning("cleanup: mcp node purge failed: %s", e)

    # Persist the changes through whatever save path the manager uses.
    for saver in ("save_to_disk", "save", "_save"):
        fn = getattr(mgr, saver, None)
        if callable(fn):
            try:
                fn()
                break
            except Exception as e:  # noqa: BLE001
                logger.debug("cleanup: mcp save %s failed: %s", saver, e)
                continue
    return removed


# ── V1 skills registry ───────────────────────────────────────────────


def _purge_skills(agent_id: str) -> int:
    """Revoke every skill grant this agent had.

    Returns the number of (skill, agent) grants revoked.
    """
    try:
        from .skills import engine as _sk
    except ImportError:
        return 0

    try:
        reg = _sk.get_registry()
    except Exception as e:  # noqa: BLE001
        logger.warning("cleanup: skill registry unavailable: %s", e)
        return -1

    try:
        grants = reg.list_for_agent(agent_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("cleanup: list_for_agent failed: %s", e)
        return -1

    removed = 0
    for inst in grants:
        try:
            if reg.revoke(inst.manifest.id, agent_id):
                removed += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("cleanup: skill revoke failed: %s", e)
    return removed


# ── V2 tasks / events / attachments ──────────────────────────────────


def _purge_v2(agent_id: str) -> int:
    """Delete the agent's V2 tasks + their event rows + attachments dir.

    V2 SQLite lives at ``~/.tudou_claw/tudou.db`` (V1 uses the
    separate file ``tudou_claw.db``). We run direct SQL here because
    we want to delete in bulk and the store doesn't expose a cascade
    method.

    Returns rows deleted across ``tasks_v2`` + ``task_events_v2``.
    """
    total = 0
    try:
        from .v2.core.task_store import get_store
        store = get_store()
    except Exception:
        return 0

    try:
        with store._connect() as conn:  # type: ignore[attr-defined]
            # Gather task ids first so we can cascade to events.
            ids = [r["id"] for r in conn.execute(
                "SELECT id FROM tasks_v2 WHERE agent_id = ?", (agent_id,),
            ).fetchall()]
            if ids:
                # Delete events in batches of 500 to dodge the SQLite
                # param-count limit.
                for i in range(0, len(ids), 500):
                    batch = ids[i:i + 500]
                    qmarks = ",".join("?" for _ in batch)
                    r = conn.execute(
                        f"DELETE FROM task_events_v2 WHERE task_id IN ({qmarks})",
                        batch,
                    )
                    total += r.rowcount or 0
                r = conn.execute(
                    "DELETE FROM tasks_v2 WHERE agent_id = ?", (agent_id,),
                )
                total += r.rowcount or 0
            # The V2 agent row itself is removed by the caller (so
            # ``archive_agent`` stays possible as a soft-delete mode).
    except Exception as e:  # noqa: BLE001
        logger.warning("cleanup: v2 purge failed: %s", e)
        return -1
    return total


# ── Workspace directory ───────────────────────────────────────────────


def _purge_workspace(agent_id: str) -> int:
    """Remove agent workspace + V2 attachments directory.

    Returns 1 if a directory was removed, 0 otherwise.
    """
    removed_any = False
    for base_env in ("TUDOU_CLAW_DATA_DIR",):
        try:
            from . import DEFAULT_DATA_DIR
            base = os.environ.get(base_env) or DEFAULT_DATA_DIR
        except Exception:
            continue
        for sub in ("workspaces", f"v2/agents/{agent_id}"):
            candidate = os.path.normpath(os.path.join(base, sub, agent_id if sub == "workspaces" else ""))
            if sub != "workspaces":
                # v2/agents/<agent_id> already includes the id
                candidate = os.path.normpath(os.path.join(base, sub))
            if os.path.isdir(candidate):
                try:
                    shutil.rmtree(candidate, ignore_errors=False)
                    removed_any = True
                except Exception as e:  # noqa: BLE001
                    logger.warning("cleanup: rm %s failed: %s", candidate, e)
    return 1 if removed_any else 0


__all__ = ["purge_agent"]
