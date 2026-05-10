"""V1 vertical tests: 4 expert API endpoints (replaces 501 stubs).

Spec: docs/superpowers/specs/2026-05-10-agent-specialty-cultivation-design.md §5.1
Plan: docs/superpowers/plans/2026-05-10-INDEX.md V1 vertical

Tests use FastAPI TestClient + a mocked Hub so we don't need the real
agent registry / SQLite / etc. Auth is bypassed via dependency override.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.domain_expert.api.routers import router as expert_router
from app.domain_expert import _config


# ── Test app + auth/hub overrides ──

def _build_test_app(hub_mock):
    """Build a minimal FastAPI app with the expert router and dependency
    overrides for auth + hub."""
    app = FastAPI()
    app.include_router(expert_router)

    # Override auth to always return a fake user
    from app.api.deps.auth import get_current_user
    from app.api.deps.hub import get_hub
    fake_user = MagicMock()
    fake_user.user_id = "test_user"
    fake_user.role = "admin"
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[get_hub] = lambda: hub_mock
    return app


def _mk_hub_with_agent(agent_id="ag1", **agent_attrs):
    """Build a Hub mock with one agent. agent_attrs override defaults."""
    agent = MagicMock()
    agent.id = agent_id
    agent.name = agent_attrs.get("name", "test-agent")
    agent.expert_specialty = agent_attrs.get("expert_specialty", "")
    agent.expert_template_version = agent_attrs.get("expert_template_version", "")
    agent.expert_level = agent_attrs.get("expert_level", "novice")
    agent.expert_lora_version = agent_attrs.get("expert_lora_version", "")
    agent.expert_initialized_at = agent_attrs.get("expert_initialized_at", 0.0)
    hub = MagicMock()
    hub.agents = {agent_id: agent}
    hub._save_agents = MagicMock()
    return hub, agent


# ── GET /specialty-templates ──

def test_list_templates_returns_legal():
    """The shipped legal.yaml should appear in the catalog."""
    hub, _ = _mk_hub_with_agent()
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.get("/api/portal/specialty-templates")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    template_ids = [t["id"] for t in body["templates"]]
    assert "legal-expert" in template_ids
    legal = next(t for t in body["templates"] if t["id"] == "legal-expert")
    assert legal["specialty"] == "legal"
    assert legal["icon"]  # non-empty
    assert legal["required_packs_count"] >= 8
    assert legal["level_count"] >= 3


# ── GET /specialty-templates/{id} ──

def test_get_template_legal_returns_full_schema():
    hub, _ = _mk_hub_with_agent()
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.get("/api/portal/specialty-templates/legal")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "legal-expert"
    # Full schema fields present
    assert "eval_suite" in body
    assert "level_rules" in body
    assert "chunker" in body
    assert "training" in body
    # eval_suite refers to Track C runner IDs
    runner_ids = [e["runner_id"] for e in body["eval_suite"]]
    assert "legalbench_zh" in runner_ids
    assert "citation_accuracy" in runner_ids


def test_get_template_unknown_404():
    hub, _ = _mk_hub_with_agent()
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.get("/api/portal/specialty-templates/nonexistent_xyz")
    assert resp.status_code == 404


# ── GET /agent/{id}/expert ──

def test_get_expert_status_uncultivated():
    """A regular agent (no expert_specialty set) should get cultivated=false."""
    hub, agent = _mk_hub_with_agent("ag-plain", name="plain-agent")
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.get("/api/portal/agent/ag-plain/expert")
    assert resp.status_code == 200
    body = resp.json()
    assert body["agent_id"] == "ag-plain"
    assert body["agent_name"] == "plain-agent"
    assert body["cultivated"] is False
    assert body["expert_specialty"] == ""
    assert body["expert_level"] == "novice"
    assert body["profile"] is None


def test_get_expert_status_cultivated_loads_profile(tmp_path, monkeypatch):
    """A cultivated agent should report cultivated=true + profile from disk."""
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    # Pre-create the on-disk profile
    from app.domain_expert.profile import ExpertProfile
    p = ExpertProfile(
        agent_id="ag-cult", specialty="legal",
        template_id="legal-expert", template_version="1.0",
        level="journeyman",
    )
    p.save()

    hub, agent = _mk_hub_with_agent(
        "ag-cult", name="legal-小法",
        expert_specialty="legal",
        expert_template_version="1.0",
        expert_level="journeyman",
        expert_initialized_at=1234567890.0,
    )
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.get("/api/portal/agent/ag-cult/expert")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cultivated"] is True
    assert body["expert_specialty"] == "legal"
    assert body["expert_level"] == "journeyman"
    assert body["expert_initialized_at"] == 1234567890.0
    # On-disk profile loaded
    assert body["profile"] is not None
    assert body["profile"]["specialty"] == "legal"
    assert body["profile"]["template_id"] == "legal-expert"


def test_get_expert_status_unknown_agent_404():
    hub = MagicMock()
    hub.agents = {}
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.get("/api/portal/agent/nonexistent/expert")
    assert resp.status_code == 404


# ── DELETE /agent/{id}/expert ──

def test_delete_expert_keeps_data_by_default():
    hub, agent = _mk_hub_with_agent(
        "ag-del", expert_specialty="legal",
        expert_template_version="1.0", expert_level="expert",
    )
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.delete("/api/portal/agent/ag-del/expert")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["was_cultivated"] is True
    assert body["cleared_fields"]["expert_specialty"] == "legal"
    assert body["cleared_fields"]["expert_level"] == "expert"
    assert body["data_removed"] is False
    assert body["data_path_kept"] is True
    # Agent fields actually cleared
    assert agent.expert_specialty == ""
    assert agent.expert_level == "novice"
    hub._save_agents.assert_called_once()


def test_delete_expert_with_keep_data_false(tmp_path, monkeypatch):
    """keep_data=false also removes ~/.tudou_claw/expert/<id>/."""
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    # Create the dir on disk
    expert_dir = tmp_path / "expert" / "ag-rmrf"
    expert_dir.mkdir(parents=True)
    (expert_dir / "config.json").write_text('{"agent_id":"ag-rmrf"}')
    (expert_dir / "marker.txt").write_text("data")

    hub, agent = _mk_hub_with_agent(
        "ag-rmrf", expert_specialty="legal",
    )
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.delete(
        "/api/portal/agent/ag-rmrf/expert?keep_data=false")
    assert resp.status_code == 200
    body = resp.json()
    assert body["was_cultivated"] is True
    assert body["data_removed"] is True
    assert not expert_dir.exists()  # data tree gone


def test_delete_expert_idempotent_on_uncultivated_agent():
    """Deleting on a普通 agent is a no-op."""
    hub, agent = _mk_hub_with_agent("ag-plain")  # specialty defaults to ""
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.delete("/api/portal/agent/ag-plain/expert")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["was_cultivated"] is False
    assert body["cleared_fields"] == {}
    # save not called since nothing changed
    hub._save_agents.assert_not_called()


def test_delete_expert_unknown_agent_404():
    hub = MagicMock()
    hub.agents = {}
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.delete("/api/portal/agent/nonexistent/expert")
    assert resp.status_code == 404


# ── Feature flag (TUDOU_EXPERT_DISABLED=1 returns 503) ──

def test_endpoints_503_when_module_disabled(monkeypatch):
    monkeypatch.setenv(_config.DISABLED_ENV_VAR, "1")
    hub, _ = _mk_hub_with_agent()
    app = _build_test_app(hub)
    client = TestClient(app)
    # All 4 V1 endpoints should return 503
    for path, method in [
        ("/api/portal/specialty-templates", "get"),
        ("/api/portal/specialty-templates/legal", "get"),
        ("/api/portal/agent/ag1/expert", "get"),
        ("/api/portal/agent/ag1/expert", "delete"),
    ]:
        resp = getattr(client, method)(path)
        assert resp.status_code == 503, f"{method.upper()} {path} expected 503, got {resp.status_code}"
        assert "disabled" in resp.json().get("detail", "").lower()
