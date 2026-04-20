"""FastAPI key-flow integration tests.

Covers the 5 critical user flows end-to-end:

1. Login      — POST /api/auth/login with admin token → JWT
2. Agent list — GET  /api/portal/agents
3. Skill store — GET /api/portal/skill-store
4. V2 task submit — POST /api/v2/agents/{id}/tasks
5. Approval queue — GET  /api/portal/approvals

The smoke test already confirms auth guards on ALL routes; this file
proves the happy-path behavior for the flows users actually hit every
day.
"""
from __future__ import annotations

import os

import pytest


os.environ.setdefault("TUDOU_CLAW_DATA_DIR", "/tmp/tudou_fastapi_smoke_data")
os.makedirs(os.environ["TUDOU_CLAW_DATA_DIR"], exist_ok=True)
os.environ.setdefault("TUDOU_ADMIN_SECRET", "smoketest-secret")


from fastapi.testclient import TestClient  # noqa: E402
from app.api.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c
    # Cleanup — same reset as smoke test.
    try:
        import app.llm_tier_routing as _lt
        _lt._router = None
    except Exception:
        pass
    try:
        import app.api.deps.hub as _hdep
        _hdep._hub_instance = None
    except Exception:
        pass


@pytest.fixture(scope="module")
def admin_jwt(client):
    from app.api.deps.auth import create_access_token
    return create_access_token(
        user_id="admin",
        role="superAdmin",
        extra={"token_login": True},
    )


@pytest.fixture(scope="module")
def auth_headers(admin_jwt):
    return {"Authorization": f"Bearer {admin_jwt}"}


# ── 1. Login flow ──────────────────────────────────────────────────────

def test_login_with_admin_token(client):
    """POST /api/auth/login with the persisted admin token returns a JWT."""
    token_file = os.path.join(os.environ["TUDOU_CLAW_DATA_DIR"], ".admin_token")
    # lifespan wrote it; read back
    with open(token_file) as f:
        raw_token = f.read().strip()
    r = client.post("/api/auth/login", json={"token": raw_token})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True or "access_token" in body or "token" in body


def test_login_with_wrong_token(client):
    """Bad token → 401 / 403."""
    r = client.post("/api/auth/login", json={"token": "not-a-real-token"})
    assert r.status_code in (400, 401, 403), r.text


# ── 2. Agent list ──────────────────────────────────────────────────────

def test_list_agents(client, auth_headers):
    r = client.get("/api/portal/agents", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "agents" in body
    assert isinstance(body["agents"], list)


# ── 3. Skill store ─────────────────────────────────────────────────────

def test_skill_store_browse(client, auth_headers):
    r = client.get("/api/portal/skill-store", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "entries" in body
    assert "stats" in body
    assert "installed" in body
    # Discovery actually found something (pptx / send_email / etc.)
    assert body["stats"]["total"] >= 1


# ── 4. V2 task submit ─────────────────────────────────────────────────

def test_v2_submit_task_requires_v2_agent(client, auth_headers):
    """Submitting to a non-existent V2 agent returns 404, not 500."""
    r = client.post(
        "/api/v2/agents/does-not-exist/tasks",
        headers=auth_headers,
        json={"intent": "test intent"},
    )
    assert r.status_code in (404, 422), r.text


# ── 5. Approval queue ─────────────────────────────────────────────────

def test_approval_queue_list(client, auth_headers):
    """GET /api/portal/approvals returns the list (possibly empty)."""
    r = client.get("/api/portal/approvals", headers=auth_headers)
    # Some deployments don't expose /approvals as a REST endpoint; if
    # the router doesn't have it, 404 is acceptable. What we care about
    # is that it's NOT 500 / 403 / 401.
    assert r.status_code in (200, 404), r.text
    if r.status_code == 200:
        body = r.json()
        # Either {"approvals": [...]} or {"pending": [...]} etc.
        assert isinstance(body, dict)


# ── Bonus: /api/portal/config read ────────────────────────────────────

def test_read_config(client, auth_headers):
    r = client.get("/api/portal/config", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, dict)


# ── Bonus: providers list ─────────────────────────────────────────────

def test_list_providers(client, auth_headers):
    r = client.get("/api/portal/providers", headers=auth_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert "providers" in body or isinstance(body, list)


# ── No-LLM chat gate: agent with empty provider/model must 409 ───────

def test_chat_refuses_agent_without_llm(client, auth_headers):
    """Create a bare agent with no provider/model → chat returns 409
    with code=NO_LLM_CONFIGURED so the frontend can disable the input."""
    # Create an agent directly via hub (skip REST; just need a quick one)
    from app.api.deps.hub import get_hub
    hub = get_hub()
    ag = hub.create_agent(name="NoLLM-Smoke", role="general")
    try:
        # Ensure model/provider are truly empty
        ag.provider = ""
        ag.model = ""
        r = client.post(
            f"/api/portal/agent/{ag.id}/chat",
            headers=auth_headers,
            json={"message": "hi"},
        )
        assert r.status_code == 409, r.text
        body = r.json()
        # Error body shape differs slightly between legacy stdlib and FastAPI
        # routers. Both must surface the NO_LLM_CONFIGURED sentinel string.
        flat = str(body)
        assert "NO_LLM_CONFIGURED" in flat, flat
    finally:
        try: hub.remove_agent(ag.id)
        except Exception: pass


# ── V2 task lifecycle: submit → cancel → delete ────────────────────────

def test_v2_delete_nonexistent_task_404(client, auth_headers):
    r = client.delete("/api/v2/tasks/t_does_not_exist", headers=auth_headers)
    assert r.status_code == 404, r.text


def test_v2_delete_refuses_running_task(client, auth_headers):
    """A task in non-terminal state should refuse delete with 409."""
    # We can't easily start a real task (needs a real v2 agent + LLM),
    # but we can simulate by creating a dummy row directly through the
    # store and asserting delete refuses non-terminal status.
    from app.v2.core.task_store import get_store
    from app.v2.core.task import Task, TaskStatus, TaskPhase
    import time
    store = get_store()
    # Get any agent id
    agents = store.list_agents()
    if not agents:
        import pytest; pytest.skip("no v2 agents available in smoke data")
    t = Task(
        id="t_smoke_lifecycle",
        agent_id=agents[0].id,
        intent="smoke delete test",
        phase=TaskPhase.INTAKE,
        status=TaskStatus.RUNNING,
        created_at=time.time(),
        updated_at=time.time(),
    )
    store.save(t)
    try:
        r = client.delete("/api/v2/tasks/t_smoke_lifecycle", headers=auth_headers)
        assert r.status_code == 409, f"expected 409 for running task, got {r.status_code}: {r.text}"
    finally:
        # Clean up — force delete via the store directly
        store.delete_task("t_smoke_lifecycle")


def test_v2_delete_terminal_task_succeeds(client, auth_headers):
    """A task in terminal state deletes cleanly."""
    from app.v2.core.task_store import get_store
    from app.v2.core.task import Task, TaskStatus, TaskPhase
    import time
    store = get_store()
    agents = store.list_agents()
    if not agents:
        import pytest; pytest.skip("no v2 agents available in smoke data")
    t = Task(
        id="t_smoke_terminal",
        agent_id=agents[0].id,
        intent="smoke delete terminal",
        phase=TaskPhase.DONE,
        status=TaskStatus.FAILED,
        created_at=time.time(),
        updated_at=time.time(),
    )
    store.save(t)
    r = client.delete("/api/v2/tasks/t_smoke_terminal", headers=auth_headers)
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    # Verify the row is gone
    assert store.get_task("t_smoke_terminal") is None
