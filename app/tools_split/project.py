"""Project-management tools — deliverables / goals / milestones.

Five tools that all operate on a Project instance resolved from either
an explicit ``project_id`` argument, a ``_project_id`` snapshot kwarg
injected by the dispatcher, or the thread-local project context set by
the project/meeting chat engines.

Also owns the project-scope helper functions (``_get_current_scope``,
``_resolve_project``, ``_save_projects_silently``) used only by this
category's tools. ``_get_current_scope`` is re-exported from
``app.tools`` for backwards compat with ``agent.py`` /
``agent_execution.py``.
"""
from __future__ import annotations

import logging
from typing import Any

from ._common import _get_hub

logger = logging.getLogger(__name__)


# submit_deliverable: give filename slugs a hard cap so runaway titles
# don't produce 10 kB path segments.
_SLUG_MAX_CHARS = 80

# When deconflicting filenames (a.md → a_2.md → a_3.md), give up after
# this many attempts and fall back to overwriting.
_UNIQUE_SUFFIX_MAX = 1000


# ── scope + project resolution helpers ───────────────────────────────

def _get_current_scope() -> dict:
    """Return the current thread's agent-scope context.

    Reads both project and meeting thread-local contexts (set by
    ``ProjectChatEngine._agent_respond`` and the meeting equivalent).
    Either field may be empty if the caller is not inside that scope.

    Returns:
        {"project_id": str, "meeting_id": str}
    """
    try:
        from ..project_context import get_project_context
        pid = get_project_context()
    except Exception:
        pid = ""
    try:
        from ..meeting_context import get_meeting_context
        mid = get_meeting_context()
    except Exception:
        mid = ""
    return {"project_id": pid or "", "meeting_id": mid or ""}


def _resolve_project(project_id: str = "",
                     kwargs: dict | None = None) -> tuple[Any, str]:
    """Resolve a Project instance from explicit id, injected kwarg, or thread-local.

    Resolution order:
      1. explicit ``project_id`` argument
      2. ``_project_id`` snapshot in kwargs (set by dispatcher — survives
         ThreadPoolExecutor handoff)
      3. thread-local ``get_project_context()`` (works on sequential path)

    Returns (project, error_message). If project is None, error_message
    explains why (for surfacing back to the LLM).
    """
    pid = (project_id or "").strip()
    if not pid and kwargs:
        pid = (kwargs.get("_project_id") or "").strip()
    if not pid:
        pid = _get_current_scope().get("project_id", "")
    if not pid:
        return None, (
            "Error: no project context. Call this tool from within a project "
            "chat, or pass project_id explicitly."
        )
    try:
        hub = _get_hub()
        proj = hub.get_project(pid) if hasattr(hub, "get_project") else None
        if proj is None:
            return None, f"Error: project not found: {pid}"
        return proj, ""
    except Exception as e:
        return None, f"Error: failed to resolve project {pid}: {e}"


def _save_projects_silently() -> None:
    """Persist projects to disk; swallow errors (best-effort)."""
    try:
        hub = _get_hub()
        save_fn = getattr(hub, "_save_projects", None)
        if callable(save_fn):
            save_fn()
    except Exception as e:
        logger.debug("_save_projects_silently failed: %s", e)


# ── submit_deliverable ───────────────────────────────────────────────

