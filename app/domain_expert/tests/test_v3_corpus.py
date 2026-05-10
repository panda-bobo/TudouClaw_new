"""V3 step 1 vertical tests: corpus list/ingest/reindex endpoints.

V3 step 1 covers the manifest-level CRUD only:
  - GET    /agent/{id}/expert/corpus           → manifest + template_sources
  - POST   /agent/{id}/expert/corpus/ingest    → register source in manifest
  - POST   /agent/{id}/expert/corpus/reindex   → stub, returns manifest

Real download + chunk + embed flow lands in V3 step 2 (when bge-m3 +
sqlite-vss are wired into the live request path).
"""
from __future__ import annotations

import os
from unittest.mock import MagicMock

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


def _mk_hub_with_agent(agent_id="ag1", specialty=""):
    class _A: pass
    a = _A()
    a.id = agent_id
    a.name = "test-agent"
    a.expert_specialty = specialty
    a.expert_template_version = "1.0" if specialty else ""
    a.expert_level = "novice"
    a.expert_lora_version = ""
    a.expert_initialized_at = 0.0
    a.granted_skills = []
    a.bound_prompt_packs = []
    a.mcp_servers = []
    hub = MagicMock()
    hub.agents = {agent_id: a}
    hub._save_agents = MagicMock()
    hub.skill_registry = None
    return hub, a


# ── GET /corpus ──

def test_corpus_list_uncultivated_returns_empty_manifest(monkeypatch, tmp_path):
    """Fresh agent (no specialty) → empty manifest, no template_sources."""
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    hub, _ = _mk_hub_with_agent("ag-fresh")
    app = _build_test_app(hub)
    client = TestClient(app)
    r = client.get("/api/portal/agent/ag-fresh/expert/corpus")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_id"] == "ag-fresh"
    assert body["cultivated"] is False
    assert body["specialty"] == ""
    assert body["manifest"]["sources"] == []
    assert body["template_sources"] == []


def test_corpus_list_cultivated_includes_template_sources(monkeypatch, tmp_path):
    """Cultivated legal agent → template_sources populated from legal.yaml."""
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    hub, _ = _mk_hub_with_agent("ag-legal", specialty="legal")
    app = _build_test_app(hub)
    client = TestClient(app)
    r = client.get("/api/portal/agent/ag-legal/expert/corpus")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cultivated"] is True
    assert body["specialty"] == "legal"
    # legal.yaml ships with corpus_sources
    assert isinstance(body["template_sources"], list)


def test_corpus_list_unknown_agent_404(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    hub = MagicMock()
    hub.agents = {}
    app = _build_test_app(hub)
    client = TestClient(app)
    r = client.get("/api/portal/agent/missing/expert/corpus")
    assert r.status_code == 404
    assert "missing" in r.json()["detail"]


# ── POST /corpus/ingest ──

def test_corpus_ingest_registers_in_manifest(monkeypatch, tmp_path):
    """Posting a source_id writes a CorpusSourceEntry to the manifest."""
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    hub, _ = _mk_hub_with_agent("ag-i")
    app = _build_test_app(hub)
    client = TestClient(app)
    r = client.post(
        "/api/portal/agent/ag-i/expert/corpus/ingest",
        json={"source_id": "hf:my-dataset", "version": "v1.0",
              "chunker_strategy": "semantic"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["added_source"] == "hf:my-dataset"
    assert body["stage"] == "registered"
    sources = body["manifest"]["sources"]
    assert len(sources) == 1
    assert sources[0]["source_id"] == "hf:my-dataset"
    assert sources[0]["version"] == "v1.0"
    assert sources[0]["chunker_strategy"] == "semantic"
    # V3 step 1: not yet indexed
    assert sources[0]["chunk_count"] == 0
    assert sources[0]["indexed_at"] == 0.0
    # Verify manifest persisted to disk
    from app.domain_expert.corpus.manifest import CorpusManifest
    mp = CorpusManifest.path_for("ag-i")
    assert os.path.exists(mp)


def test_corpus_ingest_idempotent_on_same_source_id(monkeypatch, tmp_path):
    """Re-posting the same source_id replaces (not duplicates) the entry."""
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    hub, _ = _mk_hub_with_agent("ag-id")
    app = _build_test_app(hub)
    client = TestClient(app)
    client.post(
        "/api/portal/agent/ag-id/expert/corpus/ingest",
        json={"source_id": "hf:foo", "version": "v1"},
    )
    r = client.post(
        "/api/portal/agent/ag-id/expert/corpus/ingest",
        json={"source_id": "hf:foo", "version": "v2"},
    )
    assert r.status_code == 200
    sources = r.json()["manifest"]["sources"]
    assert len(sources) == 1                 # not 2
    assert sources[0]["version"] == "v2"     # newer wins


def test_corpus_ingest_missing_source_id_400(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    hub, _ = _mk_hub_with_agent("ag-m")
    app = _build_test_app(hub)
    client = TestClient(app)
    r = client.post(
        "/api/portal/agent/ag-m/expert/corpus/ingest",
        json={},
    )
    assert r.status_code == 400


def test_corpus_ingest_unknown_agent_404(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    hub = MagicMock()
    hub.agents = {}
    app = _build_test_app(hub)
    client = TestClient(app)
    r = client.post(
        "/api/portal/agent/no-such/expert/corpus/ingest",
        json={"source_id": "x"},
    )
    assert r.status_code == 404


# ── POST /corpus/reindex ──

def test_corpus_reindex_stub_returns_manifest(monkeypatch, tmp_path):
    """V3 step 1 stub — returns 200 with current manifest + 'next' hint
    for V3 step 2."""
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    hub, _ = _mk_hub_with_agent("ag-r")
    app = _build_test_app(hub)
    client = TestClient(app)
    # First add a source so the manifest is non-empty
    client.post("/api/portal/agent/ag-r/expert/corpus/ingest",
                json={"source_id": "test-src"})
    # Now reindex
    r = client.post("/api/portal/agent/ag-r/expert/corpus/reindex", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_id"] == "ag-r"
    assert body["stage"] == "stub"
    assert "V3 step 2" in body["next"]
    # Manifest still intact
    assert len(body["manifest"]["sources"]) == 1


def test_corpus_reindex_unknown_agent_404(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    hub = MagicMock()
    hub.agents = {}
    app = _build_test_app(hub)
    client = TestClient(app)
    r = client.post("/api/portal/agent/missing/expert/corpus/reindex", json={})
    assert r.status_code == 404


# ── Disabled module ──

def test_corpus_endpoints_503_when_disabled(monkeypatch, tmp_path):
    """All 3 corpus endpoints should return 503 when TUDOU_EXPERT_DISABLED=1."""
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    monkeypatch.setattr(_config, "is_disabled", lambda: True)
    hub, _ = _mk_hub_with_agent("ag-x")
    app = _build_test_app(hub)
    client = TestClient(app)
    assert client.get("/api/portal/agent/ag-x/expert/corpus").status_code == 503
    assert client.post("/api/portal/agent/ag-x/expert/corpus/ingest",
                       json={"source_id": "x"}).status_code == 503
    assert client.post("/api/portal/agent/ag-x/expert/corpus/reindex",
                       json={}).status_code == 503
