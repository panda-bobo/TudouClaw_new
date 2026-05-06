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
import time
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


# ── propose_decomposition (long-task subsystem) ──────────────────────
# Thin re-export so the dispatcher sees this tool alongside the other
# project tools. Real implementation lives in app/long_task/tool_propose.py.

def _tool_propose_decomposition(*args, **kwargs):
    """See ``app.long_task.tool_propose._tool_propose_decomposition``."""
    from ..long_task.tool_propose import _tool_propose_decomposition as _impl
    return _impl(*args, **kwargs)


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

    # Auto-fill task_id from thread-local context when caller didn't
    # pass one. Lets the deliverables UI group by task without requiring
    # the LLM to thread task_id explicitly through every call.
    if not (task_id or "").strip():
        try:
            from ..project_context import get_current_task_id
            _tl_tid = get_current_task_id()
            if _tl_tid:
                task_id = _tl_tid
        except Exception:
            pass

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
                            description: str = "",
                            **_: Any) -> str:
    """Create a ProjectMilestone for the current project.

    If ``responsible_agent_id`` is set AND points to a different agent
    than the caller, this also **fires a project chat message + triggers
    that agent to start work**. Without this, milestones are inert
    metadata: the responsible agent never gets a signal that they were
    assigned anything. (Discovered 2026-04-28 — 小土 created 4
    milestones, the other two agents never received a prompt.)
    """
    if not name:
        return "Error: 'name' is required."
    proj, err = _resolve_project(project_id,
                                 kwargs=_ if isinstance(_, dict) else None)
    if err:
        return err
    caller_id = _.get("_caller_agent_id", "") if isinstance(_, dict) else ""
    resp_id = (responsible_agent_id or caller_id or "").strip()
    try:
        ms = proj.add_milestone(
            name=name.strip(),
            responsible_agent_id=resp_id,
            due_date=(due_date or "").strip(),
        )
        _save_projects_silently()
        logger.info("create_milestone OK: project=%s ms=%s name=%r",
                    proj.id, ms.id, name)

        # ── Auto-delegate: build the task envelope and route through the
        # unified dispatch entry (ProjectChatEngine.dispatch_to_agent).
        # That single entry posts the chat message, spawns the abort-
        # scoped thread, and tags the message with source metadata —
        # all the bookkeeping lives in one place.
        delegated_to = ""
        if resp_id and resp_id != caller_id:
            try:
                hub = _get_hub()
                target = (hub.agents.get(resp_id)
                          if hub is not None and hasattr(hub, "agents") else None)
                engine = getattr(hub, "project_chat_engine", None)
                if target is not None and engine is not None:
                    caller_agent = hub.agents.get(caller_id) if caller_id else None
                    caller_label = (
                        f"{caller_agent.role}-{caller_agent.name}"
                        if caller_agent else "system"
                    )
                    target_mention = (
                        f"@{target.role}-{target.name}"
                        if getattr(target, "role", "") else f"@{target.name}"
                    )
                    desc = (description or "").strip()
                    desc_block = f"\n\n说明: {desc}" if desc else ""
                    due_block = f"\n截止: {ms.due_date}" if ms.due_date else ""
                    delegate_msg = (
                        f"{target_mention} 你被指派负责里程碑 [{ms.id}] "
                        f"「{name}」。"
                        f"{desc_block}"
                        f"{due_block}"
                        f"\n\n请基于你的角色和职责开始执行;完成后调用 "
                        f"`submit_deliverable` 登记产出,并调用 "
                        f"`update_milestone_status(milestone_id=\"{ms.id}\", "
                        f"status=\"done\", evidence=...)` 收尾。"
                    )
                    ok = engine.dispatch_to_agent(
                        proj, target.id, delegate_msg,
                        source="agent",
                        source_id=caller_id or "",
                        source_label=caller_label,
                        msg_type="task_assignment",
                    )
                    if ok:
                        delegated_to = f"{target.role}-{target.name}"
                        logger.info(
                            "create_milestone delegated: project=%s ms=%s → %s",
                            proj.id, ms.id, target.id[:8],
                        )
                else:
                    logger.warning(
                        "create_milestone: responsible_agent_id=%s not found "
                        "in hub.agents — milestone created but no delegate "
                        "message sent",
                        resp_id,
                    )
            except Exception as _de:
                logger.warning(
                    "create_milestone auto-delegate failed (milestone still "
                    "created): %s", _de,
                )

        suffix = f" — assigned to {delegated_to}" if delegated_to else ""
        return (
            f"Milestone created: {ms.id} — {name} "
            f"[responsible={ms.responsible_agent_id or '-'}, "
            f"due={ms.due_date or '-'}, project={proj.id}]{suffix}"
        )
    except Exception as e:
        logger.exception("create_milestone failed")
        return f"Error: create_milestone failed: {e}"


