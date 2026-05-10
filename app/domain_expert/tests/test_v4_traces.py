"""V4 step 1 vertical tests: traces / feedback / stats endpoints.

V4 step 1 covers read-only views + feedback ingest:
  - GET    /agent/{id}/expert/traces       → paginated trace list
  - POST   /agent/{id}/expert/feedback     → append 👍/👎 to feedback log
  - GET    /agent/{id}/expert/stats        → aggregated pipeline stats

V4 step 2 will add the actual /expert/query handler (RAG-augmented
inference + organic trace capture). For now /query stays 501.
"""
from __future__ import annotations

import json
import os
import time
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


def _mk_hub_with_agent(agent_id="ag1", specialty="legal", level="novice",
                      lora_version=""):
    class _A: pass
    a = _A()
    a.id = agent_id
    a.name = "test-agent"
    a.expert_specialty = specialty
    a.expert_template_version = "1.0" if specialty else ""
    a.expert_level = level
    a.expert_lora_version = lora_version
    a.expert_initialized_at = 0.0
    a.granted_skills = []
    a.bound_prompt_packs = []
    a.mcp_servers = []
    hub = MagicMock()
    hub.agents = {agent_id: a}
    hub._save_agents = MagicMock()
    hub.skill_registry = None
    return hub, a


# ── GET /traces ──

def test_traces_empty_when_no_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    hub, _ = _mk_hub_with_agent("ag-empty")
    app = _build_test_app(hub)
    r = TestClient(app).get("/api/portal/agent/ag-empty/expert/traces")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_id"] == "ag-empty"
    assert body["total"] == 0
    assert body["traces"] == []


def test_traces_reads_jsonl_and_sorts_recent_first(monkeypatch, tmp_path):
    """Two trace files with 5 entries each → return 10 total, sorted
    newest first."""
    edir = tmp_path / "expert" / "ag-tt"
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    traces_dir = edir / "traces"
    traces_dir.mkdir(parents=True)
    for fname, base_ts in [("a.jsonl", 1000), ("b.jsonl", 2000)]:
        with open(traces_dir / fname, "w", encoding="utf-8") as f:
            for i in range(5):
                f.write(json.dumps({"q": f"q-{fname}-{i}", "a": "...", "ts": base_ts + i}) + "\n")
    hub, _ = _mk_hub_with_agent("ag-tt")
    app = _build_test_app(hub)
    r = TestClient(app).get("/api/portal/agent/ag-tt/expert/traces")
    body = r.json()
    assert body["total"] == 10
    assert len(body["traces"]) == 10
    # Newest first — first trace should be from b.jsonl ts=2004
    assert body["traces"][0]["ts"] == 2004
    assert body["traces"][-1]["ts"] == 1000


def test_traces_limit_param_truncates(monkeypatch, tmp_path):
    edir = tmp_path / "expert" / "ag-l"
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    traces_dir = edir / "traces"
    traces_dir.mkdir(parents=True)
    with open(traces_dir / "x.jsonl", "w", encoding="utf-8") as f:
        for i in range(20):
            f.write(json.dumps({"q": f"q{i}", "ts": i}) + "\n")
    hub, _ = _mk_hub_with_agent("ag-l")
    app = _build_test_app(hub)
    r = TestClient(app).get("/api/portal/agent/ag-l/expert/traces?limit=5")
    body = r.json()
    assert body["total"] == 20
    assert len(body["traces"]) == 5


def test_traces_skips_corrupt_lines(monkeypatch, tmp_path):
    """Garbage lines in a JSONL are skipped, not 500."""
    edir = tmp_path / "expert" / "ag-c"
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    traces_dir = edir / "traces"
    traces_dir.mkdir(parents=True)
    with open(traces_dir / "y.jsonl", "w", encoding="utf-8") as f:
        f.write(json.dumps({"q": "good", "ts": 1}) + "\n")
        f.write("not valid json{{{}}\n")
        f.write(json.dumps({"q": "good2", "ts": 2}) + "\n")
    hub, _ = _mk_hub_with_agent("ag-c")
    app = _build_test_app(hub)
    r = TestClient(app).get("/api/portal/agent/ag-c/expert/traces")
    body = r.json()
    assert body["total"] == 3            # all 3 lines counted
    assert len(body["traces"]) == 2      # only 2 parseable


