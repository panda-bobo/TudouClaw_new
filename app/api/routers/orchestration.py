"""Orchestration view endpoints — feeds the operator dashboard.

Three read-only endpoints:
  • ``GET /orchestration/overview``  — at-a-glance system health
  • ``GET /orchestration/agents``    — agent leaderboard (success rate)
  • ``GET /orchestration/pipelines`` — long-task pipeline (parent tasks
                                       + child status + aggregator state)

All queries are pure aggregation over already-persisted state. No new
schema. Designed to be cheap enough that the orchestration page can
poll on a 5-10s cadence.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..deps.hub import get_hub
from ..deps.auth import CurrentUser, get_current_user

logger = logging.getLogger("tudouclaw.api.orchestration")

router = APIRouter(prefix="/api/portal/orchestration", tags=["orchestration"])


@router.get("/overview")
async def get_overview(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """At-a-glance system stats. Aggregated over the last hour."""
    agents = list((hub.agents or {}).values()) if hasattr(hub, "agents") else []
    total_events = sum(len(getattr(a, "events", []) or []) for a in agents)
    # Tokens
    total_in = 0
    total_out = 0
    for a in agents:
        try:
            total_in += int(getattr(a, "total_input_tokens", 0) or 0)
            total_out += int(getattr(a, "total_output_tokens", 0) or 0)
        except Exception:
            pass
    # Project + long-task pipeline counts
    proj_count = 0
    parent_task_count = 0
    in_flight_subtasks = 0
    aggregated_count = 0
    try:
        from ..deps.hub import get_hub as _gh  # noqa: F401
        from ...project import ProjectTaskStatus
        # Raw Project objects — list_projects() returns dicts.
        for p in list((hub.projects or {}).values()):
            proj_count += 1
            for t in (p.tasks or []):
                if getattr(t, "parent_task_id", ""):
                    if t.status == ProjectTaskStatus.IN_PROGRESS:
                        in_flight_subtasks += 1
                else:
                    # Possible parent — check if has children
                    has_children = any(
                        getattr(c, "parent_task_id", "") == t.id
                        for c in (p.tasks or [])
                    )
                    if has_children:
                        parent_task_count += 1
                        if (t.metadata or {}).get("aggregated"):
                            aggregated_count += 1
    except Exception as e:
        logger.debug("overview project scan failed: %s", e)

    # Agent status breakdown
    status_counts = {"idle": 0, "busy": 0, "error": 0, "offline": 0}
    for a in agents:
        st = getattr(a, "status", None)
        sv = getattr(st, "value", st) or "offline"
        status_counts[str(sv)] = status_counts.get(str(sv), 0) + 1

    return {
        "agent_count": len(agents),
        "agent_status": status_counts,
        "total_events": total_events,
        "tokens": {
            "in": total_in,
            "out": total_out,
            "total": total_in + total_out,
        },
        "projects": {
            "count": proj_count,
            "parent_tasks": parent_task_count,
            "in_flight_subtasks": in_flight_subtasks,
            "aggregated": aggregated_count,
        },
        "ts": time.time(),
    }


@router.get("/agents")
async def get_agent_leaderboard(
    limit: int = Query(50, ge=1, le=200),
    role: Optional[str] = Query(None),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Agent leaderboard sorted by success rate.

    Smoothed rate: ``(success + 1) / (success + fail + 2)`` so brand-new
    agents start at 50% and aren't punished by zero data. Same formula
    as ``long_task.auto_assign._success_rate``.
    """
    agents = list((hub.agents or {}).values()) if hasattr(hub, "agents") else []
    rows = []
    for a in agents:
        if role and (a.role or "").lower() != role.lower():
            continue
        s = int(getattr(a, "role_success_count", 0) or 0)
        f = int(getattr(a, "role_fail_count", 0) or 0)
        rate = (s + 1) / (s + f + 2)
        last_at = float(getattr(a, "role_last_success_at", 0) or 0)
        # Status surface for the UI
        st = getattr(a, "status", None)
        st_val = getattr(st, "value", st) or "offline"
        rows.append({
            "id": a.id,
            "name": a.name,
            "role": a.role,
            "label": f"{a.role or '?'}-{a.name or '?'}",
            "success_count": s,
            "fail_count": f,
            "total_count": s + f,
            "success_rate": round(rate, 4),
            "last_success_at": last_at,
            "status": str(st_val),
        })
    # Sort: agents with real history first (push 0/0 to the bottom — their
    # 50% smoothed score is a prior, not a measurement). Within each group:
    # rate desc, then total_count desc (more data = more trustworthy).
    rows.sort(key=lambda r: (
        0 if r["total_count"] > 0 else 1,
        -r["success_rate"],
        -r["total_count"],
    ))
    return {
        "agents": rows[:limit],
        "total": len(rows),
        "ts": time.time(),
    }