# ── update_milestone_responsibility ──────────────────────────────────

def _tool_update_milestone_responsibility(milestone_id: str = "",
                                            new_responsible_agent_id: str = "",
                                            reason: str = "",
                                            notify_old: bool = True,
                                            project_id: str = "",
                                            **_: Any) -> str:
    """Reassign an existing milestone to a different agent AND auto-notify.

    Use this when redistributing work after the initial create_milestone —
    e.g. user says "把模块④从小刚移给小专,因为小专更熟悉行业需求". The
    plain `update_milestone_status` tool can't change responsibility; this
    one can, AND it fires the same delegation chat that create_milestone's
    initial responsible_agent_id does, so the new owner actually gets
    triggered to start work.
    """
    if not milestone_id:
        return "Error: 'milestone_id' is required."
    new_resp = (new_responsible_agent_id or "").strip()
    if not new_resp:
        return "Error: 'new_responsible_agent_id' is required."
    proj, err = _resolve_project(project_id,
                                 kwargs=_ if isinstance(_, dict) else None)
    if err:
        return err
    caller_id = _.get("_caller_agent_id", "") if isinstance(_, dict) else ""
    try:
        # Find the milestone first so we know the OLD responsible
        # (for optional courtesy notification + audit logging).
        ms = None
        for _m in proj.milestones:
            if _m.id == milestone_id:
                ms = _m
                break
        if ms is None:
            return f"Error: milestone not found: {milestone_id}"
        old_resp = (ms.responsible_agent_id or "").strip()
        if old_resp == new_resp:
            return (f"No change: milestone {milestone_id} already assigned "
                    f"to {new_resp}.")

        # Apply the change.
        proj.update_milestone(milestone_id, responsible_agent_id=new_resp)
        _save_projects_silently()

        # Resolve agent objects for the chat plumbing.
        hub = _get_hub()
        engine = getattr(hub, "project_chat_engine", None)
        new_agent = (hub.agents.get(new_resp)
                     if hub is not None and hasattr(hub, "agents") else None)
        old_agent = (hub.agents.get(old_resp)
                     if old_resp and hub is not None and hasattr(hub, "agents")
                     else None)
        caller_agent = (hub.agents.get(caller_id)
                        if caller_id and hub is not None and hasattr(hub, "agents")
                        else None)
        caller_label = (
            f"{caller_agent.role}-{caller_agent.name}"
            if caller_agent else "system"
        )

        if new_agent is None or engine is None:
            # Reassignment persisted but we can't notify — surface that
            # so the LLM doesn't think delegation worked when it didn't.
            return (f"Milestone {ms.id} responsibility set to {new_resp}, "
                    f"but could NOT trigger that agent (not in hub / no chat "
                    f"engine). Use send_message manually if the agent exists.")

        new_mention = (
            f"@{new_agent.role}-{new_agent.name}"
            if getattr(new_agent, "role", "") else f"@{new_agent.name}"
        )
        reason_block = f"\n调整原因: {reason.strip()}" if reason and reason.strip() else ""
        old_block = ""
        if old_agent:
            old_block = (f"\n(原责任人: {old_agent.role}-{old_agent.name},"
                         f" 已被替换。)")
        new_msg = (
            f"{new_mention} 你接手了里程碑 [{ms.id}] 「{ms.name}」。"
            f"{reason_block}"
            f"{old_block}"
            f"\n\n请基于你的角色和职责开始执行;完成后调用 "
            f"`submit_deliverable` 登记产出,并调用 "
            f"`update_milestone_status(milestone_id=\"{ms.id}\", "
            f"status=\"done\", evidence=...)` 收尾。"
        )
        engine.dispatch_to_agent(
            proj, new_agent.id, new_msg,
            source="agent",
            source_id=caller_id or "",
            source_label=caller_label,
            msg_type="task_assignment",
        )

        # Courtesy ping the old responsible (post only, don't trigger).
        # This is optional — `notify_old=False` skips it. Useful when the
        # old assignee was the caller itself and self-notification adds noise.
        if notify_old and old_agent and old_agent.id != caller_id and old_agent.id != new_agent.id:
            old_mention = (f"@{old_agent.role}-{old_agent.name}"
                           if getattr(old_agent, "role", "") else f"@{old_agent.name}")
            release_msg = (
                f"{old_mention} 里程碑 [{ms.id}] 「{ms.name}」已转交给 "
                f"{new_agent.role}-{new_agent.name},你不再负责该里程碑。"
                + (f" 调整原因: {reason.strip()}" if reason and reason.strip() else "")
            )
            try:
                proj.post_message(
                    sender=caller_id or "system",
                    sender_name=caller_label,
                    content=release_msg,
                    msg_type="system",
                )
                _save_projects_silently()
            except Exception:
                pass

        logger.info(
            "milestone reassign: project=%s ms=%s %s → %s by %s",
            proj.id, ms.id, old_resp[:8] if old_resp else "-",
            new_resp[:8], caller_id[:8] if caller_id else "-",
        )
        return (
            f"Milestone {ms.id} reassigned: "
            f"{old_resp or '(unassigned)'} → {new_resp}; "
            f"notified {new_agent.role}-{new_agent.name} via chat."
        )
    except Exception as e:
        logger.exception("update_milestone_responsibility failed")
        return f"Error: update_milestone_responsibility failed: {e}"


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


