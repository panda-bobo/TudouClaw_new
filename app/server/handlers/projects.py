"""


╔════════════════════════════════════════════════════════════════════════╗
║  ⚠️  DEPRECATED — LEGACY stdlib handler (extracted from                ║
║     portal_routes_post.py). Active only when TUDOU_USE_STDLIB=1 is     ║
║     set at launch. FastAPI (app/api/routers/*) is authoritative.       ║
║     Do NOT add new routes here.                                        ║
╚════════════════════════════════════════════════════════════════════════╝
projects — handlers for project-management POST endpoints.

Extracted from portal_routes_post.py (Phase 2).  Handles:

    POST /api/portal/projects                          — CRUD (create/update/delete)
    POST /api/portal/projects/{id}/members             — add/remove members
    POST /api/portal/projects/{id}/chat                — project chat
    POST /api/portal/projects/{id}/tasks               — assign task
    POST /api/portal/projects/{id}/task-update          — update task
    POST /api/portal/projects/{id}/task-steps           — define/replace step list
    POST /api/portal/projects/{id}/task-step-review     — human review of step
    POST /api/portal/projects/{id}/task-checkpoint      — manual step checkpoint
    POST /api/portal/projects/{id}/milestones           — add milestone
    POST /api/portal/projects/{id}/milestones/{mid}/update
    POST /api/portal/projects/{id}/milestones/{mid}/confirm
    POST /api/portal/projects/{id}/milestones/{mid}/reject
    POST /api/portal/projects/{id}/goals                — add goal
    POST /api/portal/projects/{id}/goals/{gid}/update
    POST /api/portal/projects/{id}/goals/{gid}/progress
    POST /api/portal/projects/{id}/goals/{gid}/delete
    POST /api/portal/projects/{id}/deliverables         — add deliverable
    POST /api/portal/projects/{id}/deliverables/{did}/update
    POST /api/portal/projects/{id}/deliverables/{did}/submit
    POST /api/portal/projects/{id}/deliverables/{did}/review
    POST /api/portal/projects/{id}/deliverables/{did}/delete
    POST /api/portal/projects/{id}/issues               — add issue
    POST /api/portal/projects/{id}/issues/{iid}/update
    POST /api/portal/projects/{id}/issues/{iid}/resolve
    POST /api/portal/projects/{id}/issues/{iid}/delete
    POST /api/portal/projects/{id}/tasks/{tid}/approve-step
    POST /api/portal/projects/{id}/status               — lifecycle transition
    POST /api/portal/projects/{id}/pause
    POST /api/portal/projects/{id}/resume
"""
from __future__ import annotations

import base64
import logging
import os
import re
import threading
import time

from ..portal_auth import get_client_ip
from ...defaults import MAX_DATA_UPLOAD

