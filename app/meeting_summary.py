"""Meeting summary — derives a compact context block from the in-memory
meeting registry. No persistence, no schema changes — meetings are
ephemeral by design.

Used by the agent dynamic-context injector to give in-meeting agents a
concise view of their meeting state ("3 active members, 2 open
assignments, last activity 30s ago") without polluting the static
system prompt.

Output is markdown text, capped at ~600 tokens / 2400 chars.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger("tudou.meeting_summary")


def _meeting_registry():
    """Lazy import — meeting subsystem may not be loaded in some contexts."""
    try:
        from .api.deps.hub import get_hub
        hub = get_hub()
        return getattr(hub, "meeting_registry", None)
    except Exception:
        return None


def _agent_label(agent_id: str) -> str:
    """Try to resolve agent_id → 'role-name' for nicer display."""
    try:
        from .api.deps.hub import get_hub
        hub = get_hub()
        if hasattr(hub, "get_agent"):
            a = hub.get_agent(agent_id)
            if a is not None:
                return f"{a.role}-{a.name}" if a.role else a.name
    except Exception:
        pass
    return agent_id[:12]


def meeting_summary_markdown(
    meeting_id: str, *,
    viewer_agent_id: str = "",
    max_chars: int = 1800,
) -> str:
    """Return a compact markdown block for the agent's dynamic context.

    Args:
        meeting_id: target meeting
        viewer_agent_id: if set, the per-agent personalisation
                         ("assignments to me", "questions to me") shows
                         only the viewer's slice.
        max_chars: hard cap on returned length

    Returns "" when meeting not found / inactive / no useful state.
    """
    if not meeting_id:
        return ""
    reg = _meeting_registry()
    if reg is None or not hasattr(reg, "get_meeting"):
        return ""
    try:
        meeting = reg.get_meeting(meeting_id)
    except Exception:
        meeting = None
    if meeting is None:
        return ""

    status = getattr(meeting, "status", None)
    status_v = getattr(status, "value", status) or "?"

    lines = [f"[当前会议状态 · {meeting.title or meeting_id[:8]}]"]
    lines.append(f"- 状态: {status_v} · 主持: {_agent_label(meeting.host)}")

    # Members
    parts = (meeting.participants or [])
    if parts:
        labels = [_agent_label(p) for p in parts[:8]]
        more = "" if len(parts) <= 8 else f" 等 {len(parts)} 人"
        lines.append(f"- 成员 ({len(parts)}): {', '.join(labels)}{more}")

    # Agenda (one liner)
    if (meeting.agenda or "").strip():
        ag = meeting.agenda.strip().replace("\n", " ")[:120]
        lines.append(f"- 议题: {ag}")

    # Recent messages (3 most recent, brief)
    msgs = getattr(meeting, "messages", []) or []
    if msgs:
        recent = msgs[-3:]
        lines.append("\n[最近发言]")
        for m in recent:
            sender = getattr(m, "sender", "") or getattr(m, "from_agent", "") or "?"
            content = getattr(m, "content", "") or getattr(m, "text", "") or ""
            content = (content or "").replace("\n", " ").strip()[:80]
            sender_label = _agent_label(sender) if sender != "user" else "user"
            if content:
                lines.append(f"- {sender_label}: {content}")

    # Assignments — open ones, with viewer personalisation
    asgs = getattr(meeting, "assignments", []) or []
    open_asgs = [
        a for a in asgs
        if (getattr(getattr(a, "status", None), "value",
                    getattr(a, "status", "")) or "open") == "open"
    ]
    if open_asgs:
        # If we have a viewer agent, separate "mine" vs "others"
        if viewer_agent_id:
            mine = [a for a in open_asgs
                    if (getattr(a, "assignee_id", "") or
                        getattr(a, "assigned_to", "")) == viewer_agent_id]
            others = [a for a in open_asgs if a not in mine]
            if mine:
                lines.append("\n[给我的待办]")
                for a in mine[:5]:
                    desc = (getattr(a, "description", "") or
                            getattr(a, "title", "") or "").replace("\n", " ")[:120]
                    lines.append(f"- {desc}")
            if others:
                lines.append(f"\n[其他成员的待办: {len(others)}]")
        else:
            lines.append(f"\n[未完成 assignment: {len(open_asgs)}]")
            for a in open_asgs[:3]:
                assignee = (getattr(a, "assignee_id", "") or
                           getattr(a, "assigned_to", "") or "?")
                desc = (getattr(a, "description", "") or
                        getattr(a, "title", "") or "").replace("\n", " ")[:80]
                lines.append(f"- [{_agent_label(assignee)}] {desc}")

    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars - 20] + "\n…(truncated)"
    return text