def test_traces_unknown_agent_404(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    hub = MagicMock(); hub.agents = {}
    app = _build_test_app(hub)
    assert TestClient(app).get("/api/portal/agent/missing/expert/traces").status_code == 404


# ── POST /feedback ──

def test_feedback_writes_jsonl_and_normalizes_rating(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    hub, _ = _mk_hub_with_agent("ag-fb")
    app = _build_test_app(hub)
    client = TestClient(app)
    # Various forms of "thumbs up"
    for rating in ["thumbs_up", "up", "👍"]:
        r = client.post("/api/portal/agent/ag-fb/expert/feedback",
                        json={"trace_id": f"t-{rating}", "rating": rating})
        assert r.status_code == 200, r.text
        assert r.json()["feedback"]["rating"] == "up"
    # And down
    r = client.post("/api/portal/agent/ag-fb/expert/feedback",
                    json={"trace_id": "td", "rating": "thumbs_down"})
    assert r.json()["feedback"]["rating"] == "down"
    # File should have 4 lines
    fp = tmp_path / "expert" / "ag-fb" / "feedback" / "feedback.jsonl"
    assert fp.exists()
    assert len(fp.read_text().strip().split("\n")) == 4


def test_feedback_invalid_rating_400(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    hub, _ = _mk_hub_with_agent("ag-i")
    app = _build_test_app(hub)
    r = TestClient(app).post("/api/portal/agent/ag-i/expert/feedback",
                             json={"trace_id": "t", "rating": "maybe"})
    assert r.status_code == 400


def test_feedback_unknown_agent_404(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    hub = MagicMock(); hub.agents = {}
    app = _build_test_app(hub)
    r = TestClient(app).post("/api/portal/agent/x/expert/feedback",
                             json={"trace_id": "t", "rating": "up"})
    assert r.status_code == 404


# ── GET /stats ──

def test_stats_uncultivated_returns_zeros(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    hub, _ = _mk_hub_with_agent("ag-z", specialty="")
    app = _build_test_app(hub)
    r = TestClient(app).get("/api/portal/agent/ag-z/expert/stats")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["cultivated"] is False
    assert body["specialty"] == ""
    assert body["trace_count"] == 0
    assert body["feedback_counts"] == {"up": 0, "down": 0}
    assert body["lora_versions"] == []
    assert body["active_lora"] == ""


def test_stats_aggregates_across_disk(monkeypatch, tmp_path):
    """Setup an expert dir with traces + feedback + lora versions, verify
    the stats endpoint reads each correctly."""
    edir = tmp_path / "expert" / "ag-s"
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    traces_dir = edir / "traces"; traces_dir.mkdir(parents=True)
    fb_dir = edir / "feedback"; fb_dir.mkdir()
    lora_dir = edir / "lora"; lora_dir.mkdir()
    # 7 trace entries
    with open(traces_dir / "t.jsonl", "w") as f:
        for i in range(7):
            f.write(json.dumps({"q": f"q{i}"}) + "\n")
    # 3 ups + 2 downs
    with open(fb_dir / "feedback.jsonl", "w") as f:
        for r in ["up", "up", "up", "down", "down"]:
            f.write(json.dumps({"trace_id": "x", "rating": r}) + "\n")
    # 2 LoRA versions
    (lora_dir / "v1").mkdir()
    (lora_dir / "v2").mkdir()
    (lora_dir / "current").mkdir()  # symlink pointer dir, excluded

    hub, _ = _mk_hub_with_agent("ag-s", specialty="legal", level="journeyman",
                                lora_version="v2")
    app = _build_test_app(hub)
    r = TestClient(app).get("/api/portal/agent/ag-s/expert/stats")
    body = r.json()
    assert body["cultivated"] is True
    assert body["specialty"] == "legal"
    assert body["level"] == "journeyman"
    assert body["template_version"] == "1.0"
    assert body["active_lora"] == "v2"
    assert body["trace_count"] == 7
    assert body["feedback_counts"] == {"up": 3, "down": 2}
    assert sorted(body["lora_versions"]) == ["v1", "v2"]


def test_stats_unknown_agent_404(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    hub = MagicMock(); hub.agents = {}
    app = _build_test_app(hub)
    assert TestClient(app).get("/api/portal/agent/x/expert/stats").status_code == 404


# ── Disabled module ──

def test_v4_endpoints_503_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    monkeypatch.setattr(_config, "is_disabled", lambda: True)
    hub, _ = _mk_hub_with_agent("ag-d")
    app = _build_test_app(hub)
    client = TestClient(app)
    assert client.get("/api/portal/agent/ag-d/expert/traces").status_code == 503
    assert client.get("/api/portal/agent/ag-d/expert/stats").status_code == 503
    assert client.post("/api/portal/agent/ag-d/expert/feedback",
                       json={"trace_id": "t", "rating": "up"}).status_code == 503