logger = logging.getLogger("tudou.portal")


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def try_handle(handler, path: str, hub, body: dict, auth,
               actor_name: str, user_role: str) -> bool:
    """Return *True* if *path* was handled by this module, *False* otherwise."""

    # ── Project CRUD ──
    if path == "/api/portal/projects":
        return _handle_projects_crud(handler, hub, body, auth, actor_name, user_role)

    # All remaining project routes start with this prefix
    if not path.startswith("/api/portal/projects/"):
        return False

    # ── Members ──
    if path.endswith("/members"):
        return _handle_members(handler, path, hub, body, auth, actor_name, user_role)

    # ── Project chat ──
    if path.endswith("/chat"):
        return _handle_chat(handler, path, hub, body, auth, actor_name, user_role)

    # ── Task assignment ──
    if path.endswith("/tasks") and "/tasks/" not in path:
        return _handle_tasks(handler, path, hub, body, auth, actor_name, user_role)

    # ── Task update ──
    if path.endswith("/task-update"):
        return _handle_task_update(handler, path, hub, body, auth, actor_name, user_role)

    # ── Task steps ──
    if path.endswith("/task-steps"):
        return _handle_task_steps(handler, path, hub, body, auth, actor_name, user_role)

    # ── Task step review ──
    if path.endswith("/task-step-review"):
        return _handle_task_step_review(handler, path, hub, body, auth, actor_name, user_role)

    # ── Task checkpoint ──
    if path.endswith("/task-checkpoint"):
        return _handle_task_checkpoint(handler, path, hub, body, auth, actor_name, user_role)

    # ── Milestones ──
    if "/milestones/" in path and path.endswith("/confirm"):
        return _handle_milestone_confirm(handler, path, hub, body, auth, actor_name, user_role)

    if "/milestones/" in path and path.endswith("/reject"):
        return _handle_milestone_reject(handler, path, hub, body, auth, actor_name, user_role)

    if "/milestones/" in path and path.endswith("/update"):
        return _handle_milestone_update(handler, path, hub, body, auth, actor_name, user_role)

    if path.endswith("/milestones"):
        return _handle_milestones(handler, path, hub, body, auth, actor_name, user_role)

    # ── Goals ──
    if "/goals/" in path and path.endswith("/update"):
        return _handle_goal_update(handler, path, hub, body, auth, actor_name, user_role)

    if "/goals/" in path and path.endswith("/progress"):
        return _handle_goal_progress(handler, path, hub, body, auth, actor_name, user_role)

    if "/goals/" in path and path.endswith("/delete"):
        return _handle_goal_delete(handler, path, hub, body, auth, actor_name, user_role)

    if path.endswith("/goals"):
        return _handle_goals(handler, path, hub, body, auth, actor_name, user_role)

    # ── Deliverables ──
    if "/deliverables/" in path and path.endswith("/update"):
        return _handle_deliverable_update(handler, path, hub, body, auth, actor_name, user_role)

    if "/deliverables/" in path and path.endswith("/submit"):
        return _handle_deliverable_submit(handler, path, hub, body, auth, actor_name, user_role)

    if "/deliverables/" in path and path.endswith("/review"):
        return _handle_deliverable_review(handler, path, hub, body, auth, actor_name, user_role)

    if "/deliverables/" in path and path.endswith("/delete"):
        return _handle_deliverable_delete(handler, path, hub, body, auth, actor_name, user_role)

    if path.endswith("/deliverables"):
        return _handle_deliverables(handler, path, hub, body, auth, actor_name, user_role)

    # ── Issues ──
    if "/issues/" in path and path.endswith("/update"):
        return _handle_issue_update(handler, path, hub, body, auth, actor_name, user_role)

    if "/issues/" in path and path.endswith("/resolve"):
        return _handle_issue_resolve(handler, path, hub, body, auth, actor_name, user_role)

    if "/issues/" in path and path.endswith("/delete"):
        return _handle_issue_delete(handler, path, hub, body, auth, actor_name, user_role)

    if path.endswith("/issues"):
        return _handle_issues(handler, path, hub, body, auth, actor_name, user_role)

    # ── Approve step (workflow) ──
    if "/tasks/" in path and path.endswith("/approve-step"):
        return _handle_approve_step(handler, path, hub, body, auth, actor_name, user_role)

    # ── Project lifecycle ──
    if path.endswith("/status"):
        return _handle_status(handler, path, hub, body, auth, actor_name, user_role)

    if path.endswith("/pause"):
        return _handle_pause(handler, path, hub, body, auth, actor_name, user_role)

    if path.endswith("/resume"):
        return _handle_resume(handler, path, hub, body, auth, actor_name, user_role)

    return False


# ------------------------------------------------------------------
# Helper: extract project_id from path
# ------------------------------------------------------------------

def _proj_id(path: str) -> str:
    """Extract project id (segment [4]) from an /api/portal/projects/{id}/... path."""
    return path.split("/")[4]


# ------------------------------------------------------------------
# Handler implementations
# ------------------------------------------------------------------

