"""Memory reference REST — view / delete agent-private L3 memory entries.

UX: every assistant message carries a ``memory_refs`` field listing the
L3 memory entries the agent consulted this turn (via memory_recall).
The portal renders a 🧠 badge; clicking a ref shows its details; if the
user flags it as wrong, we simply DELETE the row — the next time the
agent needs that info, it'll miss in memory, explore fresh, and
save_experience will mirror a new (correct) version into L3.

Endpoints (all portal-auth gated):
    GET    /api/portal/memory/{fact_id}        → full fact
    DELETE /api/portal/memory/{fact_id}        → flag-incorrect flow
    POST   /api/portal/memory/bulk_delete      → {ids: [...]}  bulk
    GET    /api/portal/memory/stats            → counts per agent / category
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..deps.auth import CurrentUser, get_current_user


logger = logging.getLogger("tudouclaw.api.memory_refs")
router = APIRouter(prefix="/api/portal/memory", tags=["memory_refs"])


def _get_mm():
    from ...core.memory import get_memory_manager
    return get_memory_manager()


def _fact_to_dict(f) -> dict:
    return {
        "id": f.id,
        "agent_id": f.agent_id,
        "category": f.category,
        "content": f.content,
        "source": f.source or "",
        "confidence": round(float(f.confidence or 0.0), 3),
        "created_at": f.created_at or 0.0,
        "updated_at": f.updated_at or 0.0,
    }


# ── single-row ─────────────────────────────────────────────────────


@router.get("/stats")
async def memory_stats(
    agent_id: str = "",
    user: CurrentUser = Depends(get_current_user),
):
    """Counts per category for a given agent (empty agent_id = globally
    summarize via hub)."""
    mm = _get_mm()
    if mm is None:
        raise HTTPException(503, "memory manager unavailable")
    out: dict = {"agent_id": agent_id or "", "total": 0, "by_category": {}}
    if agent_id:
        facts = mm.get_recent_facts(agent_id, limit=10000)
    else:
        # aggregate across all agents in the hub
        try:
            from ...hub import get_hub
            hub = get_hub()
            facts = []
            for aid in (hub.agents.keys() if hub else []):
                facts.extend(mm.get_recent_facts(aid, limit=10000))
        except Exception:
            facts = []
    out["total"] = len(facts)
    for f in facts:
        c = f.category or "general"
        out["by_category"][c] = out["by_category"].get(c, 0) + 1
    return out


@router.get("/{fact_id}")
async def memory_get(
    fact_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    mm = _get_mm()
    if mm is None:
        raise HTTPException(503, "memory manager unavailable")
    with mm._rlock:
        row = mm._conn.execute(
            "SELECT * FROM memory_semantic WHERE id=?", (fact_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, f"memory {fact_id} not found")
    from ...core.memory import SemanticFact
    f = SemanticFact.from_dict(dict(row))
    return _fact_to_dict(f)


@router.delete("/{fact_id}")
async def memory_delete(
    fact_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    """Flag-incorrect flow: the user thinks this memory is wrong, so we
    DELETE it. Next time the agent needs this info, it'll miss in
    memory_recall, explore fresh, and save_experience will write the
    corrected version."""
    mm = _get_mm()
    if mm is None:
        raise HTTPException(503, "memory manager unavailable")
    with mm._rlock:
        row = mm._conn.execute(
            "SELECT id, agent_id, content FROM memory_semantic WHERE id=?",
            (fact_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(404, f"memory {fact_id} not found")
    mm.delete_fact(fact_id)
    logger.info(
        "memory flagged-incorrect + deleted: id=%s agent=%s",
        fact_id, row["agent_id"],
    )
    return {
        "ok": True,
        "deleted_id": fact_id,
        "agent_id": row["agent_id"],
        "preview": (row["content"] or "")[:120],
    }


# ── bulk ───────────────────────────────────────────────────────────


class BulkDeleteRequest(BaseModel):
    ids: list[str]


@router.post("/bulk_delete")
async def memory_bulk_delete(
    req: BulkDeleteRequest,
    user: CurrentUser = Depends(get_current_user),
):
    if not req.ids:
        raise HTTPException(400, "ids is required")
    mm = _get_mm()
    if mm is None:
        raise HTTPException(503, "memory manager unavailable")
    deleted = 0
    skipped = 0
    for fid in req.ids:
        with mm._rlock:
            row = mm._conn.execute(
                "SELECT id FROM memory_semantic WHERE id=?", (fid,),
            ).fetchone()
        if row is None:
            skipped += 1
            continue
        mm.delete_fact(fid)
        deleted += 1
    return {"deleted": deleted, "skipped": skipped, "requested": len(req.ids)}


# ── Dream — full-sweep memory maintenance ──────────────────────────
# Inspired by gbrain's `dream` command. Runs on-demand (this endpoint)
# OR on a daily cron (registered at app startup, see hub init). Reports
# back what got cleaned so the user can audit.

@router.post("/dream")
async def memory_dream(
    user: CurrentUser = Depends(get_current_user),
):
    """Trigger a full-sweep memory maintenance pass.

    Walks every agent, runs MemoryConsolidator(force=True), prunes
    "never-accessed + low-confidence" L3 facts, and audits the shared
    knowledge base for entries no fact ever cites.

    Returns ``DreamReport.to_dict()``. The same report's markdown
    rendering is available at ``GET /api/portal/memory/dream/last``.
    """
    from ...core.memory_dream import get_memory_dream
    from ..deps.hub import get_hub
    # No DI on the dream singleton — it lazily binds to the global
    # MemoryManager. Hub injection just makes agent enumeration cleaner.
    try:
        hub = get_hub()
    except Exception:
        hub = None
    dream = get_memory_dream()
    if hub is not None and dream._hub is None:
        dream._hub = hub
    # llm_call=None for now; consolidator's similarity-merge step has a
    # cheap deterministic fallback. P2 will wire the preprocessor model
    # in here once it lands.
    report = dream.dream_all(llm_call=None)
    return report.to_dict()


@router.get("/dream/last")
async def memory_dream_last(
    user: CurrentUser = Depends(get_current_user),
):
    """Return the most recent dream report (or empty if never run)."""
    from ...core.memory_dream import get_memory_dream
    dream = get_memory_dream()
    last = dream.last_report()
    if last is None:
        return {"available": False}
    return {
        "available": True,
        "report": last.to_dict(),
        "markdown": last.to_markdown(),
    }
