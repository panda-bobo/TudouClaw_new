"""Tests for V1 ↔ V2 agent id pairing.

V2 agents can be created with a caller-chosen id so they share that id
with the paired V1 agent. That lets the frontend probe
``/api/v2/agents/<v1_id>`` directly instead of maintaining a separate
V1→V2 lookup table.

Also verifies the id-conflict guard that prevents silent overwrites.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def v2_client(monkeypatch, tmp_path):
    monkeypatch.setenv("TUDOU_CLAW_DB_PATH", str(tmp_path / "v2.db"))
    import app.v2.core.task_store as ts_mod
    monkeypatch.setattr(ts_mod, "_STORE", None)

    import app.api.routers.v2 as v2mod
    monkeypatch.setattr(v2mod, "_bus_singleton", None)

    from app.api.deps.auth import get_current_user, CurrentUser

    async def _admin():
        return CurrentUser(user_id="u1", role="superAdmin")

    app = FastAPI()
    app.dependency_overrides[get_current_user] = _admin
    app.dependency_overrides[v2mod._sse_auth_dep] = _admin
    app.include_router(v2mod.router)

    with TestClient(app) as c:
        yield c


def test_create_agent_with_explicit_id_adopts_it(v2_client):
    """POST /api/v2/agents with body.id uses that id verbatim."""
    r = v2_client.post("/api/v2/agents", json={
        "id": "abc123def456",   # V1-style id
        "v1_agent_id": "abc123def456",
        "name": "TestBot",
        "role": "assistant",
        "capabilities": {},
        "task_template_ids": ["conversation"],
    })
    assert r.status_code == 201, r.text
    agent = r.json()["agent"]
    assert agent["id"] == "abc123def456"
    assert agent["v1_agent_id"] == "abc123def456"

    # And GET /api/v2/agents/<v1_id> now resolves instead of 404.
    r2 = v2_client.get(f"/api/v2/agents/{agent['id']}")
    assert r2.status_code == 200


def test_create_agent_without_id_generates_av2_prefix(v2_client):
    """When body.id is absent, a fresh av2_* id is generated (legacy V2-only path)."""
    r = v2_client.post("/api/v2/agents", json={
        "name": "Solo",
        "role": "assistant",
        "capabilities": {},
        "task_template_ids": [],
    })
    assert r.status_code == 201
    aid = r.json()["agent"]["id"]
    assert aid.startswith("av2_"), f"expected av2_* id, got {aid!r}"


def test_create_agent_with_duplicate_id_returns_409(v2_client):
    """Repeatedly POSTing with the same id must NOT silently overwrite —
    the old behavior of generating fresh av2_* shells on every click
    was what caused 9 duplicate agents in the wild."""
    body = {
        "id": "same_id_123",
        "name": "Doppelganger",
        "role": "assistant",
        "capabilities": {},
        "task_template_ids": [],
    }
    r1 = v2_client.post("/api/v2/agents", json=body)
    assert r1.status_code == 201

    r2 = v2_client.post("/api/v2/agents", json=body)
    assert r2.status_code == 409
    assert r2.json()["detail"]["error_code"] == "ID_CONFLICT"


def test_get_by_v1_id_after_pairing_resolves(v2_client):
    """End-to-end pairing: create V2 shell with V1 id, then probe from
    the frontend perspective (GET /api/v2/agents/<v1_id>) → 200."""
    v1_id = "deadbeef1234"
    v2_client.post("/api/v2/agents", json={
        "id": v1_id,
        "v1_agent_id": v1_id,
        "name": "Paired",
        "role": "analyst",
        "capabilities": {"llm_tier": "coding_strong"},
        "task_template_ids": ["conversation", "research_report"],
    })

    r = v2_client.get(f"/api/v2/agents/{v1_id}")
    assert r.status_code == 200
    data = r.json()["agent"]
    assert data["id"] == v1_id
    assert data["v1_agent_id"] == v1_id
    assert data["capabilities"]["llm_tier"] == "coding_strong"
    assert "research_report" in data["task_template_ids"]
