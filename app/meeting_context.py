"""Thread-local meeting context.

Lets tool handlers (e.g. ``task_update``) detect that they are being called
from within a meeting reply loop, without mutating the shared Agent object.

Usage (producer side, in ``meeting.py::meeting_agent_reply``)::

    from .meeting_context import set_meeting_context
    set_meeting_context(meeting.id)
    try:
        reply = agent_chat_fn(aid, chat_msg)
    finally:
        set_meeting_context("")

Usage (consumer side, in a tool handler)::

    from .meeting_context import get_meeting_context
    mid = get_meeting_context()
    if mid:
        # route task to StandaloneTaskRegistry with source_meeting_id=mid
        ...
"""
from __future__ import annotations

import threading

_tl = threading.local()


def set_meeting_context(meeting_id: str) -> None:
    """Set the current thread's meeting id. Empty string clears it."""
    _tl.meeting_id = meeting_id or ""


def get_meeting_context() -> str:
    """Return the current thread's meeting id, or empty string if not in a meeting."""
    return getattr(_tl, "meeting_id", "") or ""
