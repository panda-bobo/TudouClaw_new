"""V4 step 2 vertical tests: /expert/query + pipeline.answer.

V4 step 2 = RAG-augmented answer + organic trace capture. Tests cover:
  - pipeline.answer with corpus → retrieves chunks, builds aug prompt,
    calls LLM, writes trace to disk
  - pipeline.answer without corpus → fallback prompt, still writes trace
  - POST /expert/query happy path + edge cases
"""
from __future__ import annotations

import json
import os
import time
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


def _mk_hub_with_agent(agent_id="ag1", specialty="legal"):
    class _A: pass
    a = _A()
    a.id = agent_id
    a.name = "test-法师"
    a.expert_specialty = specialty
    a.expert_template_version = "1.0" if specialty else ""
    a.expert_level = "novice"
    a.expert_lora_version = ""
    a.expert_initialized_at = 0.0
    a.granted_skills = []
    a.bound_prompt_packs = []
    a.mcp_servers = []
    a.provider = "test-provider"
    a.model = "test-model"
    hub = MagicMock()
    hub.agents = {agent_id: a}
    hub._save_agents = MagicMock()
    hub.skill_registry = None
    return hub, a


def _seed_chunks(tmp_path, agent_id, source_id, chunk_texts):
    """Write chunks.jsonl for an agent."""
    edir = tmp_path / "expert" / agent_id
    src_dir = edir / "corpus" / source_id
    src_dir.mkdir(parents=True)
    with open(src_dir / "chunks.jsonl", "w", encoding="utf-8") as f:
        for text in chunk_texts:
            f.write(json.dumps({"text": text, "metadata": {}}, ensure_ascii=False) + "\n")


# ── pipeline.answer direct ──

def test_pipeline_answer_with_corpus_retrieves_and_writes_trace(monkeypatch, tmp_path):
    """answer() loads chunks, retrieves matching ones, calls LLM, traces."""
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    _seed_chunks(tmp_path, "ag-c", "civil-code", [
        "第一条 民事主体的人身权利受法律保护。",
        "第二条 物权法保护财产权。",
        "第三条 合同法调整合同关系。",
    ])
    captured_messages = []
    def fake_chat(messages, provider="", model=""):
        captured_messages.append(messages)
        return {"message": {"content": "(LLM 回答: 民事主体权利受保护)"}}
    monkeypatch.setattr("app.llm.chat_no_stream", fake_chat)

    _, agent = _mk_hub_with_agent("ag-c", "legal")
    from app.domain_expert.inference import pipeline
    out = pipeline.answer(agent, "民事主体的权利如何保护")

    # Sanity
    assert "LLM" in out
    # System prompt should include the retrieved chunks
    sys_msg = captured_messages[0][0]["content"]
    assert "民事主体" in sys_msg
    # R5: typed RAG renders chunks with [type · source_id] header.
    # Seeded chunks have no metadata.type, so they default to "reference".
    assert "[reference · civil-code]" in sys_msg
    # The block carries a typed section header
    assert "📚 参考资料" in sys_msg

    # Trace was written
    today = time.strftime("%Y-%m-%d")
    trace_path = tmp_path / "expert" / "ag-c" / "traces" / f"{today}.jsonl"
    assert trace_path.exists()
    records = [json.loads(l) for l in trace_path.read_text().splitlines() if l.strip()]
    assert len(records) == 1
    rec = records[0]
    assert rec["q"] == "民事主体的权利如何保护"
    assert rec["retrieved_count"] >= 1
    assert "civil-code" in rec["retrieved_sources"]
    assert rec["origin"] == "organic"
    assert rec["specialty"] == "legal"


