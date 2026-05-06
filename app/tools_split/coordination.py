"""Coordination tools — team_create, send_message, task_update.

All three operate on the hub's agent registry and task queues. Grouped
because they share the ``_get_hub()`` entry point and the "caller-agent"
context-extraction pattern via the ``_caller_agent_id`` kwarg.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from ._common import _get_hub

logger = logging.getLogger(__name__)


def _rule_engine_check_dispatch_task(*, from_agent_id: str, to_agent_id: str,
                                       brief: str, priority: int,
                                       deadline: str, project_id: str) -> str:
    """PEP for ``before_dispatch_task``. Returns deny message string
    if any matched rule says deny, else empty string.

    Context exposed:
      - from_agent / to_agent: {id, name, role} (resolved via hub)
      - brief, priority, deadline, project_id
      - to_agent_inflight: count of in-flight assignments to to_agent
      - scope: project (when project_id set) else solo (caller view)
    """
    try:
        from ..rule_engine import get_engine
    except Exception:
        return ""
    eng = get_engine()
    if eng is None:
        return ""
    hub = _get_hub()
    from_a = hub.agents.get(from_agent_id) if from_agent_id else None
    to_a = hub.agents.get(to_agent_id) if to_agent_id else None
    # Count in-flight assignments to the target — useful for capacity rules.
    inflight = 0
    try:
        from ..core.task_assignment import get_store as _get_ta_store
        ts = _get_ta_store()
        if ts and hasattr(ts, "list_for_agent"):
            for a in ts.list_for_agent(to_agent_id):
                if getattr(a, "status", "") in ("dispatched", "accepted", "in_progress"):
                    inflight += 1
    except Exception:
        pass
    if project_id:
        scope = {"kind": "project", "project_id": project_id}
    else:
        scope = {"kind": "global"}
    # Tier-2 enrichment from the dispatching agent — recent_writes,
    # recent_tool_calls, project.has_design_doc/has_plan_md/workspace_files,
    # task.status. Lets rules express e.g. "PM cannot dispatch a coding
    # task until project.has_design_doc==true".
    enrich: dict = {}
    try:
        if from_a is not None and hasattr(from_a, "_build_pep_workflow_enrichment"):
            enrich = from_a._build_pep_workflow_enrichment() or {}
    except Exception:
        enrich = {}
    agent_extra = enrich.get("agent", {})
    ctx = {
        "from_agent": {
            "id": from_agent_id,
            "name": getattr(from_a, "name", "") if from_a else "",
            "role": getattr(from_a, "role", "") if from_a else "",
        },
        "to_agent": {
            "id": to_agent_id,
            "name": getattr(to_a, "name", "") if to_a else "",
            "role": getattr(to_a, "role", "") if to_a else "",
            "inflight": inflight,
        },
        "brief": brief or "",
        "priority": int(priority or 0),
        "deadline": deadline or "",
        "agent": {
            "id": from_agent_id,
            "name": getattr(from_a, "name", "") if from_a else "",
            "role": getattr(from_a, "role", "") if from_a else "",
            **agent_extra,
        },
        "project": enrich.get("project", {"id": project_id}),
        "task": enrich.get("task", {"id": "", "title": "", "status": ""}),
        "scope": scope,
    }
    try:
        decisions = eng.evaluate("before_dispatch_task", ctx)
    except Exception:
        return ""
    for d in decisions:
        if d.matched and d.action == "deny":
            return f"Error: dispatch_task denied by rule '{d.rule_name}': {d.message}"
    return ""


# task_update result preview cap — we store completed task results on
# the parent task but truncate so a chatty worker doesn't blow up the
# agent-list JSON payload.
_BG_RESULT_PREVIEW_CHARS = 4000
_BG_EVENT_PREVIEW_CHARS = 200
_SEND_MESSAGE_PREVIEW_CHARS = 200


# ── team_create ──────────────────────────────────────────────────────

def _tool_team_create(name: str, task: str, role: str = "coder",
                      working_dir: str = "", **_: Any) -> str:
    """Spawn a background worker to run a task in parallel.

    The worker is NOT a first-class Agent — it's a transient background
    task owned by the caller. It inherits the caller's model/provider,
    runs the task to completion, pushes a result entry into the caller's
    task list, then disappears. The UI never sees it as a separate agent.
    """
    try:
        hub = _get_hub()
        caller_id = _.get("_caller_agent_id", "") if isinstance(_, dict) else ""
        parent = hub.get_agent(caller_id) if caller_id else None
        if parent is None:
            return ("Error: team_create requires a calling agent context; "
                    "none was found.")

        worker_id = uuid.uuid4().hex[:8]
        worker_label = f"{role}:{name}" if name else role

        # Record the background job as a task on the PARENT agent so the
        # user can track it from the parent's task list / execution log.
        try:
            t = parent.add_task(
                title=f"[bg:{worker_label}] {task[:80]}",
                description=task,
            )
            task_id = t.id
        except Exception:
            task_id = ""

        def _run_background():
            from ..agent import create_agent as _create_agent_fn
            try:
                # Build an ephemeral worker that inherits parent's config.
                # Resolve working directory: explicit > parent's active
                # shared workspace (project/meeting) > parent's working_dir.
                _parent_active_sw = (
                    parent.get_active_shared_workspace()
                    if hasattr(parent, "get_active_shared_workspace") else ""
                )
                _wd = working_dir or _parent_active_sw or parent.working_dir
                worker = _create_agent_fn(
                    name=f"__bg_{worker_label}_{worker_id}",
                    role=role,
                    model=parent.model,
                    provider=parent.provider,
                    working_dir=_wd,
                    node_id=parent.node_id,
                    parent_id=parent.id,
                )
                # Inherit active context so child resolves the same shared
                # workspace via project/meeting registry.
                try:
                    worker._active_context_id = getattr(
                        parent, "_active_context_id", "") or ""
                except Exception:
                    pass
                # Snapshot of legacy field for boot/sandbox path that still
                # reads it (kept until full deprecation lands).
                worker.shared_workspace = _parent_active_sw
                worker.project_id = parent.project_id
                worker.project_name = parent.project_name
                # Don't register it in the hub — it's transient.
                result_text = ""
                try:
                    result_text = worker.chat(task) or ""
                except Exception as e:
                    result_text = f"Worker error: {e}"
                # Push result back as a completed task entry on the parent.
                if task_id:
                    try:
                        parent.update_task(
                            task_id,
                            status="done",
                            result=(result_text or "")[:_BG_RESULT_PREVIEW_CHARS],
                        )
                    except Exception:
                        pass
                # Log to parent event stream for visibility.
                try:
                    parent._log("bg_task_complete", {
                        "worker": worker_label,
                        "task_id": task_id,
                        "result_preview": (result_text or "")[:_BG_EVENT_PREVIEW_CHARS],
                    })
                except Exception:
                    pass
            except Exception as e:
                if task_id:
                    try:
                        parent.update_task(task_id, status="failed",
                                           result=f"{type(e).__name__}: {e}")
                    except Exception:
                        pass

        th = threading.Thread(target=_run_background, daemon=True,
                              name=f"bg-{worker_label}-{worker_id}")
        th.start()

        return (
            f"Background worker dispatched.\n"
            f"  Role: {role}\n"
            f"  Worker: {worker_label} (id={worker_id})\n"
            f"  Task ID on parent: {task_id or '(none)'}\n"
            f"  Model inherited: {parent.model or '(default)'} @ "
            f"{parent.provider or '(default)'}\n"
            f"The worker runs in background and will post its result back "
            f"to your task list when done. It is NOT a separately managed agent."
        )
    except Exception as e:
        return f"Error dispatching background worker: {e}"


# ── Handoff envelope (P0-A) ─────────────────────────────────────
# Structure any cross-agent message / reply / handoff carries. Fields
# in priority order for recipient:
#   summary     — 1-3 sentence conclusion (ALWAYS rendered)
#   key_fields  — structured dict: decisions / numbers / names
#   artifact_refs — paths/ids pointing to large outputs on disk
#   detail      — optional long-form body (legacy `content`)
#
# When a sender only provides `content`, we auto-derive `summary` from
# its first ~200 chars so downstream rendering still stays compact.
#
# Downstream (inbox injection) renders envelope in this order. The
# agent default is "read summary + key_fields + ref list; call
# read_file(path) if you need the detail".

_ENVELOPE_SUMMARY_MAX_CHARS = 800
_ENVELOPE_DETAIL_PREVIEW_CHARS = 400


def _build_handoff_envelope(content: str, summary: str,
                            key_fields, artifact_refs) -> dict:
    """Normalize envelope fields into a dict ready to stash in inbox
    metadata. All fields optional; sensible defaults derived from
    ``content`` when omitted."""
    content = (content or "").strip()
    summary = (summary or "").strip()
    # Auto-derive summary from content if not provided — preserves
    # back-compat with callers that only pass raw content.
    if not summary and content:
        head = content.replace("\n", " ")[:_ENVELOPE_SUMMARY_MAX_CHARS]
        if len(content) > _ENVELOPE_SUMMARY_MAX_CHARS:
            head = head.rstrip() + "…"
        summary = head

    # Normalize key_fields: accept dict or None.
    if not isinstance(key_fields, dict):
        key_fields = {}
    # JSON-safe the values via default=str coercion later when stored.

    # Normalize artifact_refs: accept list/tuple/str.
    if artifact_refs is None:
        refs: list[str] = []
    elif isinstance(artifact_refs, str):
        refs = [artifact_refs] if artifact_refs.strip() else []
    else:
        try:
            refs = [str(r).strip() for r in artifact_refs if str(r).strip()]
        except Exception:
            refs = []

    return {
        "summary": summary,
        "key_fields": dict(key_fields),
        "artifact_refs": refs,
        "detail_len": len(content),
    }


def _render_envelope_for_wire(content: str, envelope: dict) -> str:
    """Compact text representation of envelope + detail for legacy
    message-content field. Used as the `content` passed through
    hub.route_message and stored on the inbox row so recipients that
    DON'T read metadata still see the summary up top."""
    lines: list[str] = []
    summary = envelope.get("summary") or ""
    if summary:
        lines.append(f"📣 Summary: {summary}")
    kf = envelope.get("key_fields") or {}
    if kf:
        try:
            import json as _json
            kf_txt = _json.dumps(kf, ensure_ascii=False, default=str)
        except Exception:
            kf_txt = str(kf)
        if len(kf_txt) > 600:
            kf_txt = kf_txt[:600] + "…"
        lines.append(f"🔑 Key: {kf_txt}")
    refs = envelope.get("artifact_refs") or []
    if refs:
        lines.append("📎 Artifacts: " + ", ".join(refs[:6])
                     + (f" (+{len(refs)-6})" if len(refs) > 6 else ""))
    if content and len(content.strip()) > 0:
        # Keep a bounded detail preview for recipients that want to see
        # the raw text without reading the artifact. Full detail still
        # lives in inbox row's metadata.detail field.
        detail = content.strip()
        if len(detail) > _ENVELOPE_DETAIL_PREVIEW_CHARS:
            detail = (detail[:_ENVELOPE_DETAIL_PREVIEW_CHARS]
                      + f"…(+{len(content) - _ENVELOPE_DETAIL_PREVIEW_CHARS} chars in detail)")
        lines.append(f"📄 Detail: {detail}")
    # If neither summary nor any structured field was given, just use
    # the raw content (legacy callers).
    return "\n".join(lines) if lines else content


