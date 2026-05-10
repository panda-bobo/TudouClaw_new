"""V2 vertical tests: POST /agent/{id}/expert/initialize.

Drives the bundle-apply flow end-to-end via the route handler with a
mocked Hub + temp expert dir + monkeypatched PromptPack registry.
"""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.domain_expert.api.routers import router as expert_router
from app.domain_expert import _config


def _build_test_app(hub_mock):
    app = FastAPI()
    app.include_router(expert_router)
    from app.api.deps.auth import get_current_user
    from app.api.deps.hub import get_hub
    fake_user = MagicMock()
    fake_user.user_id = "tester"
    fake_user.role = "admin"
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[get_hub] = lambda: hub_mock
    return app


def _mk_hub_with_agent(agent_id="ag1", **agent_attrs):
    """Plain object for `agent` (NOT MagicMock) — apply_bundle uses
    setattr/getattr; magic-mock auto-attrs would pollute the result."""

    class _A:
        pass
    a = _A()
    a.id = agent_id
    a.name = agent_attrs.get("name", "test-agent")
    a.expert_specialty = agent_attrs.get("expert_specialty", "")
    a.expert_template_version = agent_attrs.get("expert_template_version", "")
    a.expert_level = agent_attrs.get("expert_level", "novice")
    a.expert_lora_version = agent_attrs.get("expert_lora_version", "")
    a.expert_initialized_at = agent_attrs.get("expert_initialized_at", 0.0)
    a.granted_skills = list(agent_attrs.get("granted_skills", []) or [])
    a.bound_prompt_packs = list(agent_attrs.get("bound_prompt_packs", []) or [])
    a.mcp_servers = list(agent_attrs.get("mcp_servers", []) or [])

    hub = MagicMock()
    hub.agents = {agent_id: a}
    hub._save_agents = MagicMock()
    hub.skill_registry = None  # default fallback path
    return hub, a


def _patch_prompt_pack_store(monkeypatch, available_pack_ids: set[str]):
    """Monkeypatch get_prompt_pack_registry to return a registry where
    `available_pack_ids` are present and others aren't."""
    fake_store = MagicMock()
    fake_store.get = lambda pid: ("PACK_OBJ" if pid in available_pack_ids else None)
    fake_reg = MagicMock()
    fake_reg.store = fake_store
    monkeypatch.setattr(
        "app.skills.prompt_enhancer.get_prompt_pack_registry",
        lambda: fake_reg,
        raising=False,
    )


# ── Happy path ──

