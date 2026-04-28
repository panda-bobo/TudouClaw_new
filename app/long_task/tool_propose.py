"""Builtin tool ``propose_decomposition`` — agent-callable.

Called by a main/architect agent when it judges a project task is too
big to handle alone. The tool persists a Draft (does NOT create
ProjectTasks) and returns instructions for the agent to tell the user
to confirm via the UI.

Schema (the JSON the LLM emits as tool args):

.. code-block:: json

   {
     "parent_task_id": "tsk_xxx",      // the big task being decomposed
     "title": "Decompose: build admin panel",
     "summary": "Plain-language pitch of the decomposition strategy",
     "prd": "# PRD\\nFull markdown PRD body",
     "scaffold_dirs": ["backend/auth", "frontend/pages"],
     "sub_tasks": [
        {
          "title": "Auth backend",
          "description": "Implement login/logout/JWT issuance",
          "role_hint": "coder",
          "output_path": "backend/auth",
          "acceptance": "POST /api/auth/login returns valid JWT",
          "order": 1,
          "depends_on": []
        },
        ...
     ]
   }

The dispatcher (``app/tools.py``) wires the agent's id into a
``_caller_agent_id`` kwarg before calling — we use it to record who
proposed the draft.
"""
from __future__ import annotations

import logging
from typing import Any

from .draft_store import get_draft_store
from .models import Draft, SubTaskSpec

logger = logging.getLogger("tudouclaw.long_task.tool_propose")


def _resolve_project_id(parent_task_id: str, project_id_hint: str) -> str:
    """Find the project that owns ``parent_task_id``. ``project_id_hint``
    (if non-empty) takes priority — most chat dispatch flows already
    snapshot the project context into the tool kwargs."""
    if project_id_hint:
        return project_id_hint
    if not parent_task_id:
        return ""
    # Walk hub.list_projects() looking for the task. Cheap enough at
    # MVP scale (< 100 projects); optimize if it ever becomes hot.
    try:
        from ..hub import get_hub
        hub = get_hub()
    except Exception:
        return ""
    try:
        for p in (hub.list_projects() or []):
            for t in (p.tasks or []):
                if t.id == parent_task_id:
                    return p.id
    except Exception as e:
        logger.debug("project resolution failed: %s", e)
    return ""


def _validate_sub_tasks(raw: list) -> tuple[list[SubTaskSpec], str]:
    """Coerce raw dicts into SubTaskSpec + return error string on
    validation failure (or '' if all good)."""
    if not isinstance(raw, list) or not raw:
        return [], "sub_tasks must be a non-empty array"
    specs: list[SubTaskSpec] = []
    seen_ids: set[str] = set()
    for i, st in enumerate(raw):
        if not isinstance(st, dict):
            return [], f"sub_tasks[{i}] must be an object, got {type(st).__name__}"
        title = (st.get("title") or "").strip()
        if not title:
            return [], f"sub_tasks[{i}].title is required"
        spec = SubTaskSpec.from_dict(st)
        if spec.id in seen_ids:
            return [], f"sub_tasks[{i}].id collision: {spec.id!r}"
        seen_ids.add(spec.id)
        specs.append(spec)
    # Validate depends_on references resolve within the batch
    for spec in specs:
        for dep in spec.depends_on:
            if dep not in seen_ids:
                return [], (f"sub_task {spec.id!r}: depends_on references "
                            f"unknown sub_task id {dep!r}")
    return specs, ""


def _tool_propose_decomposition(
    parent_task_id: str = "",
    title: str = "",
    summary: str = "",
    prd: str = "",
    scaffold_dirs: list | None = None,
    sub_tasks: list | None = None,
    # injected by the dispatcher — caller's identity + scope snapshot
    _caller_agent_id: str = "",
    _project_id: str = "",
    **_extra,
) -> dict:
    """Tool body. Returns a JSON-serializable dict the LLM gets back.

    Successful response shape:
      {ok: true, draft_id, project_id, sub_task_count, message}
    The ``message`` is a one-paragraph instruction the LLM should relay
    to the user verbatim (so the user knows to look at the UI for
    confirmation).
    """
    parent_task_id = (parent_task_id or "").strip()
    if not parent_task_id:
        return {"ok": False, "error": "parent_task_id is required"}
    project_id = _resolve_project_id(parent_task_id, _project_id)
    if not project_id:
        return {
            "ok": False,
            "error": f"could not resolve project for parent_task_id={parent_task_id!r}; "
                     "make sure you're inside a project context.",
        }

    # ── Preprocessor hook: small-model first-pass draft ─────────────────
    # When the calling LLM passes empty ``sub_tasks`` AND its agent has
    # preprocessor configured, route to the small local model to generate
    # a rough draft. The big LLM then sees the response and can call
    # propose_decomposition again with refinements, OR the user can edit
    # in the UI before confirming. Failure → fall through (caller's
    # responsibility to provide sub_tasks).
    if not (sub_tasks or []):
        try:
            from .. import preprocessing as _prep
            from ..api.deps.hub import get_hub
            hub = get_hub()
            agent = hub.get_agent(_caller_agent_id) if (hub and _caller_agent_id) else None
            if agent and _prep.is_enabled(agent):
                res = _prep.invoke(
                    agent,
                    kind="task_decompose",
                    payload={
                        "intent": (title + "\n" + (summary or "")
                                   if title else (summary or "")),
                        "n": 4,
                        "context": (prd or "")[:1500],
                    },
                    timeout_s=15.0,
                )
                if res.ok and isinstance(res.value, dict):
                    drafted = res.value.get("sub_tasks") or []
                    if drafted:
                        sub_tasks = drafted
                        logger.info(
                            "preprocessor.task_decompose agent=%s drafted "
                            "%d sub-tasks (%dms, cache_hit=%s)",
                            agent.id, len(drafted), res.latency_ms, res.cache_hit,
                        )
        except Exception as _pe:
            logger.debug("preprocessor.task_decompose failed (non-fatal): %s", _pe)

    specs, err = _validate_sub_tasks(sub_tasks or [])
    if err:
        return {"ok": False, "error": err}

    # Number-of-agents requires user confirm (per Q2 design decision):
    # we flag the count in the response so the LLM relays it clearly.
    distinct_roles = len({s.role_hint for s in specs})

    draft = Draft(
        project_id=project_id,
        parent_task_id=parent_task_id,
        proposed_by_agent_id=_caller_agent_id,
        title=title.strip() or f"Decompose task {parent_task_id[:8]}",
        summary=summary.strip(),
        prd=prd or "",
        prd_source="agent_generated" if prd else "none",
        scaffold_dirs=list(scaffold_dirs or []),
        sub_tasks=specs,
    )
    get_draft_store().save(draft)

    logger.info(
        "Decomposition draft %s saved: project=%s parent=%s sub_tasks=%d "
        "by agent=%s",
        draft.id, project_id, parent_task_id, len(specs),
        _caller_agent_id[:8] if _caller_agent_id else "?",
    )

    return {
        "ok": True,
        "draft_id": draft.id,
        "project_id": project_id,
        "parent_task_id": parent_task_id,
        "sub_task_count": len(specs),
        "distinct_roles": distinct_roles,
        "message": (
            f"已为任务 {parent_task_id[:8]} 提交拆分草稿,共 {len(specs)} 个子任务、"
            f"涉及 {distinct_roles} 种角色。请用户在【项目 → 任务面板】打开"
            "「拆分草稿」卡片确认或调整,**确认后才会真正派单开跑**。"
            "在用户确认前你不要继续执行子任务。"
        ),
    }
