"""Observer that turns V1 chat events into ConversationTask mutations.

Flow
----
``send_chat`` creates a ``ConversationTask`` when classifier decides the
message is complex (see M1). From that point we want the existing V1
chat loop to drive execution while an observer *watches* its event
stream and annotates the task row.

Events of interest (emitted by ``agent.AgentEvent`` / ``_log``):

    kind="message"    role=assistant, content=...
        — candidate for plan extraction (first time) and step-complete
          markers (every time).

    kind="tool_call"  data.name, data.arguments
        — attach to the current step; increment tool_call_total.

    kind="tool_result"  data.name, data.result
        — enrich the most-recent tool_call entry with a short result
          preview.

    kind="status" or terminal signal
        — mark task done / failed.

Attachment model
----------------
Observer is stateless per agent; it looks up the active
ConversationTask by ``agent_id`` + (optionally) ``chat_task_id`` on
every event and persists via ``get_store().save(...)``. This is
slightly noisier than keeping in-memory state, but guarantees crash
safety — the DB row is always at most one event behind.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from .conversation_task import (
    ConversationStep, ConversationTask, ConversationTaskStatus,
    get_store as _get_store,
)
from .conversation_plan_parser import (
    extract_plan, find_completed_step_markers,
)


logger = logging.getLogger("tudou.conversation_observer")


def _summarize(s: str, n: int = 160) -> str:
    """Shrink a string to n chars, adding an ellipsis if truncated."""
    if not isinstance(s, str):
        s = str(s)
    s = s.strip()
    return s if len(s) <= n else s[:n] + "…"


def _find_active_task(agent_id: str,
                      chat_task_id: str = "") -> Optional[ConversationTask]:
    """Return the conversation task this event belongs to, or None.

    Strategy:
      1. Prefer exact match on chat_task_id (what send_chat stored).
      2. Else, newest RUNNING task for the agent.
    """
    store = _get_store()
    if chat_task_id:
        # Scan running+paused first (cheapest); fall back to terminal if
        # the chat task closed and we're enriching late.
        for t in store.list_resumable(agent_id):
            if t.chat_task_id == chat_task_id:
                return t
    # Fallback: newest RUNNING task for this agent.
    active = store.list_for_agent(agent_id, include_terminal=False, limit=5)
    for t in active:
        if t.status == ConversationTaskStatus.RUNNING:
            return t
    return None


# ── Public hook ────────────────────────────────────────────────────────


def on_agent_event(agent_id: str, event: dict,
                   chat_task_id: str = "") -> None:
    """Called once per AgentEvent dict. Best-effort; swallows exceptions.

    ``event`` shape is the serialized AgentEvent:
        {"timestamp": float, "kind": str, "data": {...}}

    For chat events we care about kind in {"message", "tool_call",
    "tool_result"}. Everything else is ignored.
    """
    try:
        kind = event.get("kind") or ""
        data = event.get("data") or {}
        if kind == "message":
            _on_message(agent_id, data, chat_task_id)
        elif kind == "tool_call":
            _on_tool_call(agent_id, data, chat_task_id)
        elif kind == "tool_result":
            _on_tool_result(agent_id, data, chat_task_id)
    except Exception as e:   # noqa: BLE001
        logger.debug("conversation_observer skipped event: %s", e)


# ── Handlers ───────────────────────────────────────────────────────────


def _on_message(agent_id: str, data: dict, chat_task_id: str) -> None:
    role = data.get("role") or ""
    content = data.get("content") or ""
    if role != "assistant" or not isinstance(content, str) or not content:
        return
    task = _find_active_task(agent_id, chat_task_id)
    if task is None:
        return

    dirty = False

    # 1. Plan extraction (only if we don't have steps yet).
    if not task.steps:
        parsed = extract_plan(content)
        if parsed:
            task.steps = [
                ConversationStep(id=s.id, goal=s.goal, tool_hint=s.tool_hint,
                                 status="pending")
                for s in parsed
            ]
            if task.steps:
                task.steps[0].status = "running"
                task.steps[0].started_at = time.time()
            task.current_step_idx = 0
            logger.info(
                "ConversationTask %s: plan extracted — %d steps",
                task.id, len(task.steps))
            dirty = True

    # 2. Step-complete markers.
    if task.steps:
        done_nums = find_completed_step_markers(content)
        for n in done_nums:
            idx = n - 1
            if 0 <= idx < len(task.steps) and task.steps[idx].status != "done":
                task.steps[idx].status = "done"
                task.steps[idx].completed_at = time.time()
                # Advance to the next pending step.
                if task.current_step_idx <= idx:
                    nxt = idx + 1
                    task.current_step_idx = nxt
                    if nxt < len(task.steps):
                        if task.steps[nxt].status == "pending":
                            task.steps[nxt].status = "running"
                            task.steps[nxt].started_at = time.time()
                dirty = True

    # 3. last_assistant_preview for the UI.
    preview = _summarize(content, 200)
    if preview != task.last_assistant_preview:
        task.last_assistant_preview = preview
        dirty = True

    if dirty:
        _get_store().save(task)


def _on_tool_call(agent_id: str, data: dict, chat_task_id: str) -> None:
    task = _find_active_task(agent_id, chat_task_id)
    if task is None:
        return
    name = str(data.get("name") or "").strip()
    if not name:
        return
    args = data.get("arguments") or data.get("args") or {}
    entry = {
        "name": name,
        "arguments_preview": _summarize(str(args), 160),
        "result_preview": "",
        "ts": time.time(),
    }

    # Choose which step owns this tool call:
    #   1. current_step_idx if its status is running
    #   2. first step whose tool_hint matches the tool name
    #   3. else last step (best-effort)
    idx = task.current_step_idx
    if idx < 0 or idx >= len(task.steps):
        idx = len(task.steps) - 1 if task.steps else -1

    if task.steps:
        # Prefer matching tool_hint
        for i, s in enumerate(task.steps):
            if s.tool_hint == name and s.status != "done":
                idx = i
                # If this is ahead of current_step_idx, also advance.
                if i > task.current_step_idx:
                    # Mark skipped steps as done? No — the agent may
                    # come back. Just update pointer conservatively.
                    task.current_step_idx = i
                if s.status == "pending":
                    s.status = "running"
                    s.started_at = time.time()
                break

        if 0 <= idx < len(task.steps):
            task.steps[idx].tool_calls.append(entry)

    task.tool_call_total += 1
    _get_store().save(task)


def _on_tool_result(agent_id: str, data: dict, chat_task_id: str) -> None:
    task = _find_active_task(agent_id, chat_task_id)
    if task is None or not task.steps:
        return
    name = str(data.get("name") or "").strip()
    result = data.get("result") or data.get("content") or ""
    if not isinstance(result, str):
        try:
            import json
            result = json.dumps(result, ensure_ascii=False)
        except Exception:
            result = str(result)

    # Find the most recent tool_call entry (any step) matching ``name``
    # that still has empty result_preview.
    for step in reversed(task.steps):
        for entry in reversed(step.tool_calls):
            if entry.get("name") == name and not entry.get("result_preview"):
                entry["result_preview"] = _summarize(result, 200)
                _get_store().save(task)
                return


# ── Terminal status ────────────────────────────────────────────────────


def mark_done(agent_id: str, chat_task_id: str = "",
              failed: bool = False) -> None:
    """Flip the task to DONE / FAILED. Called when the chat-task
    manager reports the underlying ChatTask closed."""
    task = _find_active_task(agent_id, chat_task_id)
    if task is None:
        return
    task.status = (ConversationTaskStatus.FAILED if failed
                   else ConversationTaskStatus.DONE)
    task.completed_at = time.time()
    # Close any lingering running step.
    for s in task.steps:
        if s.status == "running":
            s.status = "done" if not failed else "skipped"
            s.completed_at = time.time()
    _get_store().save(task)