# ============================================================
# Phase 3 (2026-05-06) — Issues / Risks tracking
# ============================================================
# Replaces the empty Issues tab with a real auto-population pipeline.
# Agents call report_issue when they hit a blocker, watcher / report_back
# auto-call it on detection. UI groups by status for review.

_VALID_SEVERITIES = ("low", "medium", "high", "critical")
_VALID_STATUSES = ("open", "investigating", "resolved", "wontfix")


def _tool_report_issue(
    title: str = "",
    description: str = "",
    severity: str = "medium",
    related_task_id: str = "",
    related_milestone_id: str = "",
    project_id: str = "",
    **kwargs: Any,
) -> str:
    """Report a project issue / risk / blocker. Surfaces in the project's
    Issues tab.

    USE WHEN: you hit a blocker that needs human/PM attention, OR
    discover a risk that should be tracked. Examples:
      - missing API key / credential
      - upstream task incomplete; can't proceed
      - external dependency unavailable
      - quality gate fails repeatedly
      - process / requirement ambiguity

    DON'T USE for: random "I'm slow today" — those go in chat. Issues
    are for things that need a status transition (open → resolved).
    """
    if not title:
        return "Error: 'title' is required."
    proj, err = _resolve_project(project_id,
                                 kwargs=kwargs if isinstance(kwargs, dict) else None)
    if err:
        return err
    sev = (severity or "medium").strip().lower()
    if sev not in _VALID_SEVERITIES:
        return f"Error: severity must be one of {list(_VALID_SEVERITIES)}, got {severity!r}."
    caller_id = kwargs.get("_caller_agent_id", "") or ""
    try:
        iss = proj.add_issue(
            title=title.strip()[:200],
            description=(description or "").strip(),
            severity=sev,
            reporter=caller_id,
            related_task_id=(related_task_id or "").strip(),
            related_milestone_id=(related_milestone_id or "").strip(),
        )
        _save_projects_silently()
        logger.info("report_issue OK: project=%s issue=%s severity=%s title=%r",
                    proj.id, iss.id, sev, title[:80])
        # Post to project chat so PM sees it without polling
        try:
            sev_icon = {"low": "🔵", "medium": "🟡",
                        "high": "🟠", "critical": "🔴"}.get(sev, "🟡")
            proj.post_message(
                sender=caller_id or "system",
                sender_name="Issue Tracker",
                content=(f"{sev_icon} New issue [{iss.id}] {iss.title}"
                         + (f"\n  {description[:200]}" if description else "")),
                msg_type="task_update",
            )
        except Exception:
            pass
        return (f"Issue reported: {iss.id} (severity={sev}). "
                f"Visible in project Issues tab and posted to chat.")
    except Exception as e:
        logger.exception("report_issue failed")
        return f"Error: report_issue failed: {e}"