def test_pipeline_answer_without_corpus_falls_back_with_warning(monkeypatch, tmp_path):
    """No corpus dir → fallback system prompt, LLM still called, trace written."""
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    captured = []
    def fake_chat(messages, provider="", model=""):
        captured.append(messages)
        return {"message": {"content": "(没有内部资料的回答)"}}
    monkeypatch.setattr("app.llm.chat_no_stream", fake_chat)

    _, agent = _mk_hub_with_agent("ag-empty", "legal")
    from app.domain_expert.inference import pipeline
    out = pipeline.answer(agent, "随便问")
    assert "没有内部资料" in out
    sys_msg = captured[0][0]["content"]
    assert "未检索到" in sys_msg or "未覆盖" in sys_msg

    today = time.strftime("%Y-%m-%d")
    trace_path = tmp_path / "expert" / "ag-empty" / "traces" / f"{today}.jsonl"
    assert trace_path.exists()
    rec = json.loads(trace_path.read_text().splitlines()[-1])
    assert rec["retrieved_count"] == 0


def test_pipeline_answer_handles_llm_failure_gracefully(monkeypatch, tmp_path):
    """LLM raising should produce a safe error string + still write trace."""
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    def fake_chat(messages, provider="", model=""):
        raise RuntimeError("provider unreachable")
    monkeypatch.setattr("app.llm.chat_no_stream", fake_chat)

    _, agent = _mk_hub_with_agent("ag-llmfail", "legal")
    from app.domain_expert.inference import pipeline
    out = pipeline.answer(agent, "test")
    assert "失败" in out and "provider unreachable" in out
    # Trace still written so the question isn't lost
    today = time.strftime("%Y-%m-%d")
    trace_path = tmp_path / "expert" / "ag-llmfail" / "traces" / f"{today}.jsonl"
    assert trace_path.exists()


# ── POST /expert/query ──

def test_query_happy_path(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    _seed_chunks(tmp_path, "ag-q1", "src1", ["示例资料 测试 内容"])
    monkeypatch.setattr("app.llm.chat_no_stream",
                        lambda messages, **kw: {"message": {"content": "示例回答"}})
    hub, _ = _mk_hub_with_agent("ag-q1", "legal")
    app = _build_test_app(hub)
    r = TestClient(app).post("/api/portal/agent/ag-q1/expert/query",
                              json={"q": "示例 测试"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["specialty"] == "legal"
    assert body["q"] == "示例 测试"
    assert body["answer"] == "示例回答"


def test_query_404_unknown_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    hub = MagicMock(); hub.agents = {}
    app = _build_test_app(hub)
    r = TestClient(app).post("/api/portal/agent/missing/expert/query",
                              json={"q": "x"})
    assert r.status_code == 404


def test_query_409_uncultivated(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    hub, _ = _mk_hub_with_agent("ag-u", "")  # not cultivated
    app = _build_test_app(hub)
    r = TestClient(app).post("/api/portal/agent/ag-u/expert/query",
                              json={"q": "x"})
    assert r.status_code == 409
    assert "not_cultivated" in str(r.json())


def test_query_400_missing_q(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    hub, _ = _mk_hub_with_agent("ag-mq", "legal")
    app = _build_test_app(hub)
    r = TestClient(app).post("/api/portal/agent/ag-mq/expert/query", json={})
    assert r.status_code == 400


def test_query_503_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    monkeypatch.setattr(_config, "is_disabled", lambda: True)
    hub, _ = _mk_hub_with_agent("ag-d", "legal")
    app = _build_test_app(hub)
    r = TestClient(app).post("/api/portal/agent/ag-d/expert/query",
                              json={"q": "x"})
    assert r.status_code == 503


def test_query_traces_appear_in_get_traces(monkeypatch, tmp_path):
    """A query should write a trace that GET /traces sees."""
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    monkeypatch.setattr("app.llm.chat_no_stream",
                        lambda messages, **kw: {"message": {"content": "ok"}})
    hub, _ = _mk_hub_with_agent("ag-t", "legal")
    client = TestClient(_build_test_app(hub))
    # Send 3 queries
    for q in ["问题一", "问题二", "问题三"]:
        client.post("/api/portal/agent/ag-t/expert/query", json={"q": q})
    # Read back via /traces
    r = client.get("/api/portal/agent/ag-t/expert/traces?limit=10")
    body = r.json()
    assert body["total"] == 3
    assert len(body["traces"]) == 3
    qs = sorted([t["q"] for t in body["traces"]])
    assert qs == ["问题一", "问题三", "问题二"]
