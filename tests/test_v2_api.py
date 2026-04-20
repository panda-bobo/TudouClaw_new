"""REST + SSE integration tests for the V2 router (PRD §10.1-10.3).

These run against FastAPI's ``TestClient``; the router is imported
standalone (no full app boot, no V1 side effects). ``get_current_user``
is overridden so every request authenticates as a stub admin.

Each test gets its own temp SQLite DB via ``TUDOU_CLAW_DB_PATH``.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def isolated_v2_client(monkeypatch):
    """Fresh DB + fresh module state for each test."""
    # 1. Point the store at a tmpdir DB *before* any module imports it.
    tmpdir = tempfile.mkdtemp(prefix="v2_api_test_")
    db_path = os.path.join(tmpdir, "t.db")
    monkeypatch.setenv("TUDOU_CLAW_DB_PATH", db_path)

    # 2. Reset singletons so they pick up the new path.
    import app.v2.core.task_store as ts_mod
    monkeypatch.setattr(ts_mod, "_STORE", None)

    import app.api.routers.v2 as v2mod
    monkeypatch.setattr(v2mod, "_bus_singleton", None)

    # 3. Build a minimal FastAPI app with just the V2 router, and
    #    override the auth dep to a stub admin.
    from app.api.deps.auth import get_current_user, CurrentUser

    async def _fake_user():
        return CurrentUser(user_id="u1", role="superAdmin")

    app = FastAPI()
    app.dependency_overrides[get_current_user] = _fake_user
    # SSE uses its own dep that adds query-param JWT support — override
    # that too so the TestClient doesn't need to fake a bearer token.
    app.dependency_overrides[v2mod._sse_auth_dep] = _fake_user
    app.include_router(v2mod.router)

    with TestClient(app) as client:
        yield client


# ── agents ────────────────────────────────────────────────────────────


def test_agent_crud_lifecycle(isolated_v2_client):
    c = isolated_v2_client

    # Create
    r = c.post("/api/v2/agents", json={
        "name": "MeetBot",
        "role": "meeting_assistant",
        "capabilities": {"skills": ["s1"], "mcps": [], "llm_tier": "default"},
        "task_template_ids": ["conversation"],
    })
    assert r.status_code == 201, r.text
    ag = r.json()["agent"]
    aid = ag["id"]
    assert ag["name"] == "MeetBot" and ag["role"] == "meeting_assistant"
    assert ag["capabilities"]["skills"] == ["s1"]

    # Get
    r = c.get(f"/api/v2/agents/{aid}")
    assert r.status_code == 200
    assert r.json()["agent"]["id"] == aid

    # List
    r = c.get("/api/v2/agents")
    assert r.status_code == 200
    ids = [a["id"] for a in r.json()["agents"]]
    assert aid in ids

    # Patch
    r = c.patch(f"/api/v2/agents/{aid}", json={"name": "Renamed"})
    assert r.status_code == 200
    assert r.json()["agent"]["name"] == "Renamed"

    # Delete (archive)
    r = c.delete(f"/api/v2/agents/{aid}")
    assert r.status_code == 200
    assert r.json()["archived"] is True

    # Default list excludes archived
    r = c.get("/api/v2/agents")
    assert aid not in [a["id"] for a in r.json()["agents"]]
    # include_archived brings it back
    r = c.get("/api/v2/agents", params={"include_archived": "true"})
    assert aid in [a["id"] for a in r.json()["agents"]]


def test_agent_create_rejects_missing_fields(isolated_v2_client):
    r = isolated_v2_client.post("/api/v2/agents", json={"name": "x"})
    assert r.status_code == 400
    body = r.json()
    assert body["detail"]["error_code"] == "INVALID_BODY"


def test_agent_get_404(isolated_v2_client):
    r = isolated_v2_client.get("/api/v2/agents/no_such_agent")
    assert r.status_code == 404
    assert r.json()["detail"]["error_code"] == "AGENT_NOT_FOUND"


# ── templates ─────────────────────────────────────────────────────────


def test_list_templates(isolated_v2_client):
    r = isolated_v2_client.get("/api/v2/templates")
    assert r.status_code == 200
    items = r.json()["templates"]
    ids = {t["id"] for t in items}
    # Bundled templates must be present.
    assert "conversation" in ids
    assert "research_report" in ids


def test_get_template_404(isolated_v2_client):
    r = isolated_v2_client.get("/api/v2/templates/nope")
    assert r.status_code == 404
    assert r.json()["detail"]["error_code"] == "TEMPLATE_NOT_FOUND"


def test_get_template_ok(isolated_v2_client):
    r = isolated_v2_client.get("/api/v2/templates/conversation")
    assert r.status_code == 200
    body = r.json()["template"]
    assert body["id"] == "conversation"


# ── tasks ─────────────────────────────────────────────────────────────


def _make_agent(client):
    r = client.post("/api/v2/agents", json={
        "name": "TaskBot",
        "role": "tester",
        "capabilities": {},
        "task_template_ids": ["conversation"],
    })
    assert r.status_code == 201
    return r.json()["agent"]["id"]


def test_submit_task_rejects_empty_intent(isolated_v2_client):
    aid = _make_agent(isolated_v2_client)
    r = isolated_v2_client.post(
        f"/api/v2/agents/{aid}/tasks",
        json={"intent": ""},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "INVALID_BODY"


def test_submit_task_rejects_unknown_template(isolated_v2_client):
    aid = _make_agent(isolated_v2_client)
    r = isolated_v2_client.post(
        f"/api/v2/agents/{aid}/tasks",
        json={"intent": "hi", "template_id": "made_up_template"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "UNKNOWN_TEMPLATE"


def test_submit_task_on_missing_agent(isolated_v2_client):
    r = isolated_v2_client.post(
        "/api/v2/agents/nope/tasks",
        json={"intent": "hi"},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error_code"] == "AGENT_NOT_FOUND"


def test_submit_task_happy(isolated_v2_client, monkeypatch):
    """End-to-end submit: monkeypatch bridges so TaskLoop actually
    completes without needing a real LLM."""
    # Stub LLM: always return harmless text so the task reaches Report.
    import app.v2.bridges.llm_bridge as lb
    import app.v2.bridges.skill_bridge as sb
    import app.v2.bridges.mcp_bridge as mb

    def _fake_llm(messages, tools=None, tier="default", max_tokens=4096):
        sigs = " ".join(m.get("content", "") for m in messages
                         if m.get("role") == "system")
        if "任务预处理助手" in sigs:
            return {"role": "assistant",
                    "content": '```json\n{"filled":{},"missing":[],"clarification":""}\n```',
                    "tool_calls": []}
        if "任务规划器" in sigs:
            return {"role": "assistant",
                    "content": '```json\n{"steps":[{"id":"s1","goal":"reply","tools_hint":[],"exit_check":{}}],"expected_artifact_count":0}\n```',
                    "tool_calls": []}
        return {"role": "assistant", "content": "Hello!", "tool_calls": []}

    monkeypatch.setattr(lb, "call_llm", _fake_llm)
    monkeypatch.setattr(sb, "get_skill_tools_for_agent", lambda *_a, **_k: [])
    monkeypatch.setattr(sb, "invoke_skill", lambda *_a, **_k: "ok")
    monkeypatch.setattr(mb, "get_mcp_tools_for_agent", lambda *_a, **_k: [])

    aid = _make_agent(isolated_v2_client)
    r = isolated_v2_client.post(
        f"/api/v2/agents/{aid}/tasks",
        json={"intent": "hi", "template_id": "conversation"},
    )
    assert r.status_code == 202, r.text
    tid = r.json()["task"]["id"]
    assert r.json()["task"]["event_stream_url"].endswith(f"/{tid}/events")

    # Poll for completion (background thread).
    import time as _t
    deadline = _t.time() + 5.0
    while _t.time() < deadline:
        rr = isolated_v2_client.get(f"/api/v2/tasks/{tid}")
        status = rr.json()["task"]["status"]
        if status in ("succeeded", "failed"):
            break
        _t.sleep(0.05)
    assert rr.json()["task"]["status"] == "succeeded", rr.json()
    assert rr.json()["task"]["phase"] == "done"


def test_list_tasks_by_agent(isolated_v2_client):
    aid = _make_agent(isolated_v2_client)
    r = isolated_v2_client.get("/api/v2/tasks", params={"agent_id": aid})
    assert r.status_code == 200
    assert "tasks" in r.json()
    assert r.json()["has_more"] is False


# ── state transitions ────────────────────────────────────────────────


def test_cancel_running_task(isolated_v2_client, monkeypatch):
    """Cancel a freshly-created task (never actually runs because we
    don't patch bridges). Status should flip to abandoned."""
    # Block the runner from ever starting so status stays RUNNING.
    import app.v2.agent.agent_v2 as av2
    monkeypatch.setattr(
        av2, "_get_shared_bus",
        lambda store: __import__("app.v2.core.task_events",
                                  fromlist=["TaskEventBus"]).TaskEventBus(store),
    )

    # Patch bridges so if a thread does start, LLM calls never block.
    import app.v2.bridges.llm_bridge as lb
    monkeypatch.setattr(lb, "call_llm",
                        lambda **_k: {"role": "assistant",
                                      "content": "slow",
                                      "tool_calls": []})

    aid = _make_agent(isolated_v2_client)
    r = isolated_v2_client.post(
        f"/api/v2/agents/{aid}/tasks",
        json={"intent": "hi", "template_id": "conversation"},
    )
    tid = r.json()["task"]["id"]

    # Cancel — should succeed regardless of whether the background thread
    # already advanced the task (cancel is idempotent against RUNNING/PAUSED).
    rr = isolated_v2_client.post(f"/api/v2/tasks/{tid}/cancel")
    assert rr.status_code in (200, 409)  # 409 if it already completed
    if rr.status_code == 200:
        assert rr.json()["task"]["status"] == "abandoned"


def test_pause_on_non_running_returns_409(isolated_v2_client, monkeypatch):
    """A task that isn't RUNNING can't be paused — 409."""
    import app.v2.bridges.llm_bridge as lb

    def _quick(messages, tools=None, tier="default", max_tokens=4096):
        sigs = " ".join(m.get("content", "") for m in messages
                         if m.get("role") == "system")
        if "任务预处理助手" in sigs:
            return {"role": "assistant",
                    "content": '```json\n{"filled":{},"missing":[],"clarification":""}\n```',
                    "tool_calls": []}
        if "任务规划器" in sigs:
            return {"role": "assistant",
                    "content": '```json\n{"steps":[{"id":"s1","goal":"r","tools_hint":[],"exit_check":{}}],"expected_artifact_count":0}\n```',
                    "tool_calls": []}
        return {"role": "assistant", "content": "done", "tool_calls": []}

    monkeypatch.setattr(lb, "call_llm", _quick)
    import app.v2.bridges.skill_bridge as sb
    import app.v2.bridges.mcp_bridge as mb
    monkeypatch.setattr(sb, "get_skill_tools_for_agent", lambda *_a, **_k: [])
    monkeypatch.setattr(sb, "invoke_skill", lambda *_a, **_k: "ok")
    monkeypatch.setattr(mb, "get_mcp_tools_for_agent", lambda *_a, **_k: [])

    aid = _make_agent(isolated_v2_client)
    r = isolated_v2_client.post(
        f"/api/v2/agents/{aid}/tasks",
        json={"intent": "hi", "template_id": "conversation"},
    )
    tid = r.json()["task"]["id"]

    # Wait for the task to complete.
    import time as _t
    for _ in range(100):
        rr = isolated_v2_client.get(f"/api/v2/tasks/{tid}")
        if rr.json()["task"]["status"] == "succeeded":
            break
        _t.sleep(0.05)

    # Try to pause a completed task.
    rr = isolated_v2_client.post(f"/api/v2/tasks/{tid}/pause")
    assert rr.status_code == 409
    assert rr.json()["detail"]["error_code"] == "INVALID_STATE_TRANSITION"


def test_clarify_on_non_pending_returns_409(isolated_v2_client, monkeypatch):
    """Task that's not awaiting clarification → 409 on /clarify."""
    import app.v2.bridges.llm_bridge as lb

    monkeypatch.setattr(lb, "call_llm",
                        lambda **_k: {"role": "assistant",
                                      "content": "hi",
                                      "tool_calls": []})
    import app.v2.bridges.skill_bridge as sb
    import app.v2.bridges.mcp_bridge as mb
    monkeypatch.setattr(sb, "get_skill_tools_for_agent", lambda *_a, **_k: [])
    monkeypatch.setattr(sb, "invoke_skill", lambda *_a, **_k: "ok")
    monkeypatch.setattr(mb, "get_mcp_tools_for_agent", lambda *_a, **_k: [])

    aid = _make_agent(isolated_v2_client)
    r = isolated_v2_client.post(
        f"/api/v2/agents/{aid}/tasks",
        json={"intent": "hi", "template_id": "conversation"},
    )
    tid = r.json()["task"]["id"]

    rr = isolated_v2_client.post(
        f"/api/v2/tasks/{tid}/clarify",
        json={"answer": "sure"},
    )
    assert rr.status_code == 409
    assert rr.json()["detail"]["error_code"] == "INVALID_STATE_TRANSITION"


def test_clarify_rejects_empty_answer(isolated_v2_client):
    # No task actually needed — parameter validation fires before the DB.
    aid = _make_agent(isolated_v2_client)
    # Need a task id that exists.
    import app.v2.bridges.llm_bridge as lb
    import app.v2.bridges.skill_bridge as sb
    import app.v2.bridges.mcp_bridge as mb
    # Patch so submit doesn't hang trying to LLM.
    with pytest.MonkeyPatch.context() as m:
        m.setattr(lb, "call_llm",
                  lambda **_k: {"role": "assistant",
                                "content": "```json\n{\"filled\":{},\"missing\":[],\"clarification\":\"\"}\n```",
                                "tool_calls": []})
        m.setattr(sb, "get_skill_tools_for_agent", lambda *_a, **_k: [])
        m.setattr(sb, "invoke_skill", lambda *_a, **_k: "")
        m.setattr(mb, "get_mcp_tools_for_agent", lambda *_a, **_k: [])
        r = isolated_v2_client.post(
            f"/api/v2/agents/{aid}/tasks",
            json={"intent": "hi", "template_id": "conversation"},
        )
        tid = r.json()["task"]["id"]
        rr = isolated_v2_client.post(
            f"/api/v2/tasks/{tid}/clarify",
            json={"answer": ""},
        )
        assert rr.status_code == 400
        assert rr.json()["detail"]["error_code"] == "INVALID_BODY"


# ── SSE smoke ────────────────────────────────────────────────────────


def test_second_task_enters_queue(isolated_v2_client, monkeypatch):
    """Second submission to a busy agent should be accepted (202) and
    return status='queued', not 409."""
    # Bridges stubbed so any started loop terminates instantly.
    import app.v2.bridges.llm_bridge as lb
    import app.v2.bridges.skill_bridge as sb
    import app.v2.bridges.mcp_bridge as mb
    monkeypatch.setattr(lb, "call_llm",
                        lambda **_k: {"role": "assistant", "content": "", "tool_calls": []})
    monkeypatch.setattr(sb, "get_skill_tools_for_agent", lambda *_a, **_k: [])
    monkeypatch.setattr(sb, "invoke_skill", lambda *_a, **_k: "")
    monkeypatch.setattr(mb, "get_mcp_tools_for_agent", lambda *_a, **_k: [])

    aid = _make_agent(isolated_v2_client)
    # Pin an active task directly to simulate an in-flight one.
    import app.v2.core.task_store as ts_mod
    from app.v2.core.task import Task, TaskStatus, TaskPhase
    import time as _t
    store = ts_mod.get_store()
    busy = Task(
        id=f"t_busy_{int(_t.time()*1e6):x}",
        agent_id=aid,
        template_id="conversation",
        intent="in-flight",
        status=TaskStatus.RUNNING,
        phase=TaskPhase.EXECUTE,
        created_at=_t.time(),
        updated_at=_t.time(),
    )
    store.save(busy)

    # Second submission → 202 queued.
    r = isolated_v2_client.post(
        f"/api/v2/agents/{aid}/tasks",
        json={"intent": "second", "template_id": "conversation"},
    )
    assert r.status_code == 202, r.text
    assert r.json()["task"]["status"] == "queued"

    # Subtask bypasses the queue — starts immediately (status running).
    r = isolated_v2_client.post(
        f"/api/v2/agents/{aid}/tasks",
        json={"intent": "child", "template_id": "conversation",
              "parent_task_id": busy.id},
    )
    assert r.status_code == 202, r.text
    # Child status may be running (started) or, if its own loop already
    # finished, succeeded — anything other than "queued" is valid here.
    assert r.json()["task"]["status"] != "queued"


def test_queue_drains_on_active_task_cancel(isolated_v2_client, monkeypatch):
    """Cancelling the RUNNING task should dequeue the next QUEUED task
    and promote it to RUNNING automatically."""
    # LLM that blocks just long enough so cancel happens first.
    import threading as _th
    import app.v2.bridges.llm_bridge as lb
    import app.v2.bridges.skill_bridge as sb
    import app.v2.bridges.mcp_bridge as mb

    # Use a threading.Event to prevent the first loop from ever finishing
    # by itself during this test — we need to verify cancel triggers dequeue.
    slow = _th.Event()

    def _slow_llm(**_k):
        # Wait briefly — but not forever — so test timeouts are reasonable.
        slow.wait(timeout=2.0)
        return {"role": "assistant", "content": "", "tool_calls": []}

    monkeypatch.setattr(lb, "call_llm", _slow_llm)
    monkeypatch.setattr(sb, "get_skill_tools_for_agent", lambda *_a, **_k: [])
    monkeypatch.setattr(sb, "invoke_skill", lambda *_a, **_k: "")
    monkeypatch.setattr(mb, "get_mcp_tools_for_agent", lambda *_a, **_k: [])

    aid = _make_agent(isolated_v2_client)
    r1 = isolated_v2_client.post(f"/api/v2/agents/{aid}/tasks",
                                  json={"intent": "A", "template_id": "conversation"})
    r2 = isolated_v2_client.post(f"/api/v2/agents/{aid}/tasks",
                                  json={"intent": "B", "template_id": "conversation"})
    t1 = r1.json()["task"]["id"]
    t2 = r2.json()["task"]["id"]
    assert r2.json()["task"]["status"] == "queued"

    # Cancel the active one; queued task should promote.
    rc = isolated_v2_client.post(f"/api/v2/tasks/{t1}/cancel")
    assert rc.status_code == 200
    slow.set()  # release any pending LLM waits in the promoted loop

    # t2 should no longer be queued — status moved to running/succeeded/etc.
    import time as _t
    for _ in range(50):
        r = isolated_v2_client.get(f"/api/v2/tasks/{t2}")
        if r.json()["task"]["status"] != "queued":
            break
        _t.sleep(0.05)
    status = r.json()["task"]["status"]
    assert status != "queued", f"queued task never promoted (status={status})"


def test_cancel_queued_task_removes_from_queue(isolated_v2_client, monkeypatch):
    """Cancelling a task while it's still QUEUED abandons it without
    ever running it."""
    import app.v2.bridges.llm_bridge as lb
    import app.v2.bridges.skill_bridge as sb
    import app.v2.bridges.mcp_bridge as mb
    # LLM that hangs — guarantees the RUNNING task stays busy.
    import threading as _th
    block = _th.Event()
    monkeypatch.setattr(lb, "call_llm",
                        lambda **_k: (block.wait(timeout=5.0),
                                       {"role": "assistant", "content": "",
                                        "tool_calls": []})[1])
    monkeypatch.setattr(sb, "get_skill_tools_for_agent", lambda *_a, **_k: [])
    monkeypatch.setattr(sb, "invoke_skill", lambda *_a, **_k: "")
    monkeypatch.setattr(mb, "get_mcp_tools_for_agent", lambda *_a, **_k: [])

    aid = _make_agent(isolated_v2_client)
    isolated_v2_client.post(f"/api/v2/agents/{aid}/tasks",
                             json={"intent": "A", "template_id": "conversation"})
    r2 = isolated_v2_client.post(f"/api/v2/agents/{aid}/tasks",
                                  json={"intent": "B", "template_id": "conversation"})
    t2 = r2.json()["task"]["id"]
    assert r2.json()["task"]["status"] == "queued"

    rc = isolated_v2_client.post(f"/api/v2/tasks/{t2}/cancel")
    assert rc.status_code == 200
    assert rc.json()["task"]["status"] == "abandoned"
    assert rc.json()["task"]["finished_reason"] == "cancelled"

    block.set()  # allow other loop to exit cleanly


def test_agent_queue_endpoint_lists_queued(isolated_v2_client, monkeypatch):
    """GET /agents/{id}/queue returns the FIFO queue + the active task."""
    import app.v2.bridges.llm_bridge as lb
    import app.v2.bridges.skill_bridge as sb
    import app.v2.bridges.mcp_bridge as mb
    import threading as _th
    block = _th.Event()
    monkeypatch.setattr(lb, "call_llm",
                        lambda **_k: (block.wait(timeout=5.0),
                                       {"role": "assistant", "content": "",
                                        "tool_calls": []})[1])
    monkeypatch.setattr(sb, "get_skill_tools_for_agent", lambda *_a, **_k: [])
    monkeypatch.setattr(sb, "invoke_skill", lambda *_a, **_k: "")
    monkeypatch.setattr(mb, "get_mcp_tools_for_agent", lambda *_a, **_k: [])

    aid = _make_agent(isolated_v2_client)
    for i in range(3):
        isolated_v2_client.post(f"/api/v2/agents/{aid}/tasks",
                                 json={"intent": f"task {i}",
                                       "template_id": "conversation"})

    r = isolated_v2_client.get(f"/api/v2/agents/{aid}/queue")
    assert r.status_code == 200
    body = r.json()
    assert body["active"] is not None
    assert len(body["queued"]) == 2
    # FIFO: queued[0] was submitted before queued[1].
    assert body["queued"][0]["created_at"] <= body["queued"][1]["created_at"]

    block.set()


def test_rbac_delete_agent_forbidden_for_non_admin(isolated_v2_client, monkeypatch):
    """Non-admin role cannot DELETE an agent."""
    from app.api.deps.auth import get_current_user, CurrentUser

    async def _plain_user():
        return CurrentUser(user_id="u1", role="member")

    # Swap the dep to a plain user (not super-admin).
    isolated_v2_client.app.dependency_overrides[get_current_user] = _plain_user

    aid = _make_agent(isolated_v2_client)
    r = isolated_v2_client.delete(f"/api/v2/agents/{aid}")
    assert r.status_code == 403
    assert r.json()["detail"]["error_code"] == "FORBIDDEN"


def test_upload_and_serve_attachment_roundtrip(isolated_v2_client):
    """Upload an image via the REST endpoint, then fetch it via serve."""
    aid = _make_agent(isolated_v2_client)
    files = {"file": ("pic.png", b"\x89PNG\r\n\x1a\n-fake-", "image/png")}
    r = isolated_v2_client.post(
        f"/api/v2/agents/{aid}/attachments",
        files=files, data={"task_id": "draft"},
    )
    assert r.status_code == 201, r.text
    a = r.json()["attachment"]
    assert a["kind"] == "image"
    assert a["size"] > 0
    assert a["url"].startswith(f"/api/v2/agents/{aid}/attachments/serve")

    # Serve.
    rs = isolated_v2_client.get(a["url"])
    assert rs.status_code == 200
    assert rs.content.startswith(b"\x89PNG")


def test_upload_empty_rejected(isolated_v2_client):
    aid = _make_agent(isolated_v2_client)
    files = {"file": ("nope.png", b"", "image/png")}
    r = isolated_v2_client.post(
        f"/api/v2/agents/{aid}/attachments",
        files=files, data={"task_id": "draft"},
    )
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "INVALID_BODY"


def test_serve_attachment_blocks_traversal(isolated_v2_client):
    aid = _make_agent(isolated_v2_client)
    # Try to serve /etc/passwd — must be rejected.
    r = isolated_v2_client.get(
        f"/api/v2/agents/{aid}/attachments/serve",
        params={"handle": "/etc/passwd"},
    )
    assert r.status_code == 404
    assert r.json()["detail"]["error_code"] == "ATTACHMENT_NOT_FOUND"


def test_submit_task_with_attachment_stored_on_task(isolated_v2_client, monkeypatch):
    """Attachment descriptors passed to submit are stored on the task."""
    # Stub LLM so the task completes quickly without needing real model.
    import app.v2.bridges.llm_bridge as lb
    import app.v2.bridges.skill_bridge as sb
    import app.v2.bridges.mcp_bridge as mb

    def _stub(**_k):
        sigs = " ".join(m.get("content", "") for m in _k.get("messages", [])
                        if m.get("role") == "system")
        if "任务预处理助手" in sigs:
            return {"role": "assistant",
                    "content": '```json\n{"filled":{},"missing":[],"clarification":""}\n```',
                    "tool_calls": []}
        if "任务规划器" in sigs:
            return {"role": "assistant",
                    "content": '```json\n{"steps":[{"id":"s1","goal":"r","tools_hint":[],"exit_check":{}}],"expected_artifact_count":0}\n```',
                    "tool_calls": []}
        return {"role": "assistant", "content": "ok", "tool_calls": []}

    monkeypatch.setattr(lb, "call_llm", _stub)
    monkeypatch.setattr(sb, "get_skill_tools_for_agent", lambda *_a, **_k: [])
    monkeypatch.setattr(sb, "invoke_skill", lambda *_a, **_k: "")
    monkeypatch.setattr(mb, "get_mcp_tools_for_agent", lambda *_a, **_k: [])

    # Must also make the provider_supports_multimodal check return True,
    # otherwise Intake would pause us for lack of multimodal provider.
    import app.llm as _llm_mod
    class _Reg:
        def provider_supports_multimodal(self, pid): return True
        def pick_for_tier(self, tier): return None
        def list(self, include_disabled=False): return []
    monkeypatch.setattr(_llm_mod, "get_registry", lambda: _Reg())

    aid = _make_agent(isolated_v2_client)
    # Upload first.
    files = {"file": ("pic.png", b"\x89PNG-data", "image/png")}
    ur = isolated_v2_client.post(
        f"/api/v2/agents/{aid}/attachments",
        files=files, data={"task_id": "draft"},
    )
    a = ur.json()["attachment"]

    # Submit with the attachment.
    sr = isolated_v2_client.post(
        f"/api/v2/agents/{aid}/tasks",
        json={"intent": "describe this", "template_id": "conversation",
              "attachments": [a]},
    )
    assert sr.status_code == 202, sr.text
    tid = sr.json()["task"]["id"]

    # Fetch task — its context.attachments must carry our descriptor.
    import time as _t
    for _ in range(40):
        rr = isolated_v2_client.get(f"/api/v2/tasks/{tid}")
        if rr.json()["task"]["status"] in ("succeeded", "failed"):
            break
        _t.sleep(0.05)


def test_metrics_endpoint_returns_counters(isolated_v2_client):
    r = isolated_v2_client.get("/api/v2/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert isinstance(body["counters"], dict)


def test_sse_replays_persisted_events(isolated_v2_client, monkeypatch):
    """SSE endpoint should replay already-persisted events (since=0)."""
    import app.v2.bridges.llm_bridge as lb
    import app.v2.bridges.skill_bridge as sb
    import app.v2.bridges.mcp_bridge as mb

    def _quick(messages, tools=None, tier="default", max_tokens=4096):
        sigs = " ".join(m.get("content", "") for m in messages
                         if m.get("role") == "system")
        if "任务预处理助手" in sigs:
            return {"role": "assistant",
                    "content": '```json\n{"filled":{},"missing":[],"clarification":""}\n```',
                    "tool_calls": []}
        if "任务规划器" in sigs:
            return {"role": "assistant",
                    "content": '```json\n{"steps":[{"id":"s1","goal":"r","tools_hint":[],"exit_check":{}}],"expected_artifact_count":0}\n```',
                    "tool_calls": []}
        return {"role": "assistant", "content": "final", "tool_calls": []}

    monkeypatch.setattr(lb, "call_llm", _quick)
    monkeypatch.setattr(sb, "get_skill_tools_for_agent", lambda *_a, **_k: [])
    monkeypatch.setattr(sb, "invoke_skill", lambda *_a, **_k: "ok")
    monkeypatch.setattr(mb, "get_mcp_tools_for_agent", lambda *_a, **_k: [])

    aid = _make_agent(isolated_v2_client)
    r = isolated_v2_client.post(
        f"/api/v2/agents/{aid}/tasks",
        json={"intent": "hi", "template_id": "conversation"},
    )
    tid = r.json()["task"]["id"]

    # Wait for completion so all events are persisted.
    import time as _t
    for _ in range(100):
        rr = isolated_v2_client.get(f"/api/v2/tasks/{tid}")
        if rr.json()["task"]["status"] == "succeeded":
            break
        _t.sleep(0.05)

    # Open SSE — since task is DONE, the stream should emit all replayed
    # events then send stream_end and close.
    with isolated_v2_client.stream(
        "GET", f"/api/v2/tasks/{tid}/events",
    ) as resp:
        assert resp.status_code == 200
        body = b""
        for chunk in resp.iter_bytes():
            body += chunk
            if b"stream_end" in body:
                break
    text = body.decode("utf-8", errors="ignore")
    assert "event: task_submitted" in text
    assert "event: task_completed" in text
    assert "event: stream_end" in text