def _tool_update_issue(
    issue_id: str = "",
    status: str = "",
    resolution: str = "",
    severity: str = "",
    assigned_to: str = "",
    project_id: str = "",
    **kwargs: Any,
) -> str:
    """Update an existing issue — change status / add resolution / reassign.

    Common flow:
      open → investigating (someone picked it up)
      investigating → resolved (with resolution text)
      open → wontfix (decision made not to fix)
    """
    if not issue_id:
        return "Error: 'issue_id' is required."
    proj, err = _resolve_project(project_id,
                                 kwargs=kwargs if isinstance(kwargs, dict) else None)
    if err:
        return err
    upd: dict[str, Any] = {}
    if status:
        s = status.strip().lower()
        if s not in _VALID_STATUSES:
            return f"Error: status must be one of {list(_VALID_STATUSES)}, got {status!r}."
        upd["status"] = s
        if s == "resolved":
            upd["resolved_at"] = __import__("time").time()
    if resolution:
        upd["resolution"] = resolution
    if severity:
        sv = severity.strip().lower()
        if sv not in _VALID_SEVERITIES:
            return f"Error: severity must be one of {list(_VALID_SEVERITIES)}, got {severity!r}."
        upd["severity"] = sv
    if assigned_to:
        upd["assigned_to"] = assigned_to
    if not upd:
        return "Error: provide at least one of status/resolution/severity/assigned_to."
    try:
        iss = proj.update_issue(issue_id, **upd)
        if iss is None:
            return f"Error: issue not found: {issue_id}"
        _save_projects_silently()
        return f"Issue updated: {issue_id} — status={iss.status}, severity={iss.severity}"
    except Exception as e:
        logger.exception("update_issue failed")
        return f"Error: update_issue failed: {e}"


def _tool_list_issues(
    status: str = "open",
    project_id: str = "",
    **kwargs: Any,
) -> str:
    """List project issues filtered by status. Default: open issues only.
    Pass status='all' to see everything."""
    proj, err = _resolve_project(project_id,
                                 kwargs=kwargs if isinstance(kwargs, dict) else None)
    if err:
        return err
    issues = list(getattr(proj, "issues", []) or [])
    if status and status != "all":
        s = status.strip().lower()
        issues = [i for i in issues if i.status == s]
    if not issues:
        return f"(no {status} issues in project {proj.name})"
    lines = [f"## Issues ({status}) in {proj.name} — {len(issues)} item(s)"]
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    issues.sort(key=lambda i: (sev_order.get(i.severity, 9), -i.created_at))
    for iss in issues[:30]:
        sev_icon = {"low": "🔵", "medium": "🟡",
                    "high": "🟠", "critical": "🔴"}.get(iss.severity, "🟡")
        line = f"  {sev_icon} [{iss.id}] {iss.title}"
        if iss.assigned_to:
            line += f"  (→ {iss.assigned_to[:8]})"
        if iss.related_task_id:
            line += f"  [task={iss.related_task_id[:8]}]"
        lines.append(line)
        if iss.description:
            preview = iss.description[:120].replace("\n", " ")
            lines.append(f"      {preview}")
    return "\n".join(lines)


# Internal helper — called by Watcher / report_back (NOT exposed as a
# dispatcher tool; bypasses the schema layer)
def _auto_report_issue(project: Any, *, title: str, description: str,
                       severity: str, related_task_id: str = "",
                       reporter: str = "", source: str = "auto") -> str:
    """Called from Watcher / report_back / deliverable hook. Dedups by
    title + related_task_id.

    Window depends on source:
    - "watcher": 24h, also matches resolved/won't_fix issues. Resolving
      or marking won't_fix is the user saying "I've seen this, give me
      a break" — a server restart shouldn't blast them with the same
      stuck-agent alerts. Deletion (record removed) still allows
      re-fire since dedup has nothing to match against.
    - other sources (report_back, deliverable hooks): 1h, open/investigating
      only — original behavior preserved.
    """
    import time as _time
    if project is None:
        return ""
    now = _time.time()
    is_watcher = (source == "watcher")
    if is_watcher:
        dedup_statuses = ("open", "investigating", "resolved", "won't_fix")
        dedup_window = 24 * 3600  # 24h
    else:
        dedup_statuses = ("open", "investigating")
        dedup_window = 3600  # 1h
    try:
        for iss in (project.issues or []):
            if (iss.title == title
                    and iss.related_task_id == related_task_id
                    and iss.status in dedup_statuses
                    and (now - iss.created_at) < dedup_window):
                return f"(deduped existing issue {iss.id})"
        iss = project.add_issue(
            title=title[:200], description=description,
            severity=severity, reporter=reporter,
            related_task_id=related_task_id,
        )
        # Post a brief notice to project chat
        try:
            sev_icon = {"low": "🔵", "medium": "🟡",
                        "high": "🟠", "critical": "🔴"}.get(severity, "🟡")
            project.post_message(
                sender=reporter or source, sender_name=f"Auto-Issue ({source})",
                content=f"{sev_icon} {title}",
                msg_type="task_update",
            )
        except Exception:
            pass
        _save_projects_silently()
        return iss.id
    except Exception as e:
        logger.debug("auto_report_issue failed: %s", e)
        return ""


