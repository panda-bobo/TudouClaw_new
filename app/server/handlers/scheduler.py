"""


╔════════════════════════════════════════════════════════════════════════╗
║  ⚠️  DEPRECATED — LEGACY stdlib handler (extracted from                ║
║     portal_routes_post.py). Active only when TUDOU_USE_STDLIB=1 is     ║
║     set at launch. FastAPI (app/api/routers/*) is authoritative.       ║
║     Do NOT add new routes here.                                        ║
╚════════════════════════════════════════════════════════════════════════╝
Scheduler route handlers extracted from portal_routes_post.py.

Handles scheduler job management endpoints:
  - POST /api/portal/scheduler/jobs  (action=create|update|delete|trigger|toggle)
"""
import logging

from ...scheduler import get_scheduler, PRESET_JOBS
from ..portal_auth import get_client_ip

logger = logging.getLogger("tudou.portal")


def try_handle(handler, path: str, hub, body: dict, auth,
               actor_name: str, user_role: str) -> bool:
    """Handle scheduler job management endpoints.

    Returns True if the path was handled, False otherwise.
    """

    if path != "/api/portal/scheduler/jobs":
        return False

    scheduler = get_scheduler()
    action = body.get("action", "create")

    if action == "create":
        agent_id = body.get("agent_id", "")
        target_type = body.get("target_type", "chat")
        workflow_id = body.get("workflow_id", "")
        # workflow jobs may omit agent_id if step_assignments cover all steps;
        # otherwise agent_id is the default assignee.
        if not agent_id and target_type != "workflow" and not workflow_id:
            handler._json({"error": "agent_id required"}, 400)
            return True
        # Support creating from preset
        preset_id = body.get("preset_id", "")
        if preset_id and preset_id in PRESET_JOBS:
            preset = dict(PRESET_JOBS[preset_id])
            preset.update({k: v for k, v in body.items()
                           if k not in ("action", "preset_id") and v})
            job = scheduler.add_job(agent_id=agent_id, **preset)
        else:
            job = scheduler.add_job(
                agent_id=agent_id,
                name=body.get("name", ""),
                description=body.get("description", ""),
                job_type=body.get("job_type", "one_time"),
                cron_expr=body.get("cron_expr", ""),
                prompt_template=body.get("prompt_template", ""),
                template_ids=body.get("template_ids", []),
                notify_channels=body.get("notify_channels", []),
                notify_on=body.get("notify_on", "always"),
                tags=body.get("tags", []),
                timeout=body.get("timeout", 300),
                max_runs=body.get("max_runs", 0),
                target_type=target_type,
                workflow_id=workflow_id,
                workflow_step_assignments=body.get("workflow_step_assignments", []) or [],
                workflow_input=body.get("workflow_input", ""),
            )
        auth.audit("create_scheduled_job", actor=actor_name,
                   role=user_role, target=job.id, ip=get_client_ip(handler))
        handler._json(job.to_dict())

    elif action == "update":
        job_id = body.get("job_id", "")
        updates = {k: v for k, v in body.items()
                   if k not in ("action", "job_id")}
        job = scheduler.update_job(job_id, **updates)
        if job:
            auth.audit("update_scheduled_job", actor=actor_name,
                       role=user_role, target=job_id, ip=get_client_ip(handler))
            handler._json(job.to_dict())
        else:
            handler._json({"error": "Job not found"}, 404)

    elif action == "delete":
        job_id = body.get("job_id", "")
        ok = scheduler.remove_job(job_id)
        if ok:
            auth.audit("delete_scheduled_job", actor=actor_name,
                       role=user_role, target=job_id, ip=get_client_ip(handler))
        handler._json({"ok": ok})

    elif action == "trigger":
        job_id = body.get("job_id", "")
        ok = scheduler.trigger_now(job_id)
        auth.audit("trigger_scheduled_job", actor=actor_name,
                   role=user_role, target=job_id, ip=get_client_ip(handler))
        handler._json({"ok": ok})

    elif action == "toggle":
        job_id = body.get("job_id", "")
        enabled = body.get("enabled", True)
        job = scheduler.update_job(job_id, enabled=enabled)
        if job:
            handler._json(job.to_dict())
        else:
            handler._json({"error": "Job not found"}, 404)

    else:
        handler._json({"error": f"Unknown action: {action}"}, 400)

    return True
