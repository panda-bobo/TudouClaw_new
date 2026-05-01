"""Meeting management router — list, CRUD, meeting management, file ops."""
from __future__ import annotations

import logging
import os
import shutil

from fastapi import APIRouter, Depends, HTTPException, Query, Body, UploadFile, File
from fastapi.responses import FileResponse

from ..deps.hub import get_hub
from ..deps.auth import CurrentUser, get_current_user

logger = logging.getLogger("tudouclaw.api.meetings")

router = APIRouter(prefix="/api/portal", tags=["meetings"])


# ---------------------------------------------------------------------------
# Meeting listing — matches legacy portal_routes_get
# ---------------------------------------------------------------------------

@router.get("/meetings")
async def list_meetings(
    project_id: str = Query("", description="Filter by project"),
    status: str = Query("", description="Filter by status"),
    participant: str = Query("", description="Filter by participant agent ID"),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List all meetings."""
    try:
        reg = getattr(hub, "meeting_registry", None)
        if reg is None:
            return {"meetings": []}
        items = reg.list(
            project_id=project_id or None,
            status=status or None,
            participant=participant or None,
        )
        return {"meetings": [m.to_summary_dict() for m in items]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Single meeting
# ---------------------------------------------------------------------------

@router.get("/meetings/{meeting_id}")
async def get_meeting(
    meeting_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get meeting detail."""
    try:
        reg = getattr(hub, "meeting_registry", None)
        if reg is None:
            raise HTTPException(503, "meeting registry not initialized")
        m = reg.get(meeting_id)
        if not m:
            raise HTTPException(404, "Meeting not found")
        data = m.to_dict()
        # Enrich messages with file refs so the frontend can render
        # clickable FileCards for any file/URL an agent mentioned —
        # same behavior as the legacy stdlib portal route, so meeting
        # chat has parity with agent chat's artifact display.
        try:
            from ...server.portal_routes_get import (
                _enrich_meeting_messages_with_refs,
            )
            _enrich_meeting_messages_with_refs(hub, data.get("messages") or [])
        except Exception as _e:
            logger.debug("meeting ref enrichment failed: %s", _e)
        # Expose currently-busy participants as "active_speakers". This
        # lets the frontend show typing bubbles for agents re-queued
        # mid-chain via @-mention (e.g. 小安 @小土 in its reply body —
        # the moderator never told the UI to bubble for 小土, but the
        # agent IS processing). Without this the UI goes silent after
        # the last visible reply even though another agent is actively
        # working on a response.
        try:
            active: list[str] = []
            for pid in (m.participants or []):
                try:
                    ag = hub.get_agent(pid) if hasattr(hub, "get_agent") else None
                except Exception:
                    ag = None
                if ag is None:
                    continue
                status_val = getattr(ag, "status", None)
                # AgentStatus is a str-enum; compare via .value to avoid
                # importing the class just for this.
                sv = getattr(status_val, "value", status_val)
                if sv in ("busy", "waiting_approval"):
                    active.append(pid)
            data["active_speakers"] = active
        except Exception as _e:
            logger.debug("active_speakers scan failed: %s", _e)
            data["active_speakers"] = []
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Meeting messages
# ---------------------------------------------------------------------------

@router.delete("/meetings/{meeting_id}/messages")
async def clear_meeting_messages(
    meeting_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Clear the meeting's chat — drops:

      1. ``meeting.messages``  — what the UI displays.
      2. For every participant agent: their ``meeting:{meeting_id}``
         bucket in ``_messages_by_context`` — so the next LLM turn
         won't replay this meeting's old messages either.

    Solo / project / other-meeting buckets on each agent are NOT
    touched. Participants, files, assignments are NOT touched.
    """
    try:
        reg = getattr(hub, "meeting_registry", None)
        if reg is None:
            raise HTTPException(503, "meeting registry not initialized")
        m = reg.get(meeting_id)
        if not m:
            raise HTTPException(404, "Meeting not found")
        ui_cleared = len(m.messages or [])
        m.messages = []
        ctx_key = f"meeting:{m.id}"
        agents_cleared = 0
        agent_msgs_cleared = 0
        # `participants` is a list of agent ids (or dicts in some legacy
        # paths). Normalize to ids.
        for p in (getattr(m, "participants", []) or []):
            aid = p if isinstance(p, str) else (
                getattr(p, "agent_id", "") or
                (p.get("agent_id", "") if isinstance(p, dict) else "")
            )
            if not aid:
                continue
            agent = hub.agents.get(aid) if hasattr(hub, "agents") else None
            if agent is None:
                continue
            mbc = getattr(agent, "_messages_by_context", None)
            if not isinstance(mbc, dict) or ctx_key not in mbc:
                continue
            agent_msgs_cleared += len(mbc.get(ctx_key) or [])
            del mbc[ctx_key]
            if getattr(agent, "_active_context_id", "") == ctx_key:
                agent._switch_context("solo")
            agents_cleared += 1
        try:
            reg.save()
            if hasattr(hub, "_save_agents"):
                hub._save_agents()
        except Exception as _se:
            logger.debug("clear_meeting_messages: save failed: %s", _se)
        return {
            "ok": True,
            "ui_messages_cleared": ui_cleared,
            "agents_cleared": agents_cleared,
            "agent_messages_cleared": agent_msgs_cleared,
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("clear_meeting_messages failed")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/meetings/{meeting_id}/messages")
async def get_meeting_messages(
    meeting_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get meeting messages."""
    try:
        reg = getattr(hub, "meeting_registry", None)
        if reg is None:
            raise HTTPException(503, "meeting registry not initialized")
        m = reg.get(meeting_id)
        if not m:
            raise HTTPException(404, "Meeting not found")
        msg_dicts = [x.to_dict() for x in m.messages]
        # Same refs enrichment as GET /meetings/{id}. Best-effort —
        # clients still get the raw message list if the enricher fails.
        try:
            from ...server.portal_routes_get import (
                _enrich_meeting_messages_with_refs,
            )
            _enrich_meeting_messages_with_refs(hub, msg_dicts)
        except Exception as _e:
            logger.debug("meeting ref enrichment failed: %s", _e)
        return {"messages": msg_dicts}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Meeting assignments
# ---------------------------------------------------------------------------

@router.get("/meetings/{meeting_id}/assignments")
async def get_meeting_assignments(
    meeting_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get meeting assignments."""
    try:
        reg = getattr(hub, "meeting_registry", None)
        if reg is None:
            raise HTTPException(503, "meeting registry not initialized")
        m = reg.get(meeting_id)
        if not m:
            raise HTTPException(404, "Meeting not found")
        return {"assignments": [a.to_dict() for a in m.assignments]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Meeting CRUD
# ---------------------------------------------------------------------------

@router.post("/meetings")
async def manage_meetings(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Create a meeting."""
    try:
        reg = getattr(hub, "meeting_registry", None)
        if reg is None:
            raise HTTPException(503, "meeting registry not initialized")
        title = body.get("title", "")
        if not title:
            raise HTTPException(400, "title is required")
        # Resolve host: use the requesting user's name or first participant
        host = body.get("host", "")
        if not host:
            actor = getattr(user, "username", "") or getattr(user, "user_id", "user")
            host = actor
        meeting = reg.create(
            title=title,
            host=host,
            participants=body.get("participants", []),
            agenda=body.get("agenda", ""),
            project_id=body.get("project_id", ""),
        )
        return {"ok": True, "meeting": meeting.to_dict() if hasattr(meeting, "to_dict") else meeting}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Meeting deletion
# ---------------------------------------------------------------------------

@router.delete("/meetings/{meeting_id}")
async def delete_meeting(
    meeting_id: str,
    purge_workspace: bool = Query(
        True,
        description="Also rm -rf the meeting's workspace directory.",
    ),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Delete a meeting record and (optionally) its workspace directory.

    Meeting messages live inside the meeting document itself, so removing
    the meeting removes its full conversation. The workspace directory
    (``workspaces/meetings/<id>/``) is purged by default — pass
    ``purge_workspace=false`` to keep transcripts on disk.
    """
    try:
        reg = getattr(hub, "meeting_registry", None)
        if reg is None:
            raise HTTPException(503, "meeting registry not initialized")

        meeting = reg.get(meeting_id)
        if meeting is None:
            raise HTTPException(404, f"Meeting {meeting_id!r} not found")

        if not reg.delete(meeting_id):
            raise HTTPException(500, "meeting delete failed")

        purged_ws = False
        if purge_workspace:
            import os, shutil
            from app import DEFAULT_DATA_DIR
            data_dir = os.environ.get("TUDOU_CLAW_DATA_DIR") or DEFAULT_DATA_DIR
            wsp = os.path.join(data_dir, "workspaces", "meetings", meeting_id)
            if os.path.isdir(wsp):
                try:
                    shutil.rmtree(wsp, ignore_errors=False)
                    purged_ws = True
                except Exception as e:
                    logger.warning("meeting workspace rm failed: %s", e)

        return {
            "ok": True,
            "deleted": meeting_id,
            "purged_workspace": purged_ws,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Meeting management — sub-path routes matching JS client
# ---------------------------------------------------------------------------

def _get_meeting(hub, meeting_id: str):
    """Fetch meeting or raise 404/503."""
    reg = getattr(hub, "meeting_registry", None)
    if reg is None:
        raise HTTPException(503, "meeting registry not initialized")
    m = reg.get(meeting_id)
    if not m:
        raise HTTPException(404, "Meeting not found")
    return reg, m


@router.post("/meetings/{meeting_id}/start")
async def meeting_start(
    meeting_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Start a meeting."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        m.start()
        reg.save()
        return {"ok": True, "meeting": m.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/meetings/{meeting_id}/close")
async def meeting_close(
    meeting_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Close a meeting with optional summary."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        m.close(body.get("summary", ""))
        reg.save()
        return {"ok": True, "meeting": m.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/meetings/{meeting_id}/cancel")
async def meeting_cancel(
    meeting_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Cancel a meeting."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        m.cancel()
        reg.save()
        return {"ok": True, "meeting": m.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/meetings/{meeting_id}/participants")
async def meeting_add_participant(
    meeting_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Invite a new agent into the meeting."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        agent_id = (body.get("agent_id") or "").strip()
        if not agent_id:
            raise HTTPException(400, detail="agent_id required")
        added = m.add_participant(agent_id)
        if added:
            # Look up agent name for a nicer system message
            name = agent_id
            try:
                pce = getattr(hub, "project_chat_engine", None)
                if pce is not None:
                    ag = pce._lookup(agent_id)
                    if ag is not None:
                        name = getattr(ag, "name", "") or agent_id
            except Exception:
                pass
            try:
                m.add_message(
                    sender="system", sender_name="系统", role="system",
                    content=f"👥 {name} 加入了会议",
                )
            except Exception:
                pass
        reg.save()
        return {"ok": True, "added": added, "meeting": m.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.delete("/meetings/{meeting_id}/participants/{agent_id}")
async def meeting_remove_participant(
    meeting_id: str,
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Remove an agent from the meeting."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        removed = m.remove_participant(agent_id)
        if removed:
            # Cancel any in-flight reply sequence so the removed agent
            # doesn't still land a message after leaving.
            try:
                from ...meeting import bump_meeting_reply_gen
                bump_meeting_reply_gen(m.id)
            except Exception:
                pass
            name = agent_id
            try:
                pce = getattr(hub, "project_chat_engine", None)
                if pce is not None:
                    ag = pce._lookup(agent_id)
                    if ag is not None:
                        name = getattr(ag, "name", "") or agent_id
            except Exception:
                pass
            try:
                m.add_message(
                    sender="system", sender_name="系统", role="system",
                    content=f"👋 {name} 已被移出会议",
                )
            except Exception:
                pass
        reg.save()
        return {"ok": True, "removed": removed, "meeting": m.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/meetings/{meeting_id}/interrupt")
async def meeting_interrupt(
    meeting_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Stop any in-flight agent reply sequence for this meeting.

    Soft stop — waits for the agent loop to notice between turns.
    For hard stop (kill running bash subprocesses immediately) use /abort.
    """
    try:
        reg, m = _get_meeting(hub, meeting_id)
        from ...meeting import bump_meeting_reply_gen
        new_gen = bump_meeting_reply_gen(m.id)
        try:
            m.add_message(
                sender="system",
                sender_name="系统",
                role="system",
                content="⏸ 主持人已暂停 Agent 发言，等待下一指令",
            )
            reg.save()
        except Exception:
            pass
        return {"ok": True, "gen": new_gen}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/meetings/{meeting_id}/abort")
async def meeting_abort(
    meeting_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Hard abort: stop discussion loop AND SIGTERM any bash subprocesses
    spawned by agents in this meeting.

    Differs from /interrupt: this ALSO flips the central abort registry
    and kills tracked OS processes (e.g. a runaway `python build_report.py`
    launched by the pptx-author skill). Use this when agents are stuck
    in a long-running subprocess and cooperative interrupt isn't enough.
    """
    try:
        reg, m = _get_meeting(hub, meeting_id)
        from ...meeting import bump_meeting_reply_gen
        from ... import abort_registry as _ar
        from ... import checkpoint as _ckpt
        # 1. Cooperative path: bump gen so any in-flight loop exits cleanly.
        new_gen = bump_meeting_reply_gen(m.id)

        # 2. Checkpoint-then-abort: snapshot unfinished plans and the
        # recent transcript BEFORE we SIGTERM anything, so the user can
        # resume this meeting later with minimal context loss.
        def _snapshot_meeting():
            # Collect each running/unfinished assignment's plan, if any.
            assignments_snap = []
            try:
                for a in getattr(m, "assignments", []) or []:
                    if getattr(a, "status", "") in ("done", "cancelled"):
                        continue
                    assignments_snap.append({
                        "id": getattr(a, "id", ""),
                        "agent_id": getattr(a, "agent_id", ""),
                        "status": getattr(a, "status", ""),
                        "task_summary": getattr(a, "task_summary", "") or
                                        getattr(a, "prompt", "")[:200],
                        "verify": getattr(a, "verify", {}),
                    })
            except Exception:
                pass
            # Capture the last ~20 messages as chat_tail.
            tail = []
            try:
                msgs = list(getattr(m, "messages", []) or [])[-20:]
                for msg in msgs:
                    tail.append({
                        "role": getattr(msg, "role", "") or
                                (msg.get("role") if isinstance(msg, dict) else ""),
                        "content": getattr(msg, "content", "") or
                                   (msg.get("content") if isinstance(msg, dict) else ""),
                        "sender": getattr(msg, "sender", "") or
                                  (msg.get("sender") if isinstance(msg, dict) else ""),
                        "ts": getattr(msg, "timestamp", 0.0) or
                              (msg.get("timestamp") if isinstance(msg, dict) else 0.0),
                    })
            except Exception:
                pass
            return {
                "agent_id": f"meeting:{m.id}",
                "scope": _ckpt.SCOPE_MEETING,
                "scope_id": m.id,
                "plan_json": {
                    "task_summary": getattr(m, "title", "") or m.id,
                    "steps": assignments_snap,
                },
                "chat_tail": tail,
                "reason": _ckpt.REASON_USER_ABORT,
                "metadata": {
                    "meeting_title": getattr(m, "title", ""),
                    "participants": list(getattr(m, "participants", []) or []),
                    "unfinished_assignments": len(assignments_snap),
                },
            }

        result = _ar.abort_with_checkpoint(
            _ar.meeting_key(m.id),
            snapshot_fn=_snapshot_meeting,
        )
        ckpt_id = result.get("checkpoint_id", "")

        # 3. Visible note in transcript.
        try:
            killed_n = len(result.get("killed_pids") or [])
            note = "🛑 已强制终止本会议的 Agent 执行"
            if killed_n:
                note += f"（已停止 {killed_n} 个子进程）"
            if ckpt_id:
                note += f"\n📎 已保存检查点 {ckpt_id}，稍后可恢复未完成的工作。"
            m.add_message(
                sender="system", sender_name="系统", role="system",
                content=note,
            )
            reg.save()
        except Exception:
            pass
        return {"ok": True, "gen": new_gen,
                "abort": result, "checkpoint_id": ckpt_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/meetings/{meeting_id}/messages")
async def meeting_post_message(
    meeting_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Post a message to a meeting and trigger agent auto-replies."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        sender = body.get("sender", "")
        if not sender:
            sender = getattr(user, "username", "") or getattr(user, "user_id", "user")
        msg = m.add_message(
            sender=sender,
            content=body.get("content", ""),
            role=body.get("role", "user"),
            sender_name=body.get("sender_name", sender),
            attachments=body.get("attachments"),
        )
        reg.save()

        # Phase-2 wiring: auto-create MeetingAssignment when the user's
        # message contains @<agent> + a task-intent keyword ("请完成",
        # "调研", "生成报告", etc.). After the discussion round ends,
        # meeting_agent_reply will find these OPEN assignments and
        # hand each off to an execution-mode worker.
        try:
            if msg.role == "user":
                from ...meeting import _detect_task_assignment
                pce = getattr(hub, "project_chat_engine", None)
                lookup = pce._lookup if pce else (lambda _i: None)
                detected = _detect_task_assignment(
                    msg.content or "", m, lookup,
                )
                for d in detected:
                    try:
                        m.add_assignment(
                            title=d["title"],
                            assignee_agent_id=d["assignee_agent_id"],
                            description="来自会议消息的自动识别任务",
                        )
                        logger.info(
                            "meeting %s: auto-created assignment for %s",
                            m.id, d["assignee_agent_id"][:8],
                        )
                    except Exception as _ae:
                        logger.warning(
                            "meeting %s: add_assignment failed: %s",
                            m.id, _ae,
                        )
                if detected:
                    reg.save()
        except Exception as _det_err:
            logger.warning(
                "meeting %s: task detection failed: %s",
                m.id, _det_err,
            )

        # ── Agent auto-reply: when a user posts to an ACTIVE meeting,
        #    each participant agent replies in sequence (daemon thread).
        #    Stop-commands ("暂停"/"停止"/...) only interrupt in-flight
        #    replies — they do NOT trigger a new round. ──
        interrupted = False
        respondents: list[str] = []  # agent ids expected to reply (for UI typing bubbles)
        try:
            from ...meeting import (
                MeetingStatus, spawn_meeting_reply,
                is_stop_command, bump_meeting_reply_gen,
            )
            _status_val = m.status.value if hasattr(m.status, 'value') else str(m.status)
            if msg.role == "user" and _status_val == "active":
                if is_stop_command(msg.content or ""):
                    # User wants to halt — bump gen so running sequence aborts,
                    # but do not spawn a new reply round.
                    bump_meeting_reply_gen(m.id)
                    interrupted = True
                    logger.info("meeting %s: stop-command detected, aborting in-flight replies", m.id)
                else:
                    pce = getattr(hub, "project_chat_engine", None)
                    if pce is not None and m.participants:
                        logger.info("spawning meeting reply for %d participants", len(m.participants))
                        # target_agents semantics (explicit):
                        #   absent / null  -> None (all participants reply)
                        #   []             -> [] (nobody replies, user @-none)
                        #   [ids]          -> only those reply
                        _ta = body.get("target_agents", None)
                        if _ta is not None and not isinstance(_ta, list):
                            _ta = None
                        # Compute respondents list for frontend typing bubbles
                        _parts = list(m.participants or [])
                        if _ta is None:
                            respondents = _parts
                        elif isinstance(_ta, list):
                            _pset = set(_parts)
                            respondents = [x for x in _ta if x in _pset]
                        spawn_meeting_reply(
                            meeting=m,
                            registry=reg,
                            agent_chat_fn=pce._chat,
                            agent_lookup_fn=pce._lookup,
                            user_msg=msg.content,
                            target_agent_ids=_ta,
                        )
                    else:
                        logger.warning("meeting reply skipped: pce=%s, participants=%s", pce, m.participants)
        except Exception as _e:
            logger.warning("meeting agent reply spawn failed: %s", _e, exc_info=True)

        return {
            "ok": True,
            "message": msg.to_dict(),
            "interrupted": interrupted,
            "respondents": respondents,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/meetings/{meeting_id}/assignments")
async def meeting_create_assignment(
    meeting_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Create a task assignment within a meeting."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        title = body.get("title", "")
        if not title:
            raise HTTPException(400, "title is required")
        a = m.add_assignment(
            title=title,
            assignee_agent_id=body.get("assignee_agent_id", ""),
            description=body.get("description", ""),
            due_hint=body.get("due_hint", ""),
            project_id=body.get("project_id", ""),
        )
        reg.save()
        return {"ok": True, "assignment": a.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/meetings/{meeting_id}/assignments/{assignment_id}/update")
async def meeting_update_assignment(
    meeting_id: str,
    assignment_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update a meeting assignment status."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        for a in m.assignments:
            if a.id == assignment_id:
                new_status = body.get("status", "")
                if new_status:
                    a.status = new_status
                reg.save()
                return {"ok": True, "assignment": a.to_dict()}
        raise HTTPException(404, "Assignment not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/meetings/{meeting_id}/assignments/{assignment_id}/reexecute")
async def meeting_reexecute_assignment(
    meeting_id: str,
    assignment_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Re-run an assignment — skips already-done plan steps, reuses
    existing workspace artifacts, only redoes what's unfinished.

    Triggered by the "🔄 重新执行此任务" button. Works for any
    assignment status (OPEN / IN_PROGRESS / DONE / CANCELLED) — admin
    may decide a "DONE" was a false completion and want to retry.
    """
    try:
        reg, m = _get_meeting(hub, meeting_id)
        target = None
        for a in m.assignments:
            if a.id == assignment_id:
                target = a
                break
        if not target:
            raise HTTPException(404, "Assignment not found")
        if not target.assignee_agent_id:
            raise HTTPException(400, "Assignment has no assignee_agent_id")
        # Revert to IN_PROGRESS so the UI reflects the retry immediately
        from ...meeting import (
            AssignmentStatus, spawn_meeting_assignment_reexecute,
        )
        target.status = AssignmentStatus.IN_PROGRESS
        import time as _t
        target.updated_at = _t.time()
        reg.save()
        # Kick off reexecute in a daemon thread
        spawn_meeting_assignment_reexecute(
            meeting=m, registry=reg,
            agent_chat_fn=hub._direct_chat,
            agent_lookup_fn=hub.get_agent,
            assignment=target,
        )
        return {"ok": True, "assignment": target.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("reexecute failed")
        raise HTTPException(500, detail=str(e))


@router.post("/meetings/{meeting_id}/assignments/{assignment_id}/dispatch")
async def meeting_dispatch_assignment(
    meeting_id: str,
    assignment_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Materialize a meeting assignment into an AgentTask on the assignee.

    - Creates an AgentTask with source_meeting_id / source_assignment_id
    - Adds the meeting workspace to agent's authorized_workspaces
    - Updates assignment status to in_progress
    """
    try:
        reg, m = _get_meeting(hub, meeting_id)
        target = None
        for a in m.assignments:
            if a.id == assignment_id:
                target = a
                break
        if not target:
            raise HTTPException(404, "Assignment not found")
        if not target.assignee_agent_id:
            raise HTTPException(400, "Assignment has no assignee_agent_id")

        # Find the agent
        agent = hub.get_agent(target.assignee_agent_id)
        if not agent:
            raise HTTPException(404, f"Agent not found: {target.assignee_agent_id}")

        # Create AgentTask linked to this meeting assignment
        from ...agent import AgentTask, TaskStatus
        task = AgentTask(
            title=target.title,
            description=target.description or target.title,
            source="meeting",
            source_meeting_id=meeting_id,
            source_assignment_id=assignment_id,
            assigned_by=m.host or "meeting",
        )
        agent.tasks.append(task)

        # Grant meeting workspace access
        if m.workspace_dir and m.workspace_dir not in (agent.authorized_workspaces or []):
            agent.authorized_workspaces.append(m.workspace_dir)

        # Update assignment status + link
        target.status = "in_progress"
        target.updated_at = __import__("time").time()

        reg.save()
        hub._save_agent_workspace(agent)
        return {
            "ok": True,
            "agent_task_id": task.id,
            "assignment": target.to_dict(),
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


# ---------------------------------------------------------------------------
# Progress posting (agents call this to update task status in meeting)
# ---------------------------------------------------------------------------

@router.post("/meetings/{meeting_id}/progress")
async def meeting_post_progress(
    meeting_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Agent posts a progress update for a meeting assignment."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        agent_id = body.get("agent_id", "")
        agent_name = body.get("agent_name", agent_id)
        assignment_id = body.get("assignment_id", "")
        status = body.get("status", "in_progress")
        detail = body.get("detail", "")
        if not assignment_id:
            raise HTTPException(400, "assignment_id is required")
        msg = m.post_progress(
            agent_id=agent_id,
            agent_name=agent_name,
            assignment_id=assignment_id,
            status=status,
            detail=detail,
        )
        reg.save()
        return {"ok": True, "message": msg.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


# ---------------------------------------------------------------------------
# Meeting workspace file management
# ---------------------------------------------------------------------------

@router.get("/meetings/{meeting_id}/files")
async def list_meeting_files(
    meeting_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List files in the meeting shared workspace."""
    try:
        _, m = _get_meeting(hub, meeting_id)
        return {"files": m.list_files(), "workspace_dir": m.workspace_dir}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.post("/meetings/{meeting_id}/files/upload")
async def upload_meeting_file(
    meeting_id: str,
    file: UploadFile = File(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Upload a file to the meeting shared workspace."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        if not m.workspace_dir:
            raise HTTPException(500, "meeting workspace not initialized")
        os.makedirs(m.workspace_dir, exist_ok=True)
        # Sanitize filename
        safe_name = os.path.basename(file.filename or "upload")
        dest = os.path.join(m.workspace_dir, safe_name)
        # Avoid overwriting: append suffix if exists
        base, ext = os.path.splitext(safe_name)
        counter = 1
        while os.path.exists(dest):
            dest = os.path.join(m.workspace_dir, f"{base}_{counter}{ext}")
            counter += 1
        content = await file.read()
        with open(dest, "wb") as f:
            f.write(content)
        final_name = os.path.basename(dest)
        # Auto-post system message about the upload
        actor = getattr(user, "username", "") or getattr(user, "user_id", "user")
        m.add_message(
            sender=actor,
            sender_name=actor,
            role="system",
            content=f"📎 上传文件: {final_name} ({len(content)} bytes)",
        )
        reg.save()
        return {"ok": True, "filename": final_name, "size": len(content)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.get("/meetings/{meeting_id}/files/{filename}")
async def download_meeting_file(
    meeting_id: str,
    filename: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Download a file from the meeting shared workspace."""
    try:
        _, m = _get_meeting(hub, meeting_id)
        if not m.workspace_dir:
            raise HTTPException(500, "meeting workspace not initialized")
        safe_name = os.path.basename(filename)
        fpath = os.path.join(m.workspace_dir, safe_name)
        if not os.path.isfile(fpath):
            raise HTTPException(404, f"File not found: {safe_name}")
        return FileResponse(fpath, filename=safe_name)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


@router.delete("/meetings/{meeting_id}/files/{filename}")
async def delete_meeting_file(
    meeting_id: str,
    filename: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Delete a file from the meeting shared workspace."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        if not m.workspace_dir:
            raise HTTPException(500, "meeting workspace not initialized")
        safe_name = os.path.basename(filename)
        fpath = os.path.join(m.workspace_dir, safe_name)
        if not os.path.isfile(fpath):
            raise HTTPException(404, f"File not found: {safe_name}")
        os.remove(fpath)
        actor = getattr(user, "username", "") or getattr(user, "user_id", "user")
        m.add_message(
            sender=actor, sender_name=actor, role="system",
            content=f"🗑️ 删除文件: {safe_name}",
        )
        reg.save()
        return {"ok": True, "deleted": safe_name}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, detail=str(e))


# ---------------------------------------------------------------------------
# Fallback: manage meeting via action field in body
# ---------------------------------------------------------------------------

@router.post("/meetings/{meeting_id}")
async def manage_meeting(
    meeting_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Fallback: manage meeting via action field in body."""
    try:
        reg, m = _get_meeting(hub, meeting_id)
        action = body.get("action", "")
        if action == "start":
            m.start()
        elif action == "close":
            m.close(body.get("summary", ""))
        elif action == "cancel":
            m.cancel()
        elif action == "add_participant":
            m.add_participant(body.get("agent_id", ""))
        elif action == "remove_participant":
            m.remove_participant(body.get("agent_id", ""))
        reg.save()
        return {"ok": True, "meeting": m.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