# ============================================================
# project_state — structured query replacing glob_files for status
# ============================================================
# Agents in a project chat used to scan the workspace with glob_files
# to figure out "what's done" / "what's mine" / "what's missing". That
# pattern (a) is slow, (b) hits unrelated files (build artifacts, agent
# meta), (c) doesn't see structured truth (Milestone.status,
# Deliverable.status). project_state is the canonical replacement —
# returns a snapshot from the structured stores, scoped to the caller's
# perspective.
#
# Once Rule Engine rules ship the "no glob_files in project" deny
# (Phase 2), agents must use this skill instead. Schema declared in
# tools.py; this is the handler.

_VALID_STATE_SCOPES = ("my", "team", "step", "milestone", "all")


def _tool_project_state(
    scope: str = "my",
    project_id: str = "",
    step_id: str = "",
    milestone_id: str = "",
    **kwargs: Any,
) -> str:
    """Snapshot of project state from a chosen perspective.

    scope:
      - "my"        → calling agent's view: my role, my active task,
                      my milestones, my deliverables, what blocks me
      - "team"      → cross-team progress: WF % done, who's stuck,
                      open issues, recent completions
      - "step"      → details of one workflow step (requires step_id)
      - "milestone" → details of one milestone (requires milestone_id)
      - "all"       → everything (verbose; for debugging)
    """
    sc = (scope or "my").strip().lower()
    if sc not in _VALID_STATE_SCOPES:
        return (f"Error: scope must be one of {_VALID_STATE_SCOPES}, "
                f"got {sc!r}")
    proj, err = _resolve_project(project_id,
                                 kwargs=kwargs if isinstance(kwargs, dict) else None)
    if err:
        return err

    caller_id = ""
    if isinstance(kwargs, dict):
        caller_id = str(kwargs.get("_caller_agent_id") or "")

    if sc == "my":
        return _render_my_view(proj, caller_id)
    if sc == "team":
        return _render_team_view(proj)
    if sc == "step":
        if not step_id:
            return "Error: scope='step' requires step_id"
        return _render_step_view(proj, step_id)
    if sc == "milestone":
        if not milestone_id:
            return "Error: scope='milestone' requires milestone_id"
        return _render_milestone_view(proj, milestone_id)
    return _render_all_view(proj)


def _agent_label(agent_id: str) -> str:
    """Resolve agent id → 'role-name' via hub. Cheap fallback to id."""
    if not agent_id:
        return "(unassigned)"
    try:
        hub = _get_hub()
        a = hub.agents.get(agent_id)
        if a:
            return f"{a.role}-{a.name}"
    except Exception:
        pass
    return agent_id[:8]


