"""Shared-context tools — agents query/write the project shared
context database instead of pushing content through messages.

Five tools:
  * ``sc_query``           — generic SELECT over sc_* tables with filters
  * ``sc_register_artifact`` — record a workspace file as a referencable artifact
  * ``sc_get_artifact``    — fetch full record of an artifact id
  * ``sc_record_decision`` — append a structured decision
  * ``sc_handoff``         — write a handoff row (pull-model: dst pulls)

Project resolution mirrors the ``project.py`` pattern: explicit
``project_id`` arg → ``_project_id`` kwarg snapshot → thread-local
``get_project_context()``. Agent attribution from ``_caller_agent_id``.

All tools return strings (the LLM-facing tool result). Errors come back
as ``"Error: ..."`` strings — agents are expected to read and react.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from ._common import _get_hub

logger = logging.getLogger(__name__)


# ── helpers ───────────────────────────────────────────────────────────
def _resolve_pid_aid(project_id: str, kwargs: dict) -> tuple[str, str]:
    """Return (project_id, agent_id), best-effort. Empty strings when not
    resolvable."""
    pid = (project_id or "").strip()
    if not pid and kwargs:
        pid = (kwargs.get("_project_id") or "").strip()
    if not pid:
        try:
            from ..project_context import get_project_context
            pid = (get_project_context() or "").strip()
        except Exception:
            pid = ""
    aid = ""
    if kwargs:
        aid = (kwargs.get("_caller_agent_id") or "").strip()
    return pid, aid


def _store():
    from ..shared_context import get_shared_context_store
    return get_shared_context_store()


def _err(msg: str) -> str:
    return f"Error: {msg}"


# ── sc_query ──────────────────────────────────────────────────────────
def _tool_sc_query(table: str = "", project_id: str = "",
                   kind: str = "", status: str = "",
                   dst_agent: str = "", since_ts: float = 0,
                   limit: int = 10, **kwargs) -> str:
    """Query rows from any sc_* table.

    Supported tables: artifacts, decisions, milestones, handoffs,
    pending_qs. Filters apply per-table (kind for artifacts;
    status+dst_agent for handoffs; status for milestones; etc.).
    Returns JSON list of rows.
    """
    pid, aid = _resolve_pid_aid(project_id, kwargs)
    if not pid:
        return _err("no project context — pass project_id or call from a project chat")
    try:
        s = _store()
        table_norm = (table or "").strip().lower()
        limit = max(1, min(int(limit or 10), 50))
        if table_norm == "artifacts":
            rows = s.list_artifacts(
                project_id=pid, kind=kind or "", status=status or "active",
                since_ts=float(since_ts or 0), limit=limit,
            )
        elif table_norm == "decisions":
            rows = s.list_decisions(
                project_id=pid, status=status or "final",
                since_ts=float(since_ts or 0), limit=limit,
            )
        elif table_norm == "milestones":
            rows = s.list_milestones(
                project_id=pid, status=status or "", limit=limit,
            )
        elif table_norm == "handoffs":
            rows = s.list_handoffs(
                project_id=pid, dst_agent=dst_agent or "",
                status=status or "pending",
                since_ts=float(since_ts or 0), limit=limit,
            )
        elif table_norm == "pending_qs":
            rows = s.list_pending_questions(
                project_id=pid, asked_to=dst_agent or "",
                status=status or "open", limit=limit,
            )
        elif table_norm == "summary" or not table_norm:
            # Convenience: return the project-state summary
            return s.project_summary_markdown(pid)
        else:
            return _err(f"unknown table {table!r}; valid: artifacts | decisions | milestones | handoffs | pending_qs | summary")
        return json.dumps({"table": table_norm, "count": len(rows), "rows": rows},
                          ensure_ascii=False, default=str)
    except Exception as e:
        logger.exception("sc_query failed")
        return _err(f"sc_query: {e}")


# ── sc_register_artifact ──────────────────────────────────────────────
def _tool_sc_register_artifact(path: str = "", title: str = "",
                                summary: str = "", kind: str = "document",
                                token_count: int = 0,
                                project_id: str = "", **kwargs) -> str:
    """Record a workspace file as a sharable artifact reference.

    Other agents can then look it up via sc_query(table='artifacts')
    or sc_get_artifact(id). Pass ``summary`` ≤ 200 chars; the full
    file stays in the workspace, only the *card* lives in the DB.
    """
    pid, aid = _resolve_pid_aid(project_id, kwargs)
    if not pid:
        return _err("no project context")
    if not path:
        return _err("path required")
    if not aid:
        aid = "unknown"
    try:
        artifact_id = _store().register_artifact(
            project_id=pid, agent_id=aid, path=path,
            title=title or path, summary=summary or "",
            kind=kind or "document", token_count=int(token_count or 0),
        )
        return f"OK · registered artifact id={artifact_id}"
    except Exception as e:
        logger.exception("sc_register_artifact failed")
        return _err(f"sc_register_artifact: {e}")


# ── sc_get_artifact ───────────────────────────────────────────────────
def _tool_sc_get_artifact(artifact_id: str = "", **kwargs) -> str:
    """Fetch the full record of an artifact (path, title, summary, etc).

    Use this when you need to know what an ``art_*`` reference points to
    before deciding whether to read the underlying file.
    """
    if not artifact_id:
        return _err("artifact_id required")
    try:
        rec = _store().get_artifact(artifact_id)
        if not rec:
            return _err(f"artifact not found: {artifact_id}")
        return json.dumps(rec, ensure_ascii=False, default=str)
    except Exception as e:
        return _err(f"sc_get_artifact: {e}")


# ── sc_record_decision ────────────────────────────────────────────────
def _tool_sc_record_decision(topic: str = "", decision: str = "",
                              rationale: str = "",
                              supersedes_id: str = "",
                              project_id: str = "", **kwargs) -> str:
    """Append a structured decision to the project's decision log.

    Topic = what was being decided, decision = the chosen answer,
    rationale = why. Keeps team-wide decisions out of chat history and
    queryable later via sc_query(table='decisions').
    """
    pid, aid = _resolve_pid_aid(project_id, kwargs)
    if not pid:
        return _err("no project context")
    if not topic or not decision:
        return _err("topic and decision required")
    if not aid:
        aid = "unknown"
    try:
        did = _store().record_decision(
            project_id=pid, topic=topic, decision=decision,
            decided_by=aid, rationale=rationale or "",
            supersedes_id=supersedes_id or "",
        )
        return f"OK · decision recorded id={did}"
    except Exception as e:
        logger.exception("sc_record_decision failed")
        return _err(f"sc_record_decision: {e}")


# ── sc_handoff ────────────────────────────────────────────────────────
def _tool_sc_handoff(dst_agent: str = "", intent: str = "",
                      summary: str = "",
                      artifact_refs: Any = None,
                      project_id: str = "", **kwargs) -> str:
    """Pull-model handoff to another agent — writes a row to the
    handoffs table instead of pushing into dst's messages.

    The receiver discovers it via sc_query(table='handoffs',
    dst_agent='self', status='pending'). Pass artifact_refs as a list
    of ``art_*`` ids to point them at the relevant produced files.
    """
    pid, aid = _resolve_pid_aid(project_id, kwargs)
    if not pid:
        return _err("no project context")
    if not dst_agent or not intent:
        return _err("dst_agent and intent required")
    if not aid:
        aid = "unknown"
    refs: list[str] = []
    if isinstance(artifact_refs, list):
        refs = [str(x) for x in artifact_refs if x]
    elif isinstance(artifact_refs, str) and artifact_refs.strip():
        # Tolerate JSON-string or comma-separated forms (LLMs love both)
        try:
            parsed = json.loads(artifact_refs)
            if isinstance(parsed, list):
                refs = [str(x) for x in parsed if x]
        except Exception:
            refs = [s.strip() for s in artifact_refs.split(",") if s.strip()]
    try:
        hid = _store().write_handoff(
            project_id=pid, src_agent=aid, dst_agent=dst_agent,
            intent=intent, summary=summary or "",
            artifact_refs=refs,
        )
        return f"OK · handoff written id={hid}, refs={refs}"
    except Exception as e:
        logger.exception("sc_handoff failed")
        return _err(f"sc_handoff: {e}")
