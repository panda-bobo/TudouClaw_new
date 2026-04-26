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


# ── Tunables (all in chars) ─────────────────────────────────────────
# Centralised so truncation limits don't drift between call sites.
_TOOL_ARGS_PREVIEW_CHARS      = 160
_TOOL_RESULT_PREVIEW_CHARS    = 200
_LAST_ASSISTANT_PREVIEW_CHARS = 200
# Recent resumable scan window used by _find_active_task. Keeping
# this small avoids scanning months of terminal rows on every event.
_FIND_ACTIVE_SCAN_LIMIT       = 5


def _summarize(s: str, n: int) -> str:
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
    active = store.list_for_agent(agent_id, include_terminal=False,
                                    limit=_FIND_ACTIVE_SCAN_LIMIT)
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
    preview = _summarize(content, _LAST_ASSISTANT_PREVIEW_CHARS)
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
        "arguments_preview": _summarize(str(args), _TOOL_ARGS_PREVIEW_CHARS),
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
                entry["result_preview"] = _summarize(result, _TOOL_RESULT_PREVIEW_CHARS)
                _get_store().save(task)
                return


# ── Terminal status ────────────────────────────────────────────────────


def mark_done(agent_id: str, chat_task_id: str = "",
              failed: bool = False) -> None:
    """Called when the underlying ChatTask closes. NOT a final terminal —
    we move to AWAITING_USER so the user gets to confirm "this task is
    really done" before flipping to DONE.

    On success, also writes:
      - L2 episodic memory (the WORKING memory: full task process —
        original request + all steps + tool counts + final state)
      - L3 task_log fact (a SUMMARY: "2026-04-25 完成 X 任务")

    Behaviour:
      - failure path → FAILED (terminal, but still resumable from banner)
      - success path → AWAITING_USER (waits for user to click "确认完成"
                       in the UI; the resume banner shows it as
                       "待确认完成" alongside paused tasks)
    """
    task = _find_active_task(agent_id, chat_task_id)
    if task is None:
        return
    task.status = (ConversationTaskStatus.FAILED if failed
                   else ConversationTaskStatus.AWAITING_USER)
    task.completed_at = time.time()
    # Close any lingering running step.
    for s in task.steps:
        if s.status == "running":
            s.status = "done" if not failed else "skipped"
            s.completed_at = time.time()
    _get_store().save(task)

    # ── L2 / L3 memory write-back (only on successful completion) ───
    # Failed tasks intentionally NOT written — keeps memory clean of
    # "I tried to X but failed" noise. User can resume from banner if
    # they want to retry.
    if failed:
        return
    try:
        _write_task_to_memory(task)
    except Exception as e:
        logger.debug("memory write-back skipped for task %s: %s",
                     task.id, e)


def _write_task_to_memory(task: ConversationTask) -> None:
    """Persist a completed ConversationTask to L2 (working) + L3 (summary).

    L2 EpisodicEntry: full task process — what was the request, what plan
    steps were executed, what tools were called. Useful for "remind me
    how I did X last time" lookups.

    L3 task_log fact: 1-line summary "2026-04-25 完成「Title」(N steps)"
    so the agent can answer "what tasks did I do recently" without scanning
    L2 in detail. task_log facts get higher priority than intent/rule but
    lower than contact/preference (see _CATEGORY_PRIORITY).
    """
    try:
        from .core.memory import (
            get_memory_manager, EpisodicEntry, SemanticFact,
        )
    except Exception as e:
        logger.debug("memory module not available: %s", e)
        return
    mm = get_memory_manager()
    if mm is None:
        return

    # Build human-readable summary of the work done
    done_steps = [s for s in (task.steps or []) if s.status == "done"]
    skipped_steps = [s for s in (task.steps or []) if s.status == "skipped"]

    # Collect tool usage stats across all steps
    tool_count_by_name: dict = {}
    for s in (task.steps or []):
        for tc in (s.tool_calls or []):
            n = tc.get("name", "?")
            tool_count_by_name[n] = tool_count_by_name.get(n, 0) + 1
    tools_used = sorted(tool_count_by_name.items(),
                         key=lambda kv: -kv[1])[:8]

    # ── L2: episodic working memory ─────────────────────────────────
    summary_parts = [
        f"[任务: {task.title or task.intent[:40]}]",
        f"原始请求: {(task.intent or '')[:300]}",
    ]
    if done_steps:
        summary_parts.append(f"已完成 {len(done_steps)} 步:")
        for i, s in enumerate(done_steps[:10], 1):
            summary_parts.append(f"  {i}. {s.goal[:80]}")
        if len(done_steps) > 10:
            summary_parts.append(f"  ... 共 {len(done_steps)} 步")
    if skipped_steps:
        summary_parts.append(f"跳过/失败 {len(skipped_steps)} 步")
    if tools_used:
        tools_str = ", ".join(f"{n}×{c}" for n, c in tools_used)
        summary_parts.append(f"工具调用: {tools_str}")
    summary_text = "\n".join(summary_parts)

    keywords = []
    # First few words of title + main tools
    if task.title:
        keywords.extend(task.title.split()[:3])
    keywords.extend(n for n, _ in tools_used[:3])
    keywords_str = ",".join(set(keywords))[:200]

    try:
        ep = EpisodicEntry(
            agent_id=task.agent_id,
            summary=summary_text,
            keywords=keywords_str,
            turn_start=0,
            turn_end=len(task.steps or []),
            message_count=len(task.steps or []),
        )
        mm.save_episodic(ep)
        logger.info("L2 episodic written: agent=%s task=%s (%d chars)",
                    task.agent_id[:8], task.id, len(summary_text))
    except Exception as e:
        logger.debug("L2 save failed for task %s: %s", task.id, e)

    # ── L3: task_log summary fact ───────────────────────────────────
    # 1-line, human-readable, dated. Agent can recall "what did I do
    # on 2026-04-25" by querying L3 with category=task_log.
    try:
        date_str = time.strftime("%Y-%m-%d", time.localtime(task.completed_at))
        title = task.title or (task.intent or "")[:40]
        n_done = len(done_steps)
        n_total = len(task.steps or [])
        tools_short = ",".join(n for n, _ in tools_used[:3]) or "—"
        log_content = (
            f"[{date_str}] 完成「{title}」 "
            f"(步骤 {n_done}/{n_total}; 工具: {tools_short})"
        )
        fact = SemanticFact(
            agent_id=task.agent_id,
            category="task_log",
            content=log_content,
            source=f"conversation_task:{task.id}",
            confidence=0.95,    # 任务真完成了, 高置信度
        )
        mm.save_fact(fact)
        logger.info("L3 task_log written: agent=%s %s",
                    task.agent_id[:8], log_content[:80])
    except Exception as e:
        logger.debug("L3 task_log save failed for task %s: %s", task.id, e)