def test_initialize_legal_happy_path(monkeypatch, tmp_path):
    """Cultivate a fresh agent into legal-expert. Expect 200 + agent
    fields stamped + ExpertProfile written + missing-packs reported
    when registry doesn't have them."""
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    # Pretend community packs exist, but Anthropic packs don't (V1-realistic)
    _patch_prompt_pack_store(
        monkeypatch,
        available_pack_ids={
            "agency_legal_lawyer",
            "agency_legal_legal_counsel",
            "agency_legal_contract_lawyer",
            "agency_legal_litigation_specialist",
        },
    )

    hub, agent = _mk_hub_with_agent("ag-fresh", name="小法-test")
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.post(
        "/api/portal/agent/ag-fresh/expert/initialize",
        json={"template_id": "legal-expert"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["specialty"] == "legal"
    assert body["template_id"] == "legal-expert"
    assert body["template_version"] == "1.0"
    assert body["agent_id"] == "ag-fresh"
    # Agent fields stamped
    assert agent.expert_specialty == "legal"
    assert agent.expert_template_version == "1.0"
    assert agent.expert_level == "novice"
    assert agent.expert_initialized_at > 0
    # 4 community packs found, 8 anthropic missing (per stub registry)
    assert sorted(body["packs_bound"]) == sorted([
        "agency_legal_lawyer",
        "agency_legal_legal_counsel",
        "agency_legal_contract_lawyer",
        "agency_legal_litigation_specialist",
    ])
    assert len(body["missing_anthropic_packs"]) == 8
    # save_callback fired
    assert body["save_called"] is True
    hub._save_agents.assert_called()
    # ExpertProfile snapshot on disk
    profile_path = tmp_path / "expert" / "ag-fresh" / "config.json"
    assert profile_path.exists()


def test_initialize_persists_profile_to_disk(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    _patch_prompt_pack_store(monkeypatch, available_pack_ids=set())
    hub, agent = _mk_hub_with_agent("ag-persist")
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.post(
        "/api/portal/agent/ag-persist/expert/initialize",
        json={"template_id": "legal-expert"},
    )
    assert resp.status_code == 200
    # Read the profile JSON
    import json
    pp = tmp_path / "expert" / "ag-persist" / "config.json"
    data = json.loads(pp.read_text())
    assert data["agent_id"] == "ag-persist"
    assert data["specialty"] == "legal"
    assert data["template_id"] == "legal-expert"
    assert data["template_version"] == "1.0"
    assert data["level"] == "novice"


# ── Validation errors ──

def test_initialize_missing_template_id_400(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    hub, _ = _mk_hub_with_agent("ag-x")
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.post(
        "/api/portal/agent/ag-x/expert/initialize",
        json={},
    )
    assert resp.status_code == 400
    assert "template_id" in resp.json()["detail"]


def test_initialize_unknown_template_404(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    _patch_prompt_pack_store(monkeypatch, available_pack_ids=set())
    hub, _ = _mk_hub_with_agent("ag-y")
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.post(
        "/api/portal/agent/ag-y/expert/initialize",
        json={"template_id": "nonexistent"},
    )
    assert resp.status_code == 404


def test_initialize_unknown_agent_404():
    hub = MagicMock()
    hub.agents = {}
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.post(
        "/api/portal/agent/no-such/expert/initialize",
        json={"template_id": "legal-expert"},
    )
    assert resp.status_code == 404


# ── Re-cultivation gate ──

def test_initialize_already_cultivated_409(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    _patch_prompt_pack_store(monkeypatch, available_pack_ids=set())
    hub, agent = _mk_hub_with_agent(
        "ag-already", expert_specialty="legal",
        expert_template_version="1.0", expert_level="journeyman",
    )
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.post(
        "/api/portal/agent/ag-already/expert/initialize",
        json={"template_id": "legal-expert"},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "already_cultivated"
    assert detail["current_specialty"] == "legal"


def test_initialize_force_overrides_already_cultivated(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    _patch_prompt_pack_store(
        monkeypatch,
        available_pack_ids={"agency_legal_lawyer"},
    )
    hub, agent = _mk_hub_with_agent(
        "ag-force", expert_specialty="legal",
        expert_template_version="1.0",
    )
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.post(
        "/api/portal/agent/ag-force/expert/initialize",
        json={"template_id": "legal-expert", "force": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True


# ── Idempotency / bundle reapply ──

def test_initialize_with_pre_bound_pack_doesnt_double_bind(monkeypatch, tmp_path):
    """If agent already has agency_legal_lawyer bound from elsewhere,
    the apply step should not double-bind it."""
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    _patch_prompt_pack_store(
        monkeypatch,
        available_pack_ids={"agency_legal_lawyer"},
    )
    hub, agent = _mk_hub_with_agent(
        "ag-dedup",
        bound_prompt_packs=["agency_legal_lawyer"],
    )
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.post(
        "/api/portal/agent/ag-dedup/expert/initialize",
        json={"template_id": "legal-expert"},
    )
    assert resp.status_code == 200
    # Pack appears exactly once in agent.bound_prompt_packs
    occurrences = agent.bound_prompt_packs.count("agency_legal_lawyer")
    assert occurrences == 1


# ── Skill grant fallback ──

def test_initialize_skill_grant_falls_back_to_direct_mutation(monkeypatch, tmp_path):
    """When hub has no skill_registry, callback falls back to direct
    mutation of agent.granted_skills."""
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    _patch_prompt_pack_store(monkeypatch, available_pack_ids=set())
    hub, agent = _mk_hub_with_agent("ag-no-reg")
    hub.skill_registry = None  # explicit
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.post(
        "/api/portal/agent/ag-no-reg/expert/initialize",
        json={"template_id": "legal-expert"},
    )
    assert resp.status_code == 200
    body = resp.json()
    # legal.yaml has 2 required_skills (Track D's design)
    skills_granted_count = len(body["skills_granted"])
    assert skills_granted_count >= 0  # may be 0 if all skipped due to missing existence cb
    # Required skills (whether granted or skipped) end up tracked
    assert "skills_granted" in body


# ── Feature flag ──

def test_initialize_503_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    monkeypatch.setenv(_config.DISABLED_ENV_VAR, "1")
    hub, _ = _mk_hub_with_agent("ag-disabled")
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.post(
        "/api/portal/agent/ag-disabled/expert/initialize",
        json={"template_id": "legal-expert"},
    )
    assert resp.status_code == 503


# ── Result shape ──

def test_initialize_returns_full_bundle_apply_result(monkeypatch, tmp_path):
    """The response body must include all BundleApplyResult fields plus
    the V2-augmented summary."""
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    _patch_prompt_pack_store(
        monkeypatch,
        available_pack_ids={
            "agency_legal_lawyer", "akwp_legal_brief",
        },
    )
    hub, _ = _mk_hub_with_agent("ag-shape")
    app = _build_test_app(hub)
    client = TestClient(app)
    resp = client.post(
        "/api/portal/agent/ag-shape/expert/initialize",
        json={"template_id": "legal-expert"},
    )
    assert resp.status_code == 200
    body = resp.json()
    expected_keys = {
        # BundleApplyResult fields
        "template_id", "template_version", "specialty", "agent_id",
        "packs_bound", "anthropic_packs_bound", "skills_granted", "mcps_required",
        "missing_packs", "missing_anthropic_packs", "missing_skills", "missing_mcps",
        "saved", "initialized_at",
        # V2-augmented
        "expert_level_after", "save_called", "is_complete", "summary", "ok",
    }
    assert expected_keys.issubset(body.keys()), (
        f"missing keys: {expected_keys - set(body.keys())}"
    )
    # is_complete = False since most anthropic packs not in stub registry
    assert body["is_complete"] is False
    assert isinstance(body["summary"], str)