def _handle_projects_crud(handler, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects  — create / update / delete a project."""
    try:
        action = body.get("action", "create")
        if action == "create":
            proj = hub.create_project(
                name=body.get("name", "New Project"),
                description=body.get("description", ""),
                member_configs=body.get("members", []),
                working_directory=body.get("working_directory", ""),
                node_id=body.get("node_id", "local"),
                workflow_id=body.get("workflow_id", ""),
                step_assignments=body.get("step_assignments", []),
            )
            auth.audit("create_project", actor=actor_name,
                       role=user_role, target=proj.id,
                       ip=get_client_ip(handler))
            handler._json(proj.to_dict())

        elif action == "update":
            proj = hub.get_project(body.get("project_id", ""))
            if not proj:
                handler._json({"error": "Project not found"}, 404)
                return True
            if body.get("name"):
                proj.name = body["name"]
            if body.get("description") is not None:
                proj.description = body["description"]
            if body.get("status"):
                old_status = str(proj.status)
                # Route through set_status for proper enum conversion +
                # paused flag synchronization.
                ok_s, _msg_s = proj.set_status(
                    body["status"], by=actor_name or "admin", reason="")
                if not ok_s:
                    handler._json({"error": _msg_s}, 400)
                    return True
                # Agent learning closure: when project completes, trigger
                # experience consolidation for all members.
                new_status = body["status"]
                if (str(new_status) in ("completed", "ProjectStatus.COMPLETED")
                        and "completed" not in old_status.lower()):
                    for m in proj.members:
                        agent = hub.get_agent(m.agent_id)
                        if not agent:
                            continue
                        try:
                            consolidator = agent._get_memory_consolidator()
                            if consolidator:
                                consolidator.consolidate(
                                    agent_id=m.agent_id, force=True)
                                agent.history_log.add(
                                    "project_complete_learning",
                                    f"[Learning] 项目 {proj.name} 完成，经验已沉淀")
                        except Exception as _e:
                            logger.debug("Project-complete consolidate failed for %s: %s",
                                         m.agent_id, _e)
            # If working_directory is updated, re-sync all member agents
            if body.get("working_directory"):
                proj.working_directory = body["working_directory"]
                for member in proj.members:
                    hub._sync_agent_to_project_dir(
                        member.agent_id, proj.working_directory,
                        project_id=proj.id, project_name=proj.name)
                hub._save_agents()
            # Workflow binding / replacement
            if body.get("workflow_id") is not None:
                wf_id = body["workflow_id"]
                step_asgn = body.get("step_assignments", [])
                if wf_id:
                    # Clear old WF tasks then re-bind
                    proj.tasks = [t for t in proj.tasks
                                  if not t.title.startswith("[WF Step")]
                    hub._bind_workflow_to_project(
                        proj, wf_id, step_asgn)
                    if proj.working_directory:
                        hub._save_agents()
                else:
                    # Unbind: clear binding and WF tasks
                    from ...project import WorkflowBinding
                    proj.workflow_binding = WorkflowBinding()
                    proj.tasks = [t for t in proj.tasks
                                  if not t.title.startswith("[WF Step")]
            hub._save_projects()
            auth.audit("update_project", actor=actor_name,
                       role=user_role, target=proj.id,
                       ip=get_client_ip(handler))
            handler._json(proj.to_dict())

        elif action == "delete":
            pid = body.get("project_id", "")
            ok = hub.remove_project(pid)
            if ok:
                auth.audit("delete_project", actor=actor_name,
                           role=user_role, target=pid,
                           ip=get_client_ip(handler))
                handler._json({"ok": True})
            else:
                handler._json({"error": "Project not found"}, 404)
        else:
            handler._json({"error": f"Unknown action: {action}"}, 400)
    except Exception as exc:
        logger.exception("projects_crud error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_members(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/members  — add / remove members."""
    try:
        proj_id = _proj_id(path)
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        agent_id = body.get("agent_id", "")
        responsibility = body.get("responsibility", "")
        if body.get("action") == "remove":
            proj.remove_member(agent_id)
        else:
            proj.add_member(agent_id, responsibility)
            # Sync project working_directory to new member agent
            if proj.working_directory and agent_id:
                hub._sync_agent_to_project_dir(
                    agent_id, proj.working_directory,
                    project_id=proj.id, project_name=proj.name)
                hub._save_agents()
        hub._save_projects()
        handler._json({"ok": True, "members": [m.to_dict() for m in proj.members]})
    except Exception as exc:
        logger.exception("members error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_chat(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/chat  — project chat with attachments."""
    try:
        proj_id = _proj_id(path)
        content = body.get("content", "").strip()
        target_agents = body.get("target_agents")
        attachments = body.get("attachments") or []
        # Persist any attachments to the project working dir and append refs
        saved_refs: list[str] = []
        if isinstance(attachments, list) and attachments:
            proj = hub.get_project(proj_id)
            if proj is None:
                handler._json({"error": "Project not found"}, 404)
                return True
            try:
                base_dir = proj.working_directory or os.path.join(
                    os.environ.get("TUDOU_CLAW_DATA_DIR", "."),
                    "projects", proj_id,
                )
                att_dir = os.path.join(base_dir, "attachments")
                os.makedirs(att_dir, exist_ok=True)
                for att in attachments[:10]:  # cap at 10 per message
                    if not isinstance(att, dict):
                        continue
                    raw_name = str(att.get("name") or "attachment.bin")
                    safe_name = "".join(
                        c for c in raw_name if c.isalnum() or c in "._-"
                    ) or "attachment.bin"
                    data_b64 = att.get("data_base64") or ""
                    if not data_b64:
                        continue
                    try:
                        data_bytes = base64.b64decode(data_b64)
                    except Exception:
                        continue
                    if len(data_bytes) > MAX_DATA_UPLOAD:  # hard cap
                        continue
                    ts = int(time.time() * 1000)
                    fname = f"{ts}_{safe_name}"
                    fpath = os.path.join(att_dir, fname)
                    try:
                        with open(fpath, "wb") as _f:
                            _f.write(data_bytes)
                    except Exception:
                        continue
                    saved_refs.append(fname)
            except Exception as _ae:
                logger.warning("attachment save failed: %s", _ae)
        # Merge attachment refs into content
        if saved_refs:
            suffix = "\n" + " ".join(f"\U0001f4ce{r}" for r in saved_refs)
            content = (content + suffix) if content else suffix.lstrip()
        if not content:
            handler._json({"error": "Empty message"}, 400)
        else:
            respondents = hub.project_chat(proj_id, content, target_agents)
            handler._json({
                "ok": True,
                "respondents": respondents,
                "attachments_saved": saved_refs,
            })
    except Exception as exc:
        logger.exception("chat error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_tasks(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/tasks  — assign a new task."""
    try:
        proj_id = _proj_id(path)
        task = hub.project_assign_task(
            proj_id,
            title=body.get("title", ""),
            description=body.get("description", ""),
            assigned_to=body.get("assigned_to", ""),
            priority=body.get("priority", 0),
        )
        if task:
            handler._json(task.to_dict())
        else:
            handler._json({"error": "Project not found"}, 404)
    except Exception as exc:
        logger.exception("tasks error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_task_update(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/task-update  — update an existing task."""
    try:
        proj_id = _proj_id(path)
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        task = proj.update_task(
            body.get("task_id", ""),
            **{k: v for k, v in body.items() if k != "task_id"}
        )
        if task:
            hub._save_projects()
            # WF Step manual completion -> auto-progress next step
            new_status = body.get("status", "")
            if (new_status == "done"
                    and task.title.startswith("[WF Step")
                    and proj.workflow_binding.workflow_id):
                try:
                    hub.project_chat_engine._auto_progress_next_step(
                        proj, task)
                except Exception as e:
                    logger.warning(
                        "WF auto-progress after manual toggle failed: %s", e)
            handler._json(task.to_dict())
        else:
            handler._json({"error": "Task not found"}, 404)
    except Exception as exc:
        logger.exception("task_update error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_task_steps(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/task-steps  — define/replace step list."""
    try:
        proj_id = _proj_id(path)
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        task_id = body.get("task_id", "")
        items = body.get("steps") or []
        task = next((t for t in proj.tasks if t.id == task_id), None)
        if not task:
            handler._json({"error": "Task not found"}, 404)
            return True
        # Accept both legacy list[str] and new list[{"name", "manual_review"}]
        if not isinstance(items, list):
            handler._json({"error": "steps must be a list"}, 400)
            return True
        normalized = []
        for it in items:
            if isinstance(it, str):
                normalized.append({"name": it, "manual_review": False})
            elif isinstance(it, dict) and "name" in it:
                normalized.append({
                    "name": str(it.get("name", "")),
                    "manual_review": bool(it.get("manual_review", False)),
                })
            else:
                handler._json({"error": "each step must be str or {name, manual_review}"}, 400)
                return True
        # Reset steps (preserve status of already-completed same-name steps,
        # but sync their manual_review flag)
        from ...project import TaskStep
        prev_done = {s.name: s for s in (task.steps or []) if s.status in ("done", "skipped")}
        new_steps = []
        for it in normalized:
            n = it["name"]
            if n in prev_done:
                s = prev_done[n]
                s.manual_review = it["manual_review"]
                new_steps.append(s)
            else:
                new_steps.append(TaskStep(name=n, manual_review=it["manual_review"]))
        task.steps = new_steps
        task.current_step_index = 0
        task.last_checkpoint_at = time.time()
        task.updated_at = task.last_checkpoint_at
        hub._save_projects()
        handler._json({"ok": True, "task": task.to_dict()})
    except Exception as exc:
        logger.exception("task_steps error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_task_step_review(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/task-step-review  — human review of step."""
    try:
        proj_id = _proj_id(path)
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        task_id = body.get("task_id", "")
        step_id = body.get("step_id", "")
        action = body.get("action", "approve")
        task = next((t for t in proj.tasks if t.id == task_id), None)
        if not task:
            handler._json({"error": "Task not found"}, 404)
            return True
        step = next((s for s in (task.steps or []) if s.id == step_id), None)
        if not step:
            handler._json({"error": "Step not found"}, 404)
            return True
        if not step.manual_review:
            handler._json({"error": "step is not flagged for manual review"}, 400)
            return True
        if step.status != "awaiting_review":
            handler._json({
                "error": f"step is not awaiting review (current status: {step.status})"
            }, 400)
            return True
        reviewer = getattr(handler, "_admin_user", None) or "user"
        if isinstance(reviewer, dict):
            reviewer = reviewer.get("username") or reviewer.get("id") or "user"
        if action == "approve":
            ok = task.approve_step(step, reviewer_id=str(reviewer),
                                   override_result=body.get("result", ""))
            if not ok:
                handler._json({"error": "approve failed"}, 400)
                return True
            # Re-trigger the runner so remaining steps can proceed.
            try:
                hub._save_projects()
            except Exception as e:
                logger.warning("Failed to save projects after task approval: %s", e)
            try:
                hub.project_chat_engine.handle_task_assignment(proj, task)
            except Exception as _e:
                # Resume failure shouldn't fail the approval itself
                logger.debug("Failed to handle task assignment on approval: %s", _e)
            handler._json({"ok": True, "task": task.to_dict()})
        elif action == "reject":
            ok = task.reject_step(step, reviewer_id=str(reviewer),
                                  reason=body.get("reason", ""))
            if not ok:
                handler._json({"error": "reject failed"}, 400)
                return True
            try:
                hub._save_projects()
            except Exception as e:
                logger.warning("Failed to save projects after task rejection: %s", e)
            # Re-trigger so the agent can re-run the rejected step
            try:
                hub.project_chat_engine.handle_task_assignment(proj, task)
            except Exception as e:
                logger.debug("Failed to handle task assignment on rejection: %s", e)
            handler._json({"ok": True, "task": task.to_dict()})
        else:
            handler._json({"error": "action must be approve|reject"}, 400)
    except Exception as exc:
        logger.exception("task_step_review error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_task_checkpoint(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/task-checkpoint  — manual step checkpoint."""
    try:
        proj_id = _proj_id(path)
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        task_id = body.get("task_id", "")
        step_id = body.get("step_id", "")
        new_status = body.get("status", "done")
        task = next((t for t in proj.tasks if t.id == task_id), None)
        if not task:
            handler._json({"error": "Task not found"}, 404)
            return True
        step = next((s for s in (task.steps or []) if s.id == step_id), None)
        if not step:
            handler._json({"error": "Step not found"}, 404)
            return True
        if new_status == "failed":
            task.complete_step(step, error=body.get("error", "manual fail"))
        elif new_status == "skipped":
            step.status = "skipped"
            step.completed_at = time.time()
            task.last_checkpoint_at = step.completed_at
        else:
            task.complete_step(step, result=body.get("result", ""))
        hub._save_projects()
        handler._json({"ok": True, "task": task.to_dict()})
    except Exception as exc:
        logger.exception("task_checkpoint error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_milestones(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/milestones  — add a milestone."""
    try:
        proj_id = _proj_id(path)
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        milestone = proj.add_milestone(
            name=body.get("name", ""),
            responsible_agent_id=body.get("responsible_agent_id", ""),
            due_date=body.get("due_date", ""),
        )
        hub._save_projects()
        handler._json(milestone.to_dict())
    except Exception as exc:
        logger.exception("milestones error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_milestone_update(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/milestones/{mid}/update"""
    try:
        parts = path.split("/")
        proj_id = parts[4]
        milestone_id = parts[6]
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        milestone = proj.update_milestone(
            milestone_id,
            **{k: v for k, v in body.items() if k != "milestone_id"}
        )
        if milestone:
            hub._save_projects()
            handler._json(milestone.to_dict())
        else:
            handler._json({"error": "Milestone not found"}, 404)
    except Exception as exc:
        logger.exception("milestone_update error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_milestone_confirm(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/milestones/{mid}/confirm"""
    try:
        parts = path.split("/")
        proj_id = parts[4]
        milestone_id = parts[6]
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        ms = proj.confirm_milestone(milestone_id, by=actor_name or "admin")
        if ms:
            proj.post_message(sender="system", sender_name="System",
                content=f"\u2705 里程碑「{ms.name}」已被 {actor_name or 'admin'} 确认通过。",
                msg_type="system")
            hub._save_projects()
            auth.audit("confirm_milestone", actor=actor_name, role=user_role,
                       target=f"{proj_id}/{milestone_id}", ip=get_client_ip(handler))
            handler._json({"ok": True, "milestone": ms.to_dict()})
        else:
            handler._json({"error": "Milestone not found"}, 404)
    except Exception as exc:
        logger.exception("milestone_confirm error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_milestone_reject(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/milestones/{mid}/reject"""
    try:
        parts = path.split("/")
        proj_id = parts[4]
        milestone_id = parts[6]
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        reason = body.get("reason", "")
        ms = proj.reject_milestone(milestone_id, reason=reason,
                                    by=actor_name or "admin")
        if ms:
            proj.post_message(sender="system", sender_name="System",
                content=f"\u274c 里程碑「{ms.name}」被 {actor_name or 'admin'} 驳回。原因：{reason or '未说明'}",
                msg_type="system")
            # Notify responsible agent: trigger re-processing
            if ms.responsible_agent_id:
                try:
                    trigger = (f"【里程碑驳回】里程碑「{ms.name}」被 admin 驳回。\n"
                               f"原因：{reason or '未说明'}\n请修正后重新提交。")
                    threading.Thread(
                        target=hub.project_chat_engine._agent_respond,
                        args=(proj, ms.responsible_agent_id, trigger),
                        daemon=True
                    ).start()
                except Exception as e:
                    logger.debug("Failed to start agent respond thread: %s", e)
            hub._save_projects()
            auth.audit("reject_milestone", actor=actor_name, role=user_role,
                       target=f"{proj_id}/{milestone_id}", ip=get_client_ip(handler))
            handler._json({"ok": True, "milestone": ms.to_dict()})
        else:
            handler._json({"error": "Milestone not found"}, 404)
    except Exception as exc:
        logger.exception("milestone_reject error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_goals(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/goals  — add a goal."""
    try:
        proj_id = _proj_id(path)
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        g = proj.add_goal(
            name=body.get("name", ""),
            description=body.get("description", ""),
            owner_agent_id=body.get("owner_agent_id", ""),
            metric=body.get("metric", "count"),
            target_value=float(body.get("target_value", 0) or 0),
            target_text=body.get("target_text", ""),
        )
        hub._save_projects()
        handler._json(g.to_dict())
    except Exception as exc:
        logger.exception("goals error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_goal_update(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/goals/{gid}/update"""
    try:
        parts = path.split("/")
        proj_id = parts[4]
        goal_id = parts[6]
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        allowed = {k: v for k, v in body.items()
                   if k in ("name", "description", "owner_agent_id", "metric",
                            "target_value", "current_value", "target_text", "done",
                            "linked_milestone_ids", "linked_deliverable_ids")}
        g = proj.update_goal(goal_id, **allowed)
        if g:
            hub._save_projects()
            handler._json(g.to_dict())
        else:
            handler._json({"error": "Goal not found"}, 404)
    except Exception as exc:
        logger.exception("goal_update error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_goal_progress(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/goals/{gid}/progress"""
    try:
        parts = path.split("/")
        proj_id = parts[4]
        goal_id = parts[6]
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        g = proj.update_goal_progress(
            goal_id,
            current_value=(float(body["current_value"]) if "current_value" in body else None),
            done=(bool(body["done"]) if "done" in body else None),
        )
        if g:
            hub._save_projects()
            handler._json(g.to_dict())
        else:
            handler._json({"error": "Goal not found"}, 404)
    except Exception as exc:
        logger.exception("goal_progress error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_goal_delete(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/goals/{gid}/delete"""
    try:
        parts = path.split("/")
        proj_id = parts[4]
        goal_id = parts[6]
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        ok = proj.remove_goal(goal_id)
        if ok:
            hub._save_projects()
        handler._json({"ok": ok})
    except Exception as exc:
        logger.exception("goal_delete error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_deliverables(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/deliverables  — add a deliverable."""
    try:
        proj_id = _proj_id(path)
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        dv = proj.add_deliverable(
            title=body.get("title", ""),
            kind=body.get("kind", "document"),
            author_agent_id=body.get("author_agent_id", ""),
            task_id=body.get("task_id", ""),
            milestone_id=body.get("milestone_id", ""),
            content_text=body.get("content_text", ""),
            file_path=body.get("file_path", ""),
            url=body.get("url", ""),
        )
        hub._save_projects()
        handler._json(dv.to_dict())
    except Exception as exc:
        logger.exception("deliverables error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_deliverable_update(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/deliverables/{did}/update"""
    try:
        parts = path.split("/")
        proj_id = parts[4]
        dv_id = parts[6]
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        allowed = {k: v for k, v in body.items()
                   if k in ("title", "kind", "content_text", "file_path", "url",
                            "task_id", "milestone_id")}
        dv = proj.update_deliverable(dv_id, **allowed)
        if dv:
            hub._save_projects()
            handler._json(dv.to_dict())
        else:
            handler._json({"error": "Deliverable not found"}, 404)
    except Exception as exc:
        logger.exception("deliverable_update error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_deliverable_submit(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/deliverables/{did}/submit"""
    try:
        parts = path.split("/")
        proj_id = parts[4]
        dv_id = parts[6]
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        dv = proj.submit_deliverable(dv_id)
        if dv:
            hub._save_projects()
            handler._json(dv.to_dict())
        else:
            handler._json({"error": "Deliverable not found"}, 404)
    except Exception as exc:
        logger.exception("deliverable_submit error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_deliverable_review(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/deliverables/{did}/review"""
    try:
        parts = path.split("/")
        proj_id = parts[4]
        dv_id = parts[6]
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        approved = bool(body.get("approved", False))
        comment = body.get("comment", "") or ""
        dv = proj.review_deliverable(dv_id, approved=approved,
                                       reviewer=actor_name or "admin",
                                       comment=comment)
        if dv:
            # Notify author agent on rejection so it knows to revise.
            if (not approved) and dv.author_agent_id:
                try:
                    trigger = (f"【交付物被驳回】「{dv.title}」\n"
                               f"审阅意见：{comment or '未说明'}\n请修正后重新提交。")
                    threading.Thread(
                        target=hub.project_chat_engine._agent_respond,
                        args=(proj, dv.author_agent_id, trigger),
                        daemon=True,
                    ).start()
                except Exception as e:
                    logger.debug("Failed to start deliverable review thread: %s", e)
            hub._save_projects()
            auth.audit("review_deliverable", actor=actor_name, role=user_role,
                       target=f"{proj_id}/{dv_id}/{'approve' if approved else 'reject'}",
                       ip=get_client_ip(handler))
            handler._json({"ok": True, "deliverable": dv.to_dict()})
        else:
            handler._json({"error": "Deliverable not found"}, 404)
    except Exception as exc:
        logger.exception("deliverable_review error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_deliverable_delete(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/deliverables/{did}/delete"""
    try:
        parts = path.split("/")
        proj_id = parts[4]
        dv_id = parts[6]
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        ok = proj.remove_deliverable(dv_id)
        if ok:
            hub._save_projects()
        handler._json({"ok": ok})
    except Exception as exc:
        logger.exception("deliverable_delete error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_issues(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/issues  — add an issue."""
    try:
        proj_id = _proj_id(path)
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        iss = proj.add_issue(
            title=body.get("title", ""),
            description=body.get("description", ""),
            severity=body.get("severity", "medium"),
            reporter=body.get("reporter", "") or actor_name or "user",
            assigned_to=body.get("assigned_to", ""),
            related_task_id=body.get("related_task_id", ""),
            related_milestone_id=body.get("related_milestone_id", ""),
        )
        hub._save_projects()
        handler._json(iss.to_dict())
    except Exception as exc:
        logger.exception("issues error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_issue_update(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/issues/{iid}/update"""
    try:
        parts = path.split("/")
        proj_id = parts[4]
        iss_id = parts[6]
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        allowed = {k: v for k, v in body.items()
                   if k in ("title", "description", "severity", "status",
                            "assigned_to", "related_task_id", "related_milestone_id")}
        iss = proj.update_issue(iss_id, **allowed)
        if iss:
            hub._save_projects()
            handler._json(iss.to_dict())
        else:
            handler._json({"error": "Issue not found"}, 404)
    except Exception as exc:
        logger.exception("issue_update error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_issue_resolve(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/issues/{iid}/resolve"""
    try:
        parts = path.split("/")
        proj_id = parts[4]
        iss_id = parts[6]
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        iss = proj.resolve_issue(
            iss_id,
            resolution=body.get("resolution", ""),
            status=body.get("status", "resolved"),
        )
        if iss:
            hub._save_projects()
            handler._json(iss.to_dict())
        else:
            handler._json({"error": "Issue not found"}, 404)
    except Exception as exc:
        logger.exception("issue_resolve error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_issue_delete(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/issues/{iid}/delete"""
    try:
        parts = path.split("/")
        proj_id = parts[4]
        iss_id = parts[6]
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        ok = proj.remove_issue(iss_id)
        if ok:
            hub._save_projects()
        handler._json({"ok": ok})
    except Exception as exc:
        logger.exception("issue_delete error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_approve_step(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/tasks/{tid}/approve-step  — approve workflow step."""
    try:
        parts = path.split("/")
        proj_id = parts[4]
        task_id = parts[6]
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        task = next((t for t in proj.tasks if t.id == task_id), None)
        if not task:
            handler._json({"error": "Task not found"}, 404)
            return True
        md = getattr(task, "metadata", None) or {}
        if not md.get("pending_approval"):
            handler._json({"error": "Task is not awaiting approval"}, 400)
            return True
        try:
            task.metadata["pending_approval"] = False
            task.metadata["approved_by"] = actor_name or "admin"
            task.metadata["approved_at"] = time.time()
        except Exception:
            pass
        # Mark as in-progress and fire the agent
        try:
            from ...project import ProjectTaskStatus as _PTS
            task.status = _PTS.IN_PROGRESS
        except Exception:
            task.status = "in_progress"
        task.updated_at = time.time()
        step_name = re.sub(r'^\[WF Step \d+\]\s*', '', task.title)
        proj.post_message(
            sender="system", sender_name="System",
            content=f"\u2705 步骤「{step_name}」已获 {actor_name or 'admin'} 批准，开始执行。",
            msg_type="system",
        )
        if task.assigned_to:
            try:
                trigger = (
                    f"【人工已批准】步骤「{step_name}」已通过人工审核，"
                    f"请按原定职责立即开始执行该步骤。"
                    f"完成后请在回复中包含 \u2705 和 '已完成' 来标记步骤完成。"
                )
                threading.Thread(
                    target=hub.project_chat_engine._agent_respond,
                    args=(proj, task.assigned_to, trigger),
                    daemon=True,
                ).start()
            except Exception:
                pass
        hub._save_projects()
        auth.audit("approve_step", actor=actor_name, role=user_role,
                   target=f"{proj_id}/{task_id}", ip=get_client_ip(handler))
        handler._json({"ok": True, "task": task.to_dict()})
    except Exception as exc:
        logger.exception("approve_step error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_status(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/status  — lifecycle transition."""
    try:
        proj_id = _proj_id(path)
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        new_status = (body.get("status") or "").strip().lower()
        reason = body.get("reason", "") or ""
        ok, msg = proj.set_status(new_status, by=actor_name or "admin", reason=reason)
        if not ok:
            handler._json({"error": msg}, 400)
            return True
        try:
            label_map = {
                "planning": "未开始",
                "active": "进行中",
                "suspended": "挂起",
                "cancelled": "停止",
                "completed": "结束",
                "archived": "归档",
            }
            label = label_map.get(new_status, new_status)
            proj.post_message(
                sender="system", sender_name="System",
                content=f"\U0001f4cc 项目状态变更：{msg}（{label}）" + (f" — 原因：{reason}" if reason else ""),
                msg_type="system",
            )
        except Exception:
            pass
        hub._save_projects()
        auth.audit("project_status", actor=actor_name, role=user_role,
                   target=f"{proj_id}:{new_status}", ip=get_client_ip(handler))
        handler._json({"ok": True, "status": new_status, "transition": msg})
    except Exception as exc:
        logger.exception("status error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_pause(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/pause"""
    try:
        proj_id = _proj_id(path)
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        reason = body.get("reason", "")
        proj.pause(by=actor_name or "admin", reason=reason)
        proj.post_message(sender="system", sender_name="System",
            content=f"\u23f8\ufe0f 项目已被 {actor_name or 'admin'} 暂停。原因：{reason or '未说明'}",
            msg_type="system")
        hub._save_projects()
        auth.audit("pause_project", actor=actor_name, role=user_role,
                   target=proj_id, ip=get_client_ip(handler))
        handler._json({"ok": True, "paused": True})
    except Exception as exc:
        logger.exception("pause error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True


def _handle_resume(handler, path, hub, body, auth, actor_name, user_role) -> bool:
    """POST /api/portal/projects/{id}/resume"""
    try:
        proj_id = _proj_id(path)
        proj = hub.get_project(proj_id)
        if not proj:
            handler._json({"error": "Project not found"}, 404)
            return True
        proj.resume(by=actor_name or "admin")
        proj.post_message(sender="system", sender_name="System",
            content=f"\u25b6\ufe0f 项目已被 {actor_name or 'admin'} 恢复运行。",
            msg_type="system")
        # Replay messages queued during pause
        queued = proj.drain_paused_queue()
        for q in queued:
            try:
                hub.project_chat_engine.handle_user_message(
                    proj, q.get("content", ""),
                    target_agents=q.get("target_agents"))
            except Exception:
                pass
        # Trigger auto-wake
        try:
            hub.project_chat_engine._resume_auto_wake(proj)
        except Exception:
            pass
        hub._save_projects()
        auth.audit("resume_project", actor=actor_name, role=user_role,
                   target=proj_id, ip=get_client_ip(handler))
        handler._json({"ok": True, "paused": False, "replayed": len(queued)})
    except Exception as exc:
        logger.exception("resume error: %s", exc)
        handler._json({"error": str(exc)}, 500)
    return True