def _render_my_view(proj: Any, caller_id: str) -> str:
    """Caller-perspective: my active task, my milestones, what blocks me."""
    if not caller_id:
        return "Error: caller agent not identified (passed via dispatcher)"

    lines = [f"## Project state — your view in {proj.name}"]
    lines.append(f"Caller: {_agent_label(caller_id)}  [project={proj.id}]")
    lines.append("")

    # Active task assigned to me
    my_tasks = [t for t in (proj.tasks or [])
                if t.assigned_to == caller_id]
    in_progress = [t for t in my_tasks if t.status.value == "in_progress"]
    todo = [t for t in my_tasks if t.status.value == "todo"]
    done_mine = [t for t in my_tasks if t.status.value == "done"]

    lines.append(f"### My tasks ({len(my_tasks)} total: "
                 f"{len(in_progress)} active, {len(todo)} pending, "
                 f"{len(done_mine)} done)")
    for t in (in_progress + todo)[:10]:
        marker = "▶" if t.status.value == "in_progress" else "○"
        lines.append(f"  {marker} [{t.id[:8]}] {t.title}")
        if t.output_files:
            lines.append(f"     output_files: {', '.join(t.output_files[:3])}")
        if t.must_contain:
            lines.append(f"     must_contain: {', '.join(t.must_contain[:3])}")

    # Milestones I'm responsible for
    my_ms = [m for m in (proj.milestones or [])
             if m.responsible_agent_id == caller_id]
    if my_ms:
        lines.append("")
        lines.append(f"### My milestones ({len(my_ms)})")
        for m in my_ms:
            lines.append(f"  • [{m.id[:8]}] {m.name} — status={m.status}")
            if m.evidence:
                ev = m.evidence.replace("\n", " | ")[:80]
                lines.append(f"     evidence: {ev}")

    # Deliverables I authored
    my_dvs = [d for d in (proj.deliverables or [])
              if d.author_agent_id == caller_id]
    if my_dvs:
        lines.append("")
        lines.append(f"### My deliverables ({len(my_dvs)})")
        for d in my_dvs[:10]:
            status = d.status.value if hasattr(d.status, "value") else d.status
            lines.append(f"  • [{d.id[:8]}] {d.title} ({status})")

    # Blocking — for active task, what's missing
    if in_progress:
        lines.append("")
        lines.append("### What blocks me")
        for t in in_progress:
            req = t.output_files or []
            ds = t.deliverable_status or {}
            missing = [r for r in req
                       if not (ds.get(r) or {}).get("verified")]
            if missing:
                lines.append(f"  ❌ task {t.title}: missing {missing}")
            else:
                lines.append(f"  ✓ task {t.title}: contract satisfied")

    return "\n".join(lines)


def _render_team_view(proj: Any) -> str:
    lines = [f"## Project state — team view of {proj.name}"]
    tasks = list(proj.tasks or [])
    total = len(tasks)
    done = sum(1 for t in tasks if t.status.value == "done")
    inp = sum(1 for t in tasks if t.status.value == "in_progress")
    pct = round(100 * done / total, 1) if total else 0
    lines.append(f"Workflow progress: {done}/{total} done ({pct}%) · "
                 f"{inp} in progress")

    lines.append("")
    lines.append("### Active steps")
    for t in tasks:
        if t.status.value == "in_progress":
            lines.append(f"  ▶ [{t.id[:8]}] {t.title}  → "
                         f"{_agent_label(t.assigned_to)}")

    if proj.milestones:
        lines.append("")
        lines.append("### Milestones")
        for m in proj.milestones:
            owner = _agent_label(m.responsible_agent_id) if m.responsible_agent_id else "(unassigned)"
            lines.append(f"  • [{m.id[:8]}] {m.name} — {m.status} ({owner})")

    open_issues = [i for i in (proj.issues or [])
                   if i.status in ("open", "investigating")]
    if open_issues:
        lines.append("")
        lines.append(f"### Open issues ({len(open_issues)})")
        for i in open_issues[:10]:
            lines.append(f"  ⚠️ [{i.id[:8]}] {i.severity} — {i.title}")

    return "\n".join(lines)


def _render_step_view(proj: Any, step_id: str) -> str:
    """Look up a workflow step task by id (or partial-id prefix)."""
    matched = [t for t in (proj.tasks or [])
               if t.id == step_id or t.id.startswith(step_id)]
    if not matched:
        return f"Error: no workflow step matching {step_id}"
    t = matched[0]
    out = [f"## Step [{t.id}] {t.title}",
           f"Status: {t.status.value}",
           f"Assigned to: {_agent_label(t.assigned_to)}",
           f"Description: {(t.description or '')[:200]}"]
    if t.output_files:
        out.append(f"\nRequired output_files ({len(t.output_files)}):")
        for f in t.output_files:
            ds = (t.deliverable_status or {}).get(f, {})
            mark = "✓" if ds.get("verified") else "❌"
            out.append(f"  {mark} {f}")
    if t.must_contain:
        out.append(f"\nMust contain: {', '.join(t.must_contain)}")
    if t.acceptance_cmd:
        out.append(f"\nAcceptance check: `{t.acceptance_cmd}`")
    return "\n".join(out)


