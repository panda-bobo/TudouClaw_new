"""Lightweight per-agent todo list — borrowed from Claude Code's TodoWrite.

Unlike plan_update (project-step level) and create_milestone (project
checkpoint level), this is the agent's PRIVATE working list — what it
intends to do across the next few turns. Not persisted across process
restarts; lives on the agent instance only.

Why a separate tool instead of stuffing it into the prompt:
  - LLM tends to forget its own intent across context-compaction events.
  - Without a tool boundary, we have no way to log "agent updated its
    plan" as a discrete event for the timeline.
  - A typed list ≪ a free-text "remember to do X, Y, Z" sentence — the
    LLM-side model maintains structure better when it has to fill
    fields than when it free-paragraphs intent.

States:
  pending      — not started
  in_progress  — exactly one at a time (enforced)
  completed    — done; LLM should drop it next set if it wants

Fields per item:
  id          — short id, agent-chosen (e.g. "t1", "fix-cors")
  content     — what to do (≤200 chars)
  status      — pending | in_progress | completed
  activeForm  — gerund form for display ("Implementing X" vs "Implement X")
"""
from __future__ import annotations

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

_MAX_ITEMS = 20
_MAX_CONTENT_CHARS = 200
_VALID_STATUS = {"pending", "in_progress", "completed"}


def _get_agent_from_caller(caller_id: str):
    if not caller_id:
        return None
    try:
        import sys as _sys
        _llm_mod = _sys.modules.get("app.llm")
        hub = getattr(_llm_mod, "_active_hub", None) if _llm_mod else None
        if hub is None:
            return None
        return hub.agents.get(caller_id)
    except Exception:
        return None


def _normalize_todo(raw: dict, idx: int) -> tuple[dict | None, str]:
    """Return (normalized_item, error_msg). On error returns (None, msg)."""
    if not isinstance(raw, dict):
        return None, f"todo[{idx}]: must be a dict"
    content = (raw.get("content") or "").strip()
    if not content:
        return None, f"todo[{idx}]: 'content' is required"
    if len(content) > _MAX_CONTENT_CHARS:
        content = content[:_MAX_CONTENT_CHARS]
    status = (raw.get("status") or "pending").strip().lower()
    if status not in _VALID_STATUS:
        return None, (f"todo[{idx}]: invalid status {status!r}; "
                       f"must be one of {sorted(_VALID_STATUS)}")
    tid = (raw.get("id") or "").strip() or f"t{idx + 1}"
    active_form = (raw.get("activeForm") or "").strip() or content
    return {
        "id": tid,
        "content": content,
        "status": status,
        "activeForm": active_form[:_MAX_CONTENT_CHARS],
    }, ""


def _format_list(todos: list) -> str:
    if not todos:
        return "(empty — no todos)"
    icon = {"pending": "○", "in_progress": "◐", "completed": "●"}
    lines = []
    for t in todos:
        i = icon.get(t.get("status", "pending"), "?")
        tid = t.get("id", "")
        content = t.get("content", "")
        lines.append(f"  {i} [{tid}] {content}")
    return "\n".join(lines)


def _tool_agent_todo(
    action: str = "get",
    todos: list = None,
    todo_id: str = "",
    status: str = "",
    **kwargs: Any,
) -> str:
    """Manage the calling agent's private todo list.

    action='get'         — return current list, no mutation.
    action='set'         — REPLACE the whole list with `todos=[...]`.
                           Use this for the initial plan or after a
                           significant pivot. Each item: {id?, content,
                           status?, activeForm?}. Max 20 items.
    action='update_one'  — change one item's status. Requires todo_id +
                           status. Use when a step starts / completes /
                           is dropped — keeps the list stable across
                           turns instead of re-emitting the full set.
    action='clear'       — empty the list (e.g. after task complete).

    Constraint: at most ONE item may be in_progress at a time. Setting
    a second one returns an error so the LLM has to either complete
    the current one or downgrade it.
    """
    caller_id = (kwargs.get("_caller_agent_id") or "").strip()
    agent = _get_agent_from_caller(caller_id)
    if agent is None:
        return ("Error: agent_todo requires a caller agent context. "
                "(Tool dispatcher should pass _caller_agent_id; if you're "
                "calling outside of agent chat, this tool isn't available.)")

    # Lazy-init the per-agent todo list. Stored on the live Agent
    # instance — survives intra-process for the agent's lifetime, lost
    # on process restart (matches "scratch list" semantics).
    current: list = getattr(agent, "_agent_todo_list", None) or []
    action = (action or "get").strip().lower()

    if action == "get":
        return f"Current todos ({len(current)}):\n{_format_list(current)}"

    if action == "clear":
        agent._agent_todo_list = []
        return "Todos cleared."

    if action == "set":
        if not isinstance(todos, list):
            return "Error: 'todos' must be a list when action='set'."
        if len(todos) > _MAX_ITEMS:
            return (f"Error: too many todos ({len(todos)}); cap is {_MAX_ITEMS}. "
                    f"Trim down to the next batch.")
        normalized: list = []
        in_progress_count = 0
        errors: list = []
        for i, raw in enumerate(todos):
            norm, err = _normalize_todo(raw, i)
            if err:
                errors.append(err)
                continue
            if norm["status"] == "in_progress":
                in_progress_count += 1
            normalized.append(norm)
        if in_progress_count > 1:
            return (f"Error: {in_progress_count} items marked in_progress. "
                    "Only ONE may be in_progress at a time. Demote the others "
                    "to pending or mark them completed.")
        if errors:
            return "Error(s) parsing todos:\n  - " + "\n  - ".join(errors)
        agent._agent_todo_list = normalized
        try:
            agent._log("agent_todo_set", {"count": len(normalized),
                                           "ts": time.time()})
        except Exception:
            pass
        return (f"Todos updated ({len(normalized)} items):\n"
                + _format_list(normalized))

    if action == "update_one":
        if not todo_id:
            return "Error: 'todo_id' is required for update_one."
        new_status = (status or "").strip().lower()
        if new_status not in _VALID_STATUS:
            return (f"Error: invalid status {status!r}; "
                    f"must be one of {sorted(_VALID_STATUS)}.")
        target = None
        for t in current:
            if t.get("id") == todo_id:
                target = t
                break
        if target is None:
            ids = [t.get("id", "") for t in current]
            return (f"Error: no todo with id={todo_id!r}. "
                    f"Current ids: {ids}")
        if new_status == "in_progress":
            for t in current:
                if t.get("id") != todo_id and t.get("status") == "in_progress":
                    return (f"Error: another todo {t.get('id')!r} is already "
                            f"in_progress. Complete or pause it before starting "
                            f"a new one.")
        target["status"] = new_status
        agent._agent_todo_list = current  # re-anchor (in case lazy init was stale)
        try:
            agent._log("agent_todo_update", {"id": todo_id, "status": new_status})
        except Exception:
            pass
        return (f"Updated [{todo_id}] → {new_status}\n" + _format_list(current))

    return (f"Error: unknown action {action!r}. "
            "Use one of: get / set / update_one / clear.")