def _tool_submit_deliverable(title: str = "", file_path: str = "",
                              content_text: str = "", url: str = "",
                              kind: str = "document",
                              milestone_id: str = "",
                              task_id: str = "",
                              project_id: str = "",
                              **_: Any) -> str:
    """Explicitly register a deliverable for the current project.

    If content_text is provided without file_path, the content is written
    to a file under the project's shared workspace
    (~/.tudou_claw/workspaces/shared/<project_id>/) and the resulting
    path is recorded on the deliverable. This guarantees every textual
    deliverable physically exists in the canonical project directory.
    """
    if not title:
        return "Error: 'title' is required."
    if not (file_path or content_text or url):
        return "Error: one of file_path / content_text / url is required."
    proj, err = _resolve_project(project_id,
                                 kwargs=_ if isinstance(_, dict) else None)
    if err:
        return err
    caller_id = _.get("_caller_agent_id", "") if isinstance(_, dict) else ""

    resolved_file_path = (file_path or "").strip()

    # ── Ensure the deliverable physically lives under the project's shared
    # workspace (~/.tudou_claw/workspaces/shared/<project_id>/). The
    # Deliverables UI only scans the shared dir, so anything outside it is
    # invisible to the rest of the team. Two code paths:
    #   1) content_text without file_path  → write content to shared dir
    #   2) file_path outside shared dir    → copy file/folder into shared dir
    try:
        import os as _os
        import re as _re
        import shutil as _shutil
        from ..agent import Agent as _Agent

        shared_dir = _Agent.get_shared_workspace_path(proj.id)
        _os.makedirs(shared_dir, exist_ok=True)
        shared_real = _os.path.realpath(shared_dir)

        def _slug(raw: str, default: str = "deliverable") -> str:
            s = _re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", (raw or "").strip())
            s = s.strip(" .") or default
            return s[:_SLUG_MAX_CHARS]

        def _unique(target: str) -> str:
            if not _os.path.exists(target):
                return target
            stem, ext = _os.path.splitext(target)
            for n in range(2, _UNIQUE_SUFFIX_MAX):
                cand = f"{stem}_{n}{ext}"
                if not _os.path.exists(cand):
                    return cand
            return target  # give up; caller will overwrite

        # Path 1: content_text → new file in shared dir.
        if content_text and not resolved_file_path:
            ext_by_kind = {
                "document": ".md", "analysis": ".md", "report": ".md",
                "design": ".md", "spec": ".md", "plan": ".md",
                "media": ".txt", "code": ".txt",
            }
            ext = ext_by_kind.get(
                (kind or "document").strip().lower(), ".md")
            target = _unique(_os.path.join(
                shared_dir, f"{_slug(title)}{ext}"))
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(content_text)
            resolved_file_path = target
            logger.info(
                "submit_deliverable: materialized content_text → %s", target)

        # Path 2: file_path exists and is outside the shared dir → copy in.
        elif resolved_file_path:
            src = _os.path.expanduser(resolved_file_path)
            if _os.path.exists(src):
                src_real = _os.path.realpath(src)
                # Already inside shared dir? leave as-is.
                if not (src_real == shared_real
                        or src_real.startswith(shared_real + _os.sep)):
                    base = _os.path.basename(src_real.rstrip(_os.sep)) \
                        or _slug(title)
                    dst = _unique(_os.path.join(shared_dir, base))
                    if _os.path.isdir(src_real):
                        _shutil.copytree(src_real, dst)
                    else:
                        _shutil.copy2(src_real, dst)
                    resolved_file_path = dst
                    logger.info(
                        "submit_deliverable: copied %s → %s", src_real, dst)
            else:
                logger.warning(
                    "submit_deliverable: file_path does not exist: %s",
                    resolved_file_path)
    except Exception as _we:
        logger.warning(
            "submit_deliverable: failed to place deliverable under shared "
            "dir (%s); recording path as-is", _we)

    try:
        dv = proj.add_deliverable(
            title=title.strip(),
            kind=(kind or "document").strip(),
            author_agent_id=caller_id,
            task_id=(task_id or "").strip(),
            milestone_id=(milestone_id or "").strip(),
            content_text=content_text or "",
            file_path=resolved_file_path,
            url=(url or "").strip(),
        )
        # Auto-transition to SUBMITTED so it shows up in review queue.
        try:
            proj.submit_deliverable(dv.id)
        except Exception:
            pass
        _save_projects_silently()
        logger.info("submit_deliverable OK: project=%s dv=%s title=%r author=%s file=%s",
                    proj.id, dv.id, title, caller_id or "-",
                    resolved_file_path or "-")
        return (
            f"Deliverable registered: {dv.id} — {title} "
            f"[kind={kind}, project={proj.id}, "
            f"file={resolved_file_path or '(content-only)'}]"
        )
    except Exception as e:
        logger.exception("submit_deliverable failed")
        return f"Error: submit_deliverable failed: {e}"


# ── create_goal ──────────────────────────────────────────────────────