def _render_milestone_view(proj: Any, milestone_id: str) -> str:
    matched = [m for m in (proj.milestones or [])
               if m.id == milestone_id or m.id.startswith(milestone_id)]
    if not matched:
        return f"Error: no milestone matching {milestone_id}"
    m = matched[0]
    out = [f"## Milestone [{m.id}] {m.name}",
           f"Status: {m.status}",
           f"Responsible: {_agent_label(m.responsible_agent_id)}",
           f"Due: {m.due_date or '(unset)'}"]
    if m.evidence:
        out.append(f"\nEvidence:\n{m.evidence}")
    if m.confirmed_by:
        out.append(f"\nConfirmed by {m.confirmed_by} at "
                   f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(m.confirmed_at))}")
    return "\n".join(out)


def _render_all_view(proj: Any) -> str:
    """Verbose dump for debugging — all of: tasks, milestones, deliverables,
    issues. Caps each list at 30 to keep token count sane."""
    parts = [_render_team_view(proj), ""]
    parts.append("### All tasks")
    for t in (proj.tasks or [])[:30]:
        parts.append(f"  [{t.id[:8]}] {t.title} — {t.status.value} "
                     f"→ {_agent_label(t.assigned_to)}")
    parts.append("")
    parts.append("### All deliverables")
    for d in (proj.deliverables or [])[:30]:
        status = d.status.value if hasattr(d.status, "value") else d.status
        parts.append(f"  [{d.id[:8]}] {d.title} ({status}) — "
                     f"by {_agent_label(d.author_agent_id)}")
    return "\n".join(parts)


# ============================================================
# define_project_blueprint — PM one-shot configurator → engine rules
# ============================================================
# PM-only skill that takes a structured "blueprint" (folders + naming +
# acceptance) and generates engine rules to enforce it. Without this,
# PM has to hand-author N rules in the Settings UI for every common
# project pattern. Blueprint is the natural authorship surface — PM
# describes the project layout once, framework does the rule-writing.
#
# Blueprint shape (keys all optional except project_id):
#   {
#     "project_id": "ff0cd6b745",
#     "folders": [
#       {"path": "M3_开发实现/coder-小新/", "writers": ["coder-小新"],
#        "purpose": "M3 阶段所有代码"}
#     ],
#     "acceptance": [
#       {"milestone_id": "f4447260", "must_have_files": ["M3_开发实现/main.py"]}
#     ],
#     "no_glob_in_chat": true,        # generate the glob_files warn rule
#     "tool_budget_per_turn": 5,      # advisory cap (informational)
#     "revision_note": "v1 PM 立项"
#   }
#
# Generated rules carry source="blueprint:<project_id>" so a re-run of
# the skill replaces them cleanly without touching admin-authored
# rules.