# ── send_message ─────────────────────────────────────────────────────

def _tool_send_message(to_agent: str, content: str = "",
                       msg_type: str = "task",
                       thread_id: str = "",
                       reply_to: str = "",
                       priority: str = "normal",
                       ttl_s: int = 0,
                       summary: str = "",
                       key_fields=None,
                       artifact_refs=None,
                       **_: Any) -> str:
    """Send an inter-agent message with an optional structured envelope.

    Envelope-shaped delivery (P0-A) is the preferred path — pass
    `summary` + `key_fields` + `artifact_refs` instead of dumping a
    long `content`. The recipient's inbox injection then renders the
    envelope compactly (typically 200-400 tokens), saving the bulk of
    the raw detail for on-demand read_file() access.

    Backward compat: callers that still pass only `content` work
    unchanged — `summary` gets auto-derived from the first 800 chars.

    ``thread_id``/``reply_to``/``priority``/``ttl_s`` stay the same as before.
    """
    try:
        hub = _get_hub()
        # Resolve agent by name if not an ID.
        target = hub.get_agent(to_agent)
        if target is None:
            # Try finding by name (case-insensitive).
            for a in hub.agents.values():
                if a.name.lower() == to_agent.lower():
                    target = a
                    break
        if target is None:
            available = [f"{a.name} ({a.id})" for a in hub.agents.values()]
            return (
                f"Error: Agent '{to_agent}' not found.\n"
                f"Available agents: {', '.join(available) or 'none'}"
            )

        # Build envelope — auto-derives summary from content if missing.
        envelope = _build_handoff_envelope(
            content=content,
            summary=summary,
            key_fields=key_fields,
            artifact_refs=artifact_refs,
        )
        # Wire-safe text form (legacy content). Compact if envelope is
        # structured; falls back to raw `content` if no structured fields.
        wire_text = _render_envelope_for_wire(content, envelope)

        # Use hub's canonical routing entry point (audited).
        caller_id = _.get("_caller_agent_id", "unknown") if isinstance(_, dict) else "unknown"
        route = getattr(hub, "route_message", None)
        if callable(route):
            route(caller_id, target.id, wire_text, msg_type=msg_type,
                  source="tool_send_message")
        else:
            hub.send_message(caller_id, target.id, wire_text, msg_type=msg_type)

        # ── Durable inbox persistence (additive) ──
        # Never let a persistence error break the primary delivery path.
        inbox_msg_id = ""
        try:
            from ..inbox import get_store as _get_inbox_store
            _store = _get_inbox_store()
            inbox_msg_id = _store.send(
                to_agent=target.id,
                from_agent=caller_id or "system",
                content=wire_text,
                thread_id=thread_id or "",
                reply_to=reply_to or "",
                priority=priority or "normal",
                ttl_s=int(ttl_s or 0),
                metadata={
                    "msg_type": msg_type,
                    "source": "tool_send_message",
                    # Structured envelope, read by _build_inbox_context
                    # to render the recipient's injection compactly.
                    "envelope": envelope,
                    "detail_full": content if content else "",
                },
            )
        except Exception as _ibx_err:
            logger.debug("inbox persistence skipped: %s", _ibx_err)

        _tail = f"\n  Inbox id: {inbox_msg_id}" if inbox_msg_id else ""
        env_tail = ""
        if envelope.get("key_fields") or envelope.get("artifact_refs"):
            env_tail = (
                f"\n  Envelope: summary={len(envelope['summary'])}c "
                f"keys={len(envelope['key_fields'])} "
                f"artifacts={len(envelope['artifact_refs'])}"
            )
        return (
            f"Message sent to {target.name} ({target.id}).\n"
            f"  Type: {msg_type}\n"
            f"  Preview: {wire_text[:_SEND_MESSAGE_PREVIEW_CHARS]}"
            f"{env_tail}"
            f"{_tail}"
        )
    except Exception as e:
        return f"Error sending message: {e}"


