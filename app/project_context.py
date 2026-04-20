"""Thread-local project context.

Mirror of ``app/meeting_context.py`` — lets tool handlers (e.g.
``submit_deliverable``, ``create_goal``, ``create_milestone``) discover the
current project id without threading ``project_id`` through every LLM-visible
tool signature.

Usage (producer side, in ``project.py::ProjectChatEngine._agent_respond``)::

    from .project_context import set_project_context
    set_project_context(project.id)
    try:
        result = agent.chat(...)
    finally:
        set_project_context("")

Usage (consumer side, in a tool handler)::

    from .project_context import get_project_context
    pid = get_project_context()
    if pid:
        proj = hub.get_project(pid)
        proj.add_deliverable(...)

Note: project and meeting contexts are independent. An agent can be inside
both simultaneously (e.g. a meeting discussing a specific project); tools
check each scope separately.
"""
from __future__ import annotations

import threading

_tl = threading.local()


def set_project_context(project_id: str) -> None:
    """Set the current thread's project id. Empty string clears it."""
    _tl.project_id = project_id or ""


def get_project_context() -> str:
    """Return the current thread's project id, or empty string if not in a project."""
    return getattr(_tl, "project_id", "") or ""