@router.get("/pipelines")
async def get_pipelines(
    limit: int = Query(20, ge=1, le=100),
    include_done: bool = Query(False),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """In-flight long-task pipelines (parent + children + aggregator).

    Each entry: parent task title + child status breakdown + aggregator
    mode + result hint.
    """
    out = []
    try:
        from ...project import ProjectTaskStatus
        # Raw Project objects — list_projects() returns dicts.
        for p in list((hub.projects or {}).values()):
            tasks_by_id = {t.id: t for t in (p.tasks or [])}
            # Find parent tasks (those with at least one child)
            parent_ids: set[str] = set()
            for t in (p.tasks or []):
                pid = getattr(t, "parent_task_id", "") or ""
                if pid and pid in tasks_by_id:
                    parent_ids.add(pid)
            for pid in parent_ids:
                parent = tasks_by_id.get(pid)
                if not parent:
                    continue
                meta = parent.metadata or {}
                aggregated = bool(meta.get("aggregated"))
                if aggregated and not include_done:
                    continue
                children = [t for t in (p.tasks or [])
                            if getattr(t, "parent_task_id", "") == pid]
                child_status_counts = {}
                child_rows = []
                for c in children:
                    sv = getattr(c.status, "value", c.status) or "?"
                    child_status_counts[sv] = child_status_counts.get(sv, 0) + 1
                    cmeta = c.decomp_metadata or {}
                    cmeta_full = c.metadata or {}
                    # Look up assigned agent name for inline display
                    agent_name = ""
                    if c.assigned_to and hasattr(hub, "get_agent"):
                        ag = hub.get_agent(c.assigned_to)
                        if ag is not None:
                            agent_name = getattr(ag, "name", "") or c.assigned_to
                    child_rows.append({
                        "id": c.id,
                        "title": c.title,
                        "status": str(sv),
                        "assigned_to": c.assigned_to or "",
                        "assigned_to_name": agent_name,
                        "role_hint": getattr(c, "role_hint", ""),
                        "order": int(cmeta.get("order", 0) or 0),
                        "output_path": cmeta.get("output_path", ""),
                        "depends_on": list(getattr(c, "depends_on", []) or []),
                        "assignment_reason": cmeta_full.get("assignment_reason") or {},
                    })
                child_rows.sort(key=lambda r: r["order"])
                out.append({
                    "project_id": p.id,
                    "project_name": p.name,
                    "parent_task_id": parent.id,
                    "parent_title": parent.title,
                    "parent_status": str(getattr(parent.status, "value",
                                                 parent.status) or "?"),
                    "child_count": len(children),
                    "child_status_counts": child_status_counts,
                    "children": child_rows,
                    "aggregated": aggregated,
                    "aggregator_mode": meta.get("aggregator_mode") or
                                       meta.get("aggregator_mode_hint") or
                                       "concat_markdown",
                    "aggregated_at": float(meta.get("aggregated_at", 0) or 0),
                    "result_preview": (parent.result or "")[:200],
                })
    except Exception as e:
        logger.warning("pipelines scan failed: %s", e)
    # Sort: in-flight first, then most recently aggregated
    out.sort(key=lambda r: (r["aggregated"], -r.get("aggregated_at", 0)))
    return {
        "pipelines": out[:limit],
        "total": len(out),
        "ts": time.time(),
    }


@router.get("/context-preview/{agent_id}")
async def get_context_preview(
    agent_id: str,
    project_id: str = Query("", description="Project scope (omit for non-project preview)"),
    intent: str = Query("示例任务: 检查共享上下文与 RAG 注入"),
    budget: int = Query(2000, ge=200, le=8000),
    complex_task: bool = Query(False),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Per-section context-budget preview for the orchestration UI.

    Calls the budget allocator with the given intent + project + budget
    and returns the section breakdown (name, source, budget, used,
    truncated, text). Useful for debugging "why did agent X see this
    context" and visualising token consumption per section.
    """
    agent = hub.get_agent(agent_id) if hasattr(hub, "get_agent") else None
    if agent is None:
        raise HTTPException(404, f"agent not found: {agent_id}")
    try:
        from ...shared_context import get_agent_context
        bundle = get_agent_context(
            agent_id=agent_id,
            project_id=project_id,
            intent=intent,
            role=getattr(agent, "role", "") or "",
            budget=int(budget),
            complex_task=bool(complex_task),
            history_summary_text="",  # caller-supplied; preview leaves empty
        )
    except Exception as e:
        logger.warning("context-preview failed: %s", e)
        raise HTTPException(500, f"context preview failed: {e}")
    return {
        "agent_id": agent_id,
        "agent_name": getattr(agent, "name", "") or agent_id,
        "agent_role": getattr(agent, "role", "") or "",
        "intent": intent,
        "project_id": project_id,
        "complex_task": complex_task,
        "total_budget": bundle.total_budget,
        "total_used": bundle.total_used,
        "utilization_pct": round(
            100.0 * bundle.total_used / bundle.total_budget, 1
        ) if bundle.total_budget else 0,
        "sections": [
            {
                "name": s.name,
                "source": s.source,
                "budget": s.budget,
                "used": s.used,
                "truncated": s.truncated,
                "text_preview": s.text[:280] + ("…" if len(s.text) > 280 else ""),
                "text_len_chars": len(s.text),
            }
            for s in bundle.sections
        ],
        "rendered_preview": bundle.rendered[:600] + ("…" if len(bundle.rendered) > 600 else ""),
        "ts": time.time(),
    }


@router.get("/preprocessor-metrics")
async def get_preprocessor_metrics(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Per-phase preprocessor metrics + circuit breaker snapshot.

    Used by the orchestration page to render the "预处理" card:
      * per-kind: calls / cache_hits / fallbacks / tokens_in / tokens_out
        / latency_ms_avg / cache_hit_rate
      * circuit breaker rows: agent / kind / paused / paused_remaining_s
      * agent count: how many agents have preprocessor configured

    Cheap — pure dict snapshot, no I/O.
    """
    try:
        from ...preprocessing import bridge as _prep_bridge
    except Exception as e:
        return {"error": f"preprocessor module unavailable: {e}"}

    raw = _prep_bridge.get_metrics()
    cache = raw.pop("_cache", {})
    # Enrich each kind with averages
    phases = []
    for kind, m in raw.items():
        calls = m.get("calls", 0) or 0
        hits = m.get("cache_hits", 0) or 0
        latency_total = m.get("latency_ms_total", 0) or 0
        # Real (non-cache) calls = calls - hits
        real_calls = max(1, calls - hits)
        phases.append({
            "kind": kind,
            "calls": calls,
            "cache_hits": hits,
            "fallbacks": m.get("fallbacks", 0) or 0,
            "tokens_in": m.get("tokens_in", 0) or 0,
            "tokens_out": m.get("tokens_out", 0) or 0,
            "latency_ms_avg": int(latency_total / real_calls) if calls else 0,
            "cache_hit_rate": round(hits / calls, 3) if calls else 0.0,
        })

    breaker = _prep_bridge.get_breaker_state()
    paused_count = sum(1 for r in breaker if r["paused"])

    # Count agents that have preprocessor configured
    enabled_agents = []
    for aid, a in (hub.agents or {}).items():
        if _prep_bridge.is_enabled(a):
            enabled_agents.append({
                "id": a.id,
                "name": a.name,
                "model": a.preprocessor_model,
                "endpoint": getattr(a, "preprocessor_endpoint", "") or "",
                "modes": list(getattr(a, "preprocessor_modes", []) or []),
            })

    return {
        "phases": phases,
        "cache": cache,
        "breaker": breaker,
        "breaker_paused_count": paused_count,
        "enabled_agents": enabled_agents,
        "enabled_agent_count": len(enabled_agents),
        "ts": time.time(),
    }


@router.get("/shared-context/projects")
async def list_projects_with_sc(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List projects that have any shared-context state (artifacts /
    decisions / milestones / handoffs / pending Q&A).

    UI: dropdown for the Project State viz tab.
    """
    try:
        from ...shared_context import get_shared_context_store
        store = get_shared_context_store()
    except Exception as e:
        return {"projects": [], "error": str(e)}
    # Cheap aggregation — one query per table, group by project_id
    db = store.db._conn
    project_ids: set[str] = set()
    for table in ("sc_artifacts", "sc_decisions", "sc_milestones",
                  "sc_handoffs", "sc_pending_qs"):
        try:
            for r in db.execute(f"SELECT DISTINCT project_id FROM {table}").fetchall():
                pid = r["project_id"]
                if pid:
                    project_ids.add(pid)
        except Exception:
            continue
    # Resolve project name from hub registry where possible
    projects = []
    for pid in sorted(project_ids):
        name = pid
        try:
            if hasattr(hub, "get_project"):
                proj = hub.get_project(pid)
                if proj is not None:
                    name = getattr(proj, "name", "") or pid
        except Exception:
            pass
        # Quick counts
        counts = {}
        for table, key in (
            ("sc_artifacts", "artifacts"),
            ("sc_decisions", "decisions"),
            ("sc_milestones", "milestones"),
            ("sc_handoffs", "handoffs"),
            ("sc_pending_qs", "pending_qs"),
        ):
            try:
                row = db.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE project_id = ?",
                    (pid,),
                ).fetchone()
                counts[key] = int(row["n"]) if row else 0
            except Exception:
                counts[key] = 0
        projects.append({"project_id": pid, "name": name, "counts": counts})
    return {"projects": projects, "ts": time.time()}


@router.get("/shared-context/state/{project_id}")
async def get_project_state(
    project_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Full shared-context dump for one project: artifacts / decisions /
    milestones / handoffs / pending_qs (each table's recent N rows).

    Used by the Project State viz tab to render timeline + decision log
    + Gantt + handoff network + open Q&A. Bounded fetch sizes to keep
    response < 100KB.
    """
    try:
        from ...shared_context import get_shared_context_store
        store = get_shared_context_store()
    except Exception as e:
        raise HTTPException(500, f"shared-context unavailable: {e}")
    if not project_id:
        raise HTTPException(400, "project_id required")
    out = {
        "project_id": project_id,
        "artifacts":  store.list_artifacts(project_id=project_id, status="", limit=200),
        "decisions":  store.list_decisions(project_id=project_id, status="", limit=100),
        "milestones": store.list_milestones(project_id=project_id, status="", limit=100),
        "handoffs":   store.list_handoffs(project_id=project_id, status="", limit=100),
        "pending_qs": store.list_pending_questions(project_id=project_id, status="", limit=100),
    }
    # Resolve agent names for handoffs (UI needs labels, not raw IDs)
    if hasattr(hub, "get_agent"):
        agent_ids = set()
        for a in out["artifacts"]:
            agent_ids.add(a.get("agent_id", ""))
        for h in out["handoffs"]:
            agent_ids.add(h.get("src_agent", ""))
            agent_ids.add(h.get("dst_agent", ""))
        for m in out["milestones"]:
            agent_ids.add(m.get("owner_agent", ""))
        agent_names = {}
        for aid in agent_ids:
            if not aid:
                continue
            try:
                a = hub.get_agent(aid)
                if a is not None:
                    agent_names[aid] = (a.name or aid)[:20]
            except Exception:
                continue
        out["agent_names"] = agent_names
    out["ts"] = time.time()
    return out