# ── check_inbox / ack_message / reply_message ─────────────────────────

def _tool_check_inbox(limit: int = 20, include_read: bool = False,
                      **_: Any) -> str:
    """Return a compact view of the calling agent's inbox.

    By default only unread (state="new") messages are listed. Pass
    ``include_read=True`` to also see recently-read-but-not-acked ones.
    Does NOT modify state.
    """
    try:
        caller_id = _.get("_caller_agent_id", "") if isinstance(_, dict) else ""
        if not caller_id:
            return "Error: check_inbox requires a calling agent context."

        from ..inbox import get_store
        store = get_store()
        lim = max(1, min(int(limit or 20), 100))
        msgs = store.fetch_unread(caller_id, limit=lim)

        extra_read: list = []
        if include_read and len(msgs) < lim:
            # Pull recent read-but-not-acked by peeking at the raw store.
            try:
                with store._lock:  # internal; acceptable for read-only peek
                    cur = store._conn.execute(
                        "SELECT id, from_agent, content, priority, "
                        "created_at, thread_id "
                        "FROM inbox_messages "
                        "WHERE to_agent=? AND state='read' "
                        "ORDER BY created_at DESC LIMIT ?",
                        (caller_id, lim - len(msgs)),
                    )
                    extra_read = list(cur.fetchall())
            except Exception as _q_err:
                logger.debug("include_read peek failed: %s", _q_err)
                extra_read = []

        if not msgs and not extra_read:
            return "Inbox is empty (no new messages)."

        from datetime import datetime as _dt
        lines = [f"Inbox for {caller_id}:"]
        lines.append(f"  unread: {len(msgs)}    "
                     f"read (shown): {len(extra_read)}")
        lines.append("")
        for i, m in enumerate(msgs, 1):
            ts = _dt.fromtimestamp(m.created_at).strftime("%m-%d %H:%M")
            preview = (m.content or "").strip().replace("\n", " ")
            if len(preview) > 200:
                preview = preview[:200] + "…"
            lines.append(
                f"  [NEW {i}] id={m.id}  from={m.from_agent}  "
                f"prio={m.priority}  at={ts}"
            )
            if m.thread_id and m.thread_id != m.id:
                lines.append(f"         thread={m.thread_id}")
            lines.append(f"         {preview}")
        for row in extra_read:
            ts = _dt.fromtimestamp(row["created_at"]).strftime("%m-%d %H:%M")
            preview = (row["content"] or "").strip().replace("\n", " ")
            if len(preview) > 200:
                preview = preview[:200] + "…"
            lines.append(
                f"  [read ] id={row['id']}  from={row['from_agent']}  "
                f"prio={row['priority']}  at={ts}"
            )
            lines.append(f"         {preview}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error reading inbox: {e}"


def _tool_ack_message(message_ids: str = "", **_: Any) -> str:
    """Mark one or more inbox messages as acknowledged (state='acked').

    ``message_ids`` may be a single id or a comma / whitespace separated
    list. Only messages addressed to the calling agent are affected.
    Acked messages will not be re-injected at the next chat turn.
    """
    try:
        caller_id = _.get("_caller_agent_id", "") if isinstance(_, dict) else ""
        if not caller_id:
            return "Error: ack_message requires a calling agent context."
        raw = (message_ids or "").strip()
        if not raw:
            return "Error: message_ids is required."
        ids = [p.strip() for p in raw.replace(",", " ").split() if p.strip()]
        if not ids:
            return "Error: no valid message ids parsed."

        from ..inbox import get_store
        n = get_store().mark_acked(ids, caller_id)
        skipped = len(ids) - n
        if n == 0:
            return (f"No messages acked (0/{len(ids)}). IDs may not exist or "
                    f"not belong to {caller_id}.")
        tail = f" (skipped: {skipped})" if skipped else ""
        return f"Acked {n}/{len(ids)} message(s){tail}."
    except Exception as e:
        return f"Error acking message(s): {e}"


def _tool_reply_message(message_id: str, content: str = "",
                        priority: str = "normal", ttl_s: int = 0,
                        summary: str = "",
                        key_fields=None,
                        artifact_refs=None,
                        **_: Any) -> str:
    """Reply to an inbox message — preserves the thread and reply_to chain.

    Same envelope pattern as send_message (P0-A): pass `summary` +
    `key_fields` + `artifact_refs` for a token-lean delivery; plain
    `content` still accepted for back-compat (summary auto-derived).
    """
    try:
        caller_id = _.get("_caller_agent_id", "") if isinstance(_, dict) else ""
        if not caller_id:
            return "Error: reply_message requires a calling agent context."
        if not message_id:
            return "Error: message_id is required."
        if not content and not summary:
            return "Error: provide either content or summary for the reply."

        from ..inbox import get_store
        store = get_store()
        orig = store.get_by_id(message_id)
        if orig is None:
            return f"Error: message '{message_id}' not found in inbox."
        if orig.to_agent != caller_id:
            return (f"Error: cannot reply — message '{message_id}' was "
                    f"addressed to '{orig.to_agent}', not '{caller_id}'.")

        envelope = _build_handoff_envelope(
            content=content, summary=summary,
            key_fields=key_fields, artifact_refs=artifact_refs,
        )
        wire_text = _render_envelope_for_wire(content, envelope)

        target = orig.from_agent
        thread = orig.thread_id or orig.id
        new_id = store.send(
            to_agent=target,
            from_agent=caller_id,
            content=wire_text,
            thread_id=thread,
            reply_to=message_id,
            priority=priority or "normal",
            ttl_s=int(ttl_s or 0),
            metadata={
                "msg_type": "reply",
                "source": "tool_reply_message",
                "in_reply_to": message_id,
                "envelope": envelope,
                "detail_full": content if content else "",
            },
        )

        # Best-effort: mirror the reply into the legacy hub channel so
        # live routing / audit still fire (matches send_message's dual path).
        try:
            hub = _get_hub()
            route = getattr(hub, "route_message", None)
            if callable(route):
                route(caller_id, target, wire_text, msg_type="reply",
                      source="tool_reply_message")
        except Exception as _he:
            logger.debug("reply hub-mirror skipped: %s", _he)

        return (
            f"Reply sent to {target} on thread {thread}.\n"
            f"  In reply to: {message_id}\n"
            f"  New inbox id: {new_id}\n"
            f"  Preview: {wire_text[:_SEND_MESSAGE_PREVIEW_CHARS]}"
        )
    except Exception as e:
        return f"Error replying: {e}"


# ── task_update ──────────────────────────────────────────────────────

def _parse_run_at(run_at: str) -> float:
    """Parse run_at spec into a unix timestamp.

    Supported formats:
      '+5m'   → 5 minutes from now
      '+2h'   → 2 hours from now
      '18:30' → today at 18:30 (or tomorrow if already past)
    Returns 0.0 on failure.
    """
    run_at = run_at.strip()
    if not run_at:
        return 0.0
    # Relative: +Nm / +Nh / +Ns
    m = re.match(r'^\+(\d+)\s*([mMhHsS])$', run_at)
    if m:
        val = int(m.group(1))
        unit = m.group(2).lower()
        delta = {'m': timedelta(minutes=val), 'h': timedelta(hours=val),
                 's': timedelta(seconds=val)}[unit]
        return (datetime.now() + delta).timestamp()
    # Absolute: HH:MM
    m = re.match(r'^(\d{1,2}):(\d{2})$', run_at)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        now = datetime.now()
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return target.timestamp()
    return 0.0


def _tool_task_update(action: str, task_id: str = "", title: str = "",
                      description: str = "", status: str = "",
                      result: str = "",
                      recurrence: str = "once",
                      recurrence_spec: str = "",
                      run_at: str = "", **_: Any) -> str:
    """Manage the shared task list and register with scheduler for execution."""
    try:
        hub = _get_hub()
        # Use the calling agent's task list.
        caller_id = _.get("_caller_agent_id", "") if isinstance(_, dict) else ""
        agent = hub.get_agent(caller_id) if caller_id else None

        if action == "list":
            all_tasks = []
            for a in hub.agents.values():
                for t in a.tasks:
                    all_tasks.append(
                        f"  [{t.status.value:>11}] {t.id}: {t.title} "
                        f"(agent: {a.name})"
                    )
            if not all_tasks:
                return "No tasks found."
            return (f"Shared task list ({len(all_tasks)} tasks):\n"
                    + "\n".join(all_tasks))

        if action == "create":
            if not title:
                return "Error: 'title' is required for create action."
            rec = (recurrence or "once").lower()
            agent_id = caller_id or (agent.id if agent else "")

            # ── Project-scope routing (2026-04-28) ──
            # Inside a project chat, an agent that calls `task_update` MUST
            # land the task on `project.tasks` (the ProjectTask list shown
            # in the right-side panel + driven by handle_task_assignment),
            # NOT on `agent.tasks` (per-agent personal todo).
            #
            # Without this, the LLM (which doesn't distinguish the two
            # buckets) creates per-agent tasks that are invisible to the
            # project, and the right panel stays empty even after every
            # member appears to call `task_update ✓`.
            try:
                from ..project_context import get_project_context
                _project_id = get_project_context()
            except Exception:
                _project_id = ""
            if _project_id:
                proj = hub.projects.get(_project_id) if hasattr(hub, "projects") else None
                if proj is not None:
                    try:
                        ptask = proj.add_task(
                            title=title,
                            description=description or "",
                            assigned_to=caller_id or "",
                            created_by=caller_id or "system",
                            priority=0,
                        )
                        try:
                            if hasattr(hub, "_save_projects"):
                                hub._save_projects()
                        except Exception:
                            pass
                        return (
                            f"ProjectTask created: {ptask.id} — {title} "
                            f"[project={proj.id}, assigned={caller_id or '-'}]"
                        )
                    except Exception as _pe:
                        logger.warning(
                            "Project-task routing failed for project %s: %s. "
                            "Falling through to agent task path.",
                            _project_id, _pe,
                        )

            # ── Meeting-scope routing ──
            # If this tool is being called from within a meeting reply loop,
            # route the task to the Standalone Task registry (tagged with
            # meeting_id) instead of creating a scheduled job. Keeps meeting
            # discussions' "action items" out of the scheduler while still
            # making them visible in 任务中心 and traceable to the meeting.
            try:
                from ..meeting_context import get_meeting_context
                _meeting_id = get_meeting_context()
            except Exception:
                _meeting_id = ""
            if _meeting_id and getattr(hub, "standalone_task_registry", None):
                try:
                    st = hub.standalone_task_registry.create(
                        title=title,
                        description=description or "",
                        assigned_to=agent_id,
                        created_by=agent_id,
                        priority="normal",
                        due_hint=run_at or recurrence_spec or "",
                        source_meeting_id=_meeting_id,
                        tags=["from_meeting"],
                    )
                    return (
                        f"Task created (standalone, from meeting): {st.id} — {title}"
                        f" [source_meeting_id={_meeting_id}]"
                    )
                except Exception as _st_err:
                    logger.warning(
                        "Standalone-task routing failed for meeting %s: %s. "
                        "Falling through to default scheduler path.",
                        _meeting_id, _st_err,
                    )

            # Route through agent.add_task so recurrence / next_run_at is computed.
            if agent:
                new_task = agent.add_task(
                    title=title,
                    description=description,
                    assigned_by=caller_id or "system",
                    source="agent_chat",
                    recurrence=rec,
                    recurrence_spec=recurrence_spec or "",
                )
            else:
                # Fallback: no agent context — create plain task.
                from ..agent import AgentTask, TaskStatus
                new_task = AgentTask(
                    title=title,
                    description=description,
                    status=TaskStatus(status) if status else TaskStatus.TODO,
                    assigned_by=caller_id or "system",
                    recurrence=rec,
                    recurrence_spec=recurrence_spec or "",
                )

            # ── Register with TaskScheduler for actual execution ──
            # This is the critical bridge: AgentTask → ScheduledJob.
            #
            # GUARD: When the agent is running INSIDE a scheduled task,
            # block it from creating new scheduled jobs. Otherwise
            # "please generate daily report" prompts cause the agent to
            # create duplicate recurring jobs on every execution.
            _in_scheduled = getattr(agent, '_scheduled_context', False) if agent else False
            if _in_scheduled and (rec != "once" or run_at):
                return (
                    f"Task created: {new_task.id} — {title}"
                    f" [NOTE: scheduler registration skipped — "
                    f"you are already running inside a scheduled job]"
                )

            scheduler_msg = ""
            try:
                from ..scheduler import get_scheduler, recurrence_to_cron
                scheduler = get_scheduler()

                if rec != "once":
                    # Recurring task → register as recurring scheduler job.
                    cron_expr = recurrence_to_cron(rec, recurrence_spec or "")
                    if cron_expr and scheduler and agent_id:
                        job = scheduler.add_job(
                            agent_id=agent_id,
                            name=title,
                            prompt_template=description or title,
                            job_type="recurring",
                            cron_expr=cron_expr,
                        )
                        nxt = datetime.fromtimestamp(
                            job.next_run_at).strftime("%Y-%m-%d %H:%M")
                        scheduler_msg = (
                            f" [SCHEDULED: recurring {rec} @ "
                            f"{recurrence_spec or 'default'}, "
                            f"next run: {nxt}, job_id: {job.id}]")

                elif run_at:
                    # One-time delayed task → register as one_time scheduler job.
                    run_ts = _parse_run_at(run_at)
                    if run_ts > 0 and scheduler and agent_id:
                        job = scheduler.add_job(
                            agent_id=agent_id,
                            name=title,
                            prompt_template=description or title,
                            job_type="one_time",
                            cron_expr="* * * * *",  # placeholder
                            next_run_at=run_ts,
                        )
                        nxt = datetime.fromtimestamp(
                            run_ts).strftime("%Y-%m-%d %H:%M")
                        scheduler_msg = (
                            f" [SCHEDULED: one-time at {nxt}, "
                            f"job_id: {job.id}]")
                    elif run_ts <= 0:
                        scheduler_msg = (
                            f" [WARNING: could not parse run_at='{run_at}', "
                            f"task created but NOT scheduled]")

            except Exception as sched_err:
                logger.warning("Failed to register task with scheduler: %s",
                               sched_err)
                scheduler_msg = f" [scheduler registration failed: {sched_err}]"

            return f"Task created: {new_task.id} — {title}{scheduler_msg}"

        if action in ("update", "complete"):
            if not task_id:
                return "Error: 'task_id' is required for update/complete."
            from ..agent import TaskStatus
            # Find the task across all agents.
            for a in hub.agents.values():
                for t in a.tasks:
                    if t.id == task_id:
                        if action == "complete":
                            t.status = TaskStatus.DONE
                            t.result = result or "Completed"
                        elif status:
                            t.status = TaskStatus(status)
                        if description:
                            t.description = description
                        t.updated_at = time.time()
                        return f"Task {task_id} updated: status={t.status.value}"
            return f"Error: Task '{task_id}' not found."

        return f"Error: Unknown action '{action}'. Use: create | update | complete | list"
    except Exception as e:
        return f"Error managing tasks: {e}"


# ============================================================
# Day 5 (2026-05-05) — Structured handoff: dispatch_task / accept_task
# ============================================================
# Replaces "PM writes 任务派发_X.md, Coder reads markdown" pattern with
# typed TaskAssignment objects in SQLite + recipient inbox. See
# app/core/task_assignment.py for the data model.

def _tool_dispatch_task(
    to_agent: str = "",
    brief: str = "",
    context_refs: Any = None,
    deliverables: Any = None,
    project_id: str = "",
    project_task_id: str = "",
    priority: int = 0,
    deadline: str = "",
    **kwargs: Any,
) -> str:
    """PM-side tool: hand a structured task to another agent.

    Returns the task_assignment id. The receiving agent will see this
    task in its inbox the next time it calls accept_task — no markdown
    parsing required.
    """
    from ..core.task_assignment import (
        TaskAssignment, FileRef, Deliverable, get_store,
    )
    if not to_agent or not brief:
        return ("Error: dispatch_task requires `to_agent` and `brief`. "
                "Brief should be 1-3 sentences (≤ 500 chars).")
    if len(brief) > 500:
        return (f"Error: brief is too long ({len(brief)} chars). "
                f"Max 500 — be concise. Put detail in deliverables.")

    # ── PEP: before_dispatch_task ──
    # Rule Engine hook for PM dispatch — rules can deny based on
    # workload (assignee already has N in-flight), role mismatch
    # (e.g. "tester can't be assigned coder tasks"), or content
    # filters on brief. Failures isolated.
    deny_msg = _rule_engine_check_dispatch_task(
        from_agent_id=str(kwargs.get("_caller_agent_id") or ""),
        to_agent_id=to_agent,
        brief=brief,
        priority=priority,
        deadline=deadline,
        project_id=project_id or str(kwargs.get("_project_id") or ""),
    )
    if deny_msg:
        return deny_msg
    # Coerce JSON-string args (LLMs sometimes pass arrays as strings)
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
    refs_raw = _coerce_list(context_refs)
    delivs_raw = _coerce_list(deliverables)

    refs = []
    for r in refs_raw:
        if isinstance(r, str):
            refs.append(FileRef(path=r))
        elif isinstance(r, dict):
            refs.append(FileRef.from_dict(r))
    delivs = []
    for d in delivs_raw:
        if isinstance(d, dict):
            delivs.append(Deliverable.from_dict(d))
    if not delivs:
        return ("Error: dispatch_task requires at least one deliverable. "
                "Format: [{\"path\": \"src/foo.py\", \"must_contain\": "
                "[\"def main\"], \"min_lines\": 20}]. Without a contract "
                "the receiver can't verify completion.")

    deadline_ts = 0.0
    if deadline:
        try:
            deadline_ts = float(deadline)
        except (TypeError, ValueError):
            try:
                import datetime as _dt
                deadline_ts = _dt.datetime.fromisoformat(deadline).timestamp()
            except Exception:
                deadline_ts = 0.0

    from_agent = kwargs.get("_caller_agent_id", "") or ""
    if not project_id:
        # Best-effort: pick from current scope
        try:
            from ..tools import _get_current_scope
            project_id = _get_current_scope().get("project_id", "") or ""
        except Exception:
            project_id = ""

    ta = TaskAssignment(
        from_agent=from_agent, to_agent=to_agent,
        brief=brief.strip(),
        context_refs=refs, deliverables=delivs,
        project_id=project_id, project_task_id=project_task_id,
        priority=int(priority or 0), deadline=deadline_ts,
    )
    try:
        get_store().insert(ta)
    except Exception as e:
        return f"Error persisting task assignment: {e}"

    return (
        f"✅ Dispatched task {ta.id} to agent {to_agent}.\n"
        f"  Brief: {brief[:120]}\n"
        f"  Deliverables: {len(delivs)} ({', '.join(d.path for d in delivs)})\n"
        f"  Context refs: {len(refs)}\n"
        f"The receiving agent will see this in its inbox. You may "
        f"continue with other work or wait for status."
    )


def _tool_accept_task(
    ta_id: str = "",
    **kwargs: Any,
) -> str:
    """Receiver-side tool: pop a task assignment from this agent's inbox
    and render it as a structured brief.

    If ``ta_id`` is empty, returns the highest-priority pending task.
    """
    from ..core.task_assignment import get_store
    caller_id = kwargs.get("_caller_agent_id", "") or ""
    if not caller_id:
        return ("Error: accept_task requires caller agent context "
                "(_caller_agent_id). The framework injects this "
                "automatically — likely a dispatcher bug.")
    store = get_store()

    if ta_id:
        ta = store.get(ta_id)
        if ta is None or ta.to_agent != caller_id:
            return f"Error: task {ta_id} not found or not assigned to you."
    else:
        # Pop top-priority pending
        inbox = store.list_inbox(caller_id, status="pending", limit=1)
        if not inbox:
            return ("(no pending task assignments in your inbox; "
                    "you can ask the user / PM what to do next)")
        ta = inbox[0]

    if ta.status != "pending":
        return f"Task {ta.id} is already {ta.status}."
    store.update_status(ta.id, "accepted")
    return ta.render_for_recipient()


def _tool_inbox_assignments(**kwargs: Any) -> str:
    """List structured task assignments waiting in this agent's inbox.

    Different from check_inbox (which lists chat messages). Use this
    when looking for work-to-do.
    """
    from ..core.task_assignment import get_store
    caller_id = kwargs.get("_caller_agent_id", "") or ""
    if not caller_id:
        return "Error: requires caller agent context."
    inbox = get_store().list_inbox(caller_id, status="pending", limit=20)
    if not inbox:
        return "(inbox empty — no pending task assignments)"
    lines = [f"## Pending task assignments ({len(inbox)})"]
    for ta in inbox:
        prio = "🔥" if ta.priority >= 2 else ("⭐" if ta.priority == 1 else "•")
        lines.append(
            f"  {prio} {ta.id} from {ta.from_agent}: {ta.brief[:80]}"
            f"  ({len(ta.deliverables)} deliverables)"
        )
    lines.append("")
    lines.append("Call `accept_task(ta_id=\"<id>\")` to take one.")
    return "\n".join(lines)


# ============================================================
# Phase 3 P3-4 (2026-05-06): report_back — close the dispatch loop
# ============================================================
# After Coder finishes a TaskAssignment, it calls report_back with the
# outcome ("done" / "blocked" / "needs_clarification" + summary). The
# tool:
#   1. Marks the TaskAssignment as done/cancelled in SQLite
#   2. Sends a structured chat message to the original PM (from_agent)
#      so PM sees the result in their inbox without needing to poll
#   3. Optionally lists actual deliverable paths produced

def _tool_report_back(
    ta_id: str = "",
    status: str = "done",
    summary: str = "",
    actual_deliverables: Any = None,
    blocker: str = "",
    **kwargs: Any,
) -> str:
    """Coder-side: report task completion (or blocker) back to the PM.

    status ∈ done / blocked / needs_clarification / cancelled
    """
    from ..core.task_assignment import get_store
    from .. import auth as _auth_mod
    caller_id = kwargs.get("_caller_agent_id", "") or ""
    if not caller_id:
        return "Error: requires caller agent context."
    if not ta_id:
        # Auto-pick most recent accepted task for this caller
        try:
            store = get_store()
            rows = store._conn.execute(
                "SELECT id FROM task_assignments WHERE to_agent=? AND status='accepted' "
                "ORDER BY accepted_at DESC LIMIT 1",
                (caller_id,),
            ).fetchall()
            if not rows:
                return ("Error: no accepted task found for you. Pass ta_id "
                        "explicitly, or accept_task first.")
            ta_id = rows[0]["id"]
        except Exception as e:
            return f"Error finding accepted task: {e}"

    store = get_store()
    ta = store.get(ta_id)
    if ta is None:
        return f"Error: task {ta_id} not found."
    if ta.to_agent != caller_id:
        return f"Error: task {ta_id} is not assigned to you."

    valid_status = {"done", "blocked", "needs_clarification", "cancelled"}
    if status not in valid_status:
        return f"Error: status must be one of {sorted(valid_status)}, got {status!r}."

    # Coerce deliverables list
    actual_paths: list[str] = []
    if actual_deliverables is not None:
        if isinstance(actual_deliverables, list):
            actual_paths = [str(p) for p in actual_deliverables]
        elif isinstance(actual_deliverables, str):
            try:
                import json as _j
                d = _j.loads(actual_deliverables)
                if isinstance(d, list):
                    actual_paths = [str(p) for p in d]
            except Exception:
                actual_paths = [actual_deliverables]

    # Update store status: done/cancelled close the row; blocked / needs_*
    # leave it accepted so the PM can re-dispatch with clarification.
    if status in ("done", "cancelled"):
        store.update_status(ta_id, "done" if status == "done" else "cancelled")

    # Compose a chat message to the PM (from_agent) via the standard inbox
    icon = {"done": "✅", "blocked": "⛔",
            "needs_clarification": "❓", "cancelled": "🚫"}.get(status, "📨")
    msg_lines = [f"{icon} Report back on task {ta_id} (status={status})"]
    if summary:
        msg_lines.append("")
        msg_lines.append(summary)
    if actual_paths:
        msg_lines.append("")
        msg_lines.append("Deliverables produced:")
        for p in actual_paths:
            msg_lines.append(f"  • {p}")
    if blocker:
        msg_lines.append("")
        msg_lines.append(f"Blocker: {blocker}")
    chat_msg = "\n".join(msg_lines)

    # Best-effort send to PM via existing send_message infrastructure
    sent_ok = False
    if ta.from_agent and ta.from_agent != caller_id:
        try:
            send_result = _tool_send_message(
                to_agent=ta.from_agent,
                content=chat_msg,
                priority=("urgent" if status == "blocked" else "normal"),
                _caller_agent_id=caller_id,
            )
            sent_ok = (isinstance(send_result, str)
                       and not send_result.lower().startswith("error"))
        except Exception:
            sent_ok = False

    # Audit
    try:
        _auth_mod.get_auth().audit(
            "task_report_back", actor=caller_id, target=ta.from_agent,
            detail=f"ta={ta_id} status={status}",
            agent_id=caller_id,
            project_id=ta.project_id,
        )
    except Exception:
        pass

    # Phase 3 (2026-05-06): blocked / needs_clarification → auto-issue
    # so PM has a tracked item to follow up on (not just a chat msg).
    auto_issue_id = ""
    if status in ("blocked", "needs_clarification") and ta.project_id:
        try:
            from ..tools_split.project import _auto_report_issue
            from ..hub import get_hub
            hub = get_hub()
            project = hub.projects.get(ta.project_id) if hub else None
            if project is not None:
                sev = "high" if status == "blocked" else "medium"
                desc_parts = []
                if summary:
                    desc_parts.append(summary)
                if blocker:
                    desc_parts.append(f"Blocker: {blocker}")
                desc_parts.append(f"From task assignment: {ta_id}")
                auto_issue_id = _auto_report_issue(
                    project,
                    title=f"{status}: {ta.brief[:120]}",
                    description="\n".join(desc_parts),
                    severity=sev,
                    related_task_id=ta.project_task_id,
                    reporter=caller_id,
                    source=f"report_back:{status}",
                )
        except Exception as e:
            logger.debug("report_back auto-issue skipped: %s", e)

    return (
        f"Reported back on {ta_id} as {status}."
        + (f" PM ({ta.from_agent}) notified via inbox." if sent_ok else
           " (PM notification skipped — could not deliver chat message.)")
        + (f" Issue {auto_issue_id} auto-created for tracking."
           if auto_issue_id and not auto_issue_id.startswith("(") else "")
    )


# ============================================================
# Phase 2 P2-6 (2026-05-06) — Team status query tools
# ============================================================

def _tool_query_team_status(project_id: str = "", **kwargs: Any) -> str:
    """List the current activity of every agent in the given project.

    Use this BEFORE dispatching a task to see who's idle vs busy. Reads
    from the team_dashboard SQLite table — no agent introspection /
    LLM cost.
    """
    from ..core.team_dashboard import get_dashboard
    if not project_id:
        try:
            from ..tools import _get_current_scope
            project_id = _get_current_scope().get("project_id", "") or ""
        except Exception:
            project_id = ""
    if not project_id:
        return ("Error: query_team_status requires project_id (or call from "
                "within a project context).")
    rows = get_dashboard().query_project(project_id)
    if not rows:
        return f"(no agent activity recorded for project {project_id[:8]})"
    import time as _time
    now = _time.time()
    lines = [f"## Team status for project {project_id[:8]} ({len(rows)} agents)"]
    for r in rows:
        since = int(now - float(r.get("last_update_at", now)))
        cur = r.get("current_action") or "(idle)"
        nxt = r.get("next_action") or ""
        blocked = r.get("blocked_by") or ""
        marker = "🟡" if blocked else ("🟢" if since < 30 else "⚪")
        line = (f"  {marker} {r['agent_id'][:10]}  task={r.get('task_id','')[:8] or '-'}"
                f"  cur={cur[:40]!r}  age={since}s")
        if nxt:
            line += f"  next={nxt[:40]!r}"
        if blocked:
            line += f"  ⚠ blocked_by={blocked[:30]!r}"
        lines.append(line)
    return "\n".join(lines)


def _tool_query_agent_status(agent_id: str = "", project_id: str = "",
                             **kwargs: Any) -> str:
    """Get one agent's current action — what task they're on and what
    they last did. Useful when the PM agent wants to check up on a
    specific worker before re-dispatching."""
    from ..core.team_dashboard import get_dashboard
    if not agent_id:
        return "Error: agent_id required."
    row = get_dashboard().query_agent(agent_id, project_id=project_id)
    if not row:
        return f"(no recorded activity for agent {agent_id[:10]})"
    import time as _time
    age = int(_time.time() - float(row.get("last_update_at", _time.time())))
    lines = [f"## Agent {agent_id[:10]}"]
    lines.append(f"  project: {row.get('project_id','')[:10]}  task: {row.get('task_id','')[:10]}")
    lines.append(f"  current: {row.get('current_action','(idle)')!r}")
    if row.get("next_action"):
        lines.append(f"  next:    {row['next_action']!r}")
    if row.get("blocked_by"):
        lines.append(f"  ⚠ blocked: {row['blocked_by']!r}")
    lines.append(f"  age:     {age}s since last update")
    if row.get("deliverable_progress"):
        lines.append(f"  deliverables: {row['deliverable_progress']}")
    return "\n".join(lines)
