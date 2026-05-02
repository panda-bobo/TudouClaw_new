"""Job scheduler router — manage scheduled jobs, presets, history."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Body

from ..deps.hub import get_hub
from ..deps.auth import CurrentUser, get_current_user

logger = logging.getLogger("tudouclaw.api.scheduler")

router = APIRouter(prefix="/api/portal/scheduler", tags=["scheduler"])


def _get_scheduler():
    from ...scheduler import get_scheduler
    return get_scheduler()


# ---------------------------------------------------------------------------
# Job listing — matches legacy portal_routes_get
# ---------------------------------------------------------------------------

@router.get("/jobs")
async def list_jobs(
    agent_id: str = Query("", description="Filter by agent ID"),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List all scheduled jobs."""
    try:
        scheduler = _get_scheduler()
        jobs = scheduler.list_jobs(agent_id=agent_id or None)
        return {"jobs": [j.to_dict() for j in jobs]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get job detail."""
    try:
        scheduler = _get_scheduler()
        job = scheduler.get_job(job_id)
        if job:
            return job.to_dict()
        raise HTTPException(404, "Job not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/jobs/{job_id}/history")
async def get_job_history(
    job_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get execution history for a job."""
    try:
        scheduler = _get_scheduler()
        history = scheduler.get_execution_history(job_id, limit=30)
        return {"history": [r.to_dict() for r in history]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Job management
# ---------------------------------------------------------------------------

@router.post("/jobs")
async def manage_jobs(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Create, update, delete, toggle, or trigger a job — matches legacy handlers/scheduler.py."""
    try:
        action = body.get("action", "create")
        scheduler = _get_scheduler()

        if action == "create":
            agent_id = body.get("agent_id", "")
            target_type = body.get("target_type", "chat")
            workflow_id = body.get("workflow_id", "")
            canvas_workflow_id = body.get("canvas_workflow_id", "")
            # agent_id is required ONLY for chat target. Both legacy
            # workflow + canvas workflow scheduling source the agent
            # from the workflow definition itself.
            if (target_type == "chat"
                    and not agent_id
                    and not workflow_id
                    and not canvas_workflow_id):
                raise HTTPException(400, "agent_id required for chat target")
            if target_type == "canvas_workflow" and not canvas_workflow_id:
                raise HTTPException(400, "canvas_workflow_id required for canvas_workflow target")
            # Support creating from preset
            preset_id = body.get("preset_id", "")
            from ...scheduler import PRESET_JOBS
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
                    canvas_workflow_id=canvas_workflow_id,
                )
            return job.to_dict()

        elif action == "update":
            job_id = body.get("job_id", "")
            updates = {k: v for k, v in body.items()
                       if k not in ("action", "job_id")}
            job = scheduler.update_job(job_id, **updates)
            if job:
                return job.to_dict()
            else:
                raise HTTPException(404, "Job not found")

        elif action == "delete":
            job_id = body.get("job_id", "")
            ok = scheduler.remove_job(job_id)
            return {"ok": ok}

        elif action in ("run", "trigger"):
            job_id = body.get("job_id", "")
            ok = scheduler.trigger_now(job_id)
            return {"ok": ok}

        elif action == "toggle":
            job_id = body.get("job_id", "")
            enabled = body.get("enabled", True)
            job = scheduler.update_job(job_id, enabled=enabled)
            if job:
                return job.to_dict()
            else:
                raise HTTPException(404, "Job not found")

        else:
            raise HTTPException(400, f"Unknown action: {action}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Job presets — matches legacy: returns {"presets": {key: value, ...}}
# ---------------------------------------------------------------------------

@router.get("/presets")
async def get_job_presets(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get job presets (pre-defined job templates)."""
    try:
        from ...scheduler import PRESET_JOBS
        return {"presets": {k: v for k, v in PRESET_JOBS.items()}}
    except (ImportError, Exception):
        return {"presets": {}}