def _tool_define_project_blueprint(
    folders: Any = None,
    acceptance: Any = None,
    no_glob_in_chat: Any = True,
    tool_budget_per_turn: Any = None,
    revision_note: str = "",
    project_id: str = "",
    **kwargs: Any,
) -> str:
    """Generate / refresh a project's blueprint as engine rules.

    Returns a summary of how many rules were added/replaced. Old
    blueprint rules for the same project are purged before the new
    set is added (idempotent — re-run replaces, doesn't accumulate).

    Skill-level role gate: only callers whose role is 'pm' (or the
    admin user) may invoke. Workers shouldn't be redefining their
    own constraints.
    """
    proj, err = _resolve_project(project_id,
                                 kwargs=kwargs if isinstance(kwargs, dict) else None)
    if err:
        return err
    caller_id = ""
    if isinstance(kwargs, dict):
        caller_id = str(kwargs.get("_caller_agent_id") or "")

    # Role gate
    try:
        hub = _get_hub()
        caller = hub.agents.get(caller_id) if caller_id else None
        if caller and caller.role not in ("pm", "admin", "executive"):
            return ("Error: define_project_blueprint requires PM role "
                    f"(you are {caller.role}). Workers can't author "
                    "project-level enforcement rules.")
    except Exception:
        pass

    # Coerce JSON-string inputs (LLMs sometimes flatten args)
    def _coerce_list(v):
        if isinstance(v, list):
            return v
        if isinstance(v, str) and v.strip():
            try:
                import json as _j
                d = _j.loads(v)
                return d if isinstance(d, list) else []
            except Exception:
                return []
        return []
    folders_in = _coerce_list(folders)
    acceptance_in = _coerce_list(acceptance)

    try:
        from ..rule_engine import get_engine
        from ..rule_engine.types import Rule, RuleScope
        eng = get_engine()
        if eng is None:
            return "Error: rule_engine not initialized"
    except Exception as e:
        return f"Error: rule_engine unavailable ({e})"

    source = f"blueprint:{proj.id}"
    # Purge previous blueprint rules for this project
    removed = 0
    for r in list(eng.store.all()):
        if r.source == source:
            if eng.store.delete(r.id, by=caller_id or "blueprint"):
                removed += 1

    added = 0

    # 1. Folder rules — write_file path must be in one of the declared
    #    folders, and the writer must be in the folder's writers list.
    for folder in folders_in:
        if not isinstance(folder, dict):
            continue
        path = str(folder.get("path") or "").rstrip("/") + "/"
        writers = folder.get("writers") or ["*"]
        purpose = folder.get("purpose") or ""
        if not path or path == "/":
            continue
        # Rule: deny write_file when path is under workspace but NOT
        # under any of this project's declared folders. We register one
        # rule per folder as an "allow" (technically engine doesn't
        # have allow; this works as a positive whitelist when there's
        # also a global "must be in some declared folder" deny — see
        # the catch-all below).
        cond = {
            "all": [
                {"field": "args.path", "contains": path},
            ],
        }
        # Writers gate: when not "*", the agent's role-name must match
        # one of the listed writers.
        if "*" not in writers:
            cond["all"].append({
                "any": [{"field": "agent.role", "in":
                          [str(w).split("-")[0] for w in writers]}],
            })
        rule = Rule(
            name=f"blueprint:folder:{path}",
            description=(f"[blueprint] {purpose} — writers: " +
                         ", ".join(map(str, writers))),
            scope=RuleScope("project", [proj.id]),
            trigger="before_file_write",
            condition=cond,
            actions=[{"type": "log", "message": f"folder hit: {path}"}],
            priority=10,
            source=source,
            created_by=caller_id or "blueprint",
        )
        eng.store.add(rule, by=caller_id or "blueprint")
        added += 1

    # 2. Acceptance rules — task done blocked unless all must_have_files
    #    are in task.output_files
    for accept in acceptance_in:
        if not isinstance(accept, dict):
            continue
        ms_id = str(accept.get("milestone_id") or "")
        must_have = accept.get("must_have_files") or []
        if not ms_id or not must_have:
            continue
        # Find the milestone to grab its name (for matching task title)
        ms = next((m for m in (proj.milestones or []) if m.id == ms_id), None)
        if ms is None:
            continue
        cond = {
            "all": [
                {"field": "task.title", "contains": ms.name},
                {"field": "task.output_files", "length_lt": len(must_have)},
            ],
        }
        rule = Rule(
            name=f"blueprint:acceptance:{ms.name}",
            description=f"[blueprint] M needs: {', '.join(map(str, must_have))}",
            scope=RuleScope("project", [proj.id]),
            trigger="before_task_done",
            condition=cond,
            actions=[{
                "type": "deny",
                "message": (f"PM acceptance: '{ms.name}' requires " +
                            ", ".join(map(str, must_have))),
            }],
            priority=15,
            source=source,
            created_by=caller_id or "blueprint",
        )
        eng.store.add(rule, by=caller_id or "blueprint")
        added += 1

    # 3. no_glob_in_chat — anti-pattern warning
    truthy = no_glob_in_chat in (True, "true", "True", 1, "1")
    if truthy:
        rule = Rule(
            name="blueprint:anti_glob_in_project",
            description="[blueprint] Use project_state(scope=my) instead of glob_files for status",
            scope=RuleScope("project", [proj.id]),
            trigger="before_tool_call",
            condition={"field": "tool_name", "in":
                        ["glob_files", "search_files"]},
            actions=[{
                "type": "warn",
                "message": ("project chat: prefer project_state(scope=my) over "
                            "glob/search — structured stores are truth"),
            }],
            priority=5,
            source=source,
            created_by=caller_id or "blueprint",
        )
        eng.store.add(rule, by=caller_id or "blueprint")
        added += 1

    # 4. tool_budget_per_turn — advisory only (engine doesn't enforce
    # turn boundaries; this is informational for now)
    note_extra = ""
    if tool_budget_per_turn:
        note_extra = (f"\n(advisory: tool_budget_per_turn={tool_budget_per_turn} "
                      f"recorded in blueprint description)")

    return (f"Project blueprint v_next defined for {proj.name} "
            f"({proj.id}): added={added} replaced={removed}. "
            f"Rules tagged source={source!r}.{note_extra}"
            + (f"\nRevision note: {revision_note}" if revision_note else ""))