def _tool_create_goal(name: str = "", description: str = "",
                      metric: str = "count", target_value: float = 0.0,
                      target_text: str = "", owner_agent_id: str = "",
                      project_id: str = "", **_: Any) -> str:
    """Create a ProjectGoal for the current project."""
    if not name:
        return "Error: 'name' is required."
    proj, err = _resolve_project(project_id,
                                 kwargs=_ if isinstance(_, dict) else None)
    if err:
        return err
    caller_id = _.get("_caller_agent_id", "") if isinstance(_, dict) else ""
    try:
        g = proj.add_goal(
            name=name.strip(),
            description=description or "",
            owner_agent_id=(owner_agent_id or caller_id or "").strip(),
            metric=(metric or "count").strip(),
            target_value=float(target_value or 0),
            target_text=target_text or "",
        )
        _save_projects_silently()
        logger.info("create_goal OK: project=%s goal=%s name=%r",
                    proj.id, g.id, name)
        return (
            f"Goal created: {g.id} — {name} "
            f"[metric={metric}, target={target_value or target_text}, "
            f"project={proj.id}]"
        )
    except Exception as e:
        logger.exception("create_goal failed")
        return f"Error: create_goal failed: {e}"


# ── update_goal_progress ─────────────────────────────────────────────

def _tool_update_goal_progress(goal_id: str = "", current_value: Any = None,
                                done: Any = None, note: str = "",
                                project_id: str = "", **_: Any) -> str:
    """Update a goal's progress (current_value) or mark as done."""
    if not goal_id:
        return "Error: 'goal_id' is required."
    proj, err = _resolve_project(project_id,
                                 kwargs=_ if isinstance(_, dict) else None)
    if err:
        return err
    try:
        cv = None
        if current_value is not None and str(current_value) != "":
            try:
                cv = float(current_value)
            except Exception:
                return f"Error: current_value must be numeric, got {current_value!r}"
        dn = None
        if done is not None and str(done) != "":
            if isinstance(done, bool):
                dn = done
            else:
                dn = str(done).lower() in ("true", "1", "yes", "y", "done")
        g = proj.update_goal_progress(goal_id, current_value=cv, done=dn)
        if g is None:
            return f"Error: goal not found: {goal_id}"
        _save_projects_silently()
        return (
            f"Goal progress updated: {g.id} — current={g.current_value} "
            f"done={g.done}"
            + (f" note={note!r}" if note else "")
        )
    except Exception as e:
        logger.exception("update_goal_progress failed")
        return f"Error: update_goal_progress failed: {e}"


# ── create_milestone ─────────────────────────────────────────────────

def _tool_create_milestone(name: str = "", responsible_agent_id: str = "",
                            due_date: str = "", project_id: str = "",
                            **_: Any) -> str:
    """Create a ProjectMilestone for the current project."""
    if not name:
        return "Error: 'name' is required."
    proj, err = _resolve_project(project_id,
                                 kwargs=_ if isinstance(_, dict) else None)
    if err:
        return err
    caller_id = _.get("_caller_agent_id", "") if isinstance(_, dict) else ""
    try:
        ms = proj.add_milestone(
            name=name.strip(),
            responsible_agent_id=(responsible_agent_id or caller_id or "").strip(),
            due_date=(due_date or "").strip(),
        )
        _save_projects_silently()
        logger.info("create_milestone OK: project=%s ms=%s name=%r",
                    proj.id, ms.id, name)
        return (
            f"Milestone created: {ms.id} — {name} "
            f"[responsible={ms.responsible_agent_id or '-'}, "
            f"due={ms.due_date or '-'}, project={proj.id}]"
        )
    except Exception as e:
        logger.exception("create_milestone failed")
        return f"Error: create_milestone failed: {e}"


# ── update_milestone_status ──────────────────────────────────────────

def _tool_update_milestone_status(milestone_id: str = "", status: str = "",
                                   evidence: str = "",
                                   project_id: str = "", **_: Any) -> str:
    """Update a milestone's status / attach evidence.

    Status can be any string the project model accepts (pending /
    in_progress / done / etc.). Admin-level confirm/reject is handled
    via separate endpoints.
    """
    if not milestone_id:
        return "Error: 'milestone_id' is required."
    proj, err = _resolve_project(project_id,
                                 kwargs=_ if isinstance(_, dict) else None)
    if err:
        return err
    try:
        kwargs: dict[str, Any] = {}
        if status:
            kwargs["status"] = status.strip()
        if evidence:
            kwargs["evidence"] = evidence
        if not kwargs:
            return "Error: provide at least one of status / evidence."
        ms = proj.update_milestone(milestone_id, **kwargs)
        if ms is None:
            return f"Error: milestone not found: {milestone_id}"
        _save_projects_silently()
        return (
            f"Milestone updated: {ms.id} — status={ms.status} "
            + (f"evidence_len={len(evidence)} " if evidence else "")
        )
    except Exception as e:
        logger.exception("update_milestone_status failed")
        return f"Error: update_milestone_status failed: {e}"
