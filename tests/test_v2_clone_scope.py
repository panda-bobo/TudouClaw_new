"""Verify V1→V2 clone migrates ONLY the allowlisted fields.

Guardrail against future drift: if someone adds a new field to the
clone logic that leaks forbidden state, this test fails. Explicitly
asserts each field category (allowed / forbidden).
"""
from __future__ import annotations

import types

import pytest

from app.v2.agent.agent_v2 import AgentV2


class _FakeV1Agent:
    """Fake V1 Agent with a bunch of fields that MUST NOT be migrated."""
    id = "v1_abc"
    name = "Legacy Agent"
    role = "assistant"
    granted_skills = ["pptx-author", "web-search"]
    # Forbidden fields: if clone_from_v1 starts reading these, we fail.
    messages = [{"role": "user", "content": "old conversation"}]
    system_prompt = "You are a helpful assistant with baked personality."
    soul_md = "# Old personality"
    working_dir = "/tmp/v1-workspace"
    transcript = types.SimpleNamespace(entries=["a", "b"])
    events = ["old-event"]
    cost_tracker = types.SimpleNamespace(total=99.0)
    profile = types.SimpleNamespace(skills=["old"])
    priority_level = 1
    role_title = "CXO"
    channel_ids = ["chan-x"]
    authorized_workspaces = ["other-agent"]
    parent_id = "some-parent"
    project_id = "some-proj"
    extra_llms = [{"label": "special", "provider": "x", "model": "y"}]
    auto_route = {"enabled": True}
    multimodal_provider = "openai"
    multimodal_model = "gpt-4o"
    coding_provider = "deepseek"
    coding_model = "deepseek-coder"


class _FakeHub:
    node_id = "local"
    def get_agent(self, aid):
        return _FakeV1Agent() if aid == "v1_abc" else None


class _FakeStore:
    def __init__(self): self.saved = []
    def save_agent(self, agent): self.saved.append(agent)


def test_clone_migrates_only_allowed_fields(monkeypatch):
    """V1 → V2 clone must copy exactly name / role / granted_skills and
    the effective MCP list; nothing else."""
    # Stub MCP manager so the clone doesn't hit a real one.
    import app.mcp.manager as _mgr
    class _M:
        def get_agent_effective_mcps(self, node_id, agent_id):
            return [types.SimpleNamespace(id="mcp_x"),
                    types.SimpleNamespace(id="mcp_y")]
    monkeypatch.setattr(_mgr, "get_mcp_manager", lambda: _M())

    store = _FakeStore()
    v2 = AgentV2.clone_from_v1("v1_abc", hub=_FakeHub(), store=store)

    # Allowlist.
    assert v2.name == "Legacy Agent"
    assert v2.role == "assistant"
    assert v2.v1_agent_id == "v1_abc"
    assert v2.capabilities.skills == ["pptx-author", "web-search"]
    assert v2.capabilities.mcps == ["mcp_x", "mcp_y"]
    assert v2.capabilities.llm_tier == "default"   # default, not inherited
    assert v2.capabilities.denied_tools == []

    # Forbidden leakage checks. V2 dataclass doesn't have these fields,
    # so the test is inherently guarded by the type system — but we
    # assert explicitly so an earnest "enhancement" can't slip past.
    forbidden_attrs = [
        "messages", "system_prompt", "soul_md", "transcript",
        "events", "cost_tracker", "profile", "priority_level",
        "role_title", "channel_ids", "authorized_workspaces",
        "parent_id", "project_id", "extra_llms", "auto_route",
        "multimodal_provider", "multimodal_model",
        "coding_provider", "coding_model",
    ]
    for attr in forbidden_attrs:
        assert not hasattr(v2, attr), (
            f"clone leaked V1 field {attr!r} onto V2 agent"
        )

    # working_dir is renamed to working_directory in V2 and is always
    # assigned a FRESH path, NEVER V1's path.
    assert v2.working_directory != "/tmp/v1-workspace"

    # Persisted if store supplied.
    assert len(store.saved) == 1


def test_clone_missing_agent_raises(monkeypatch):
    with pytest.raises(KeyError):
        AgentV2.clone_from_v1("does_not_exist", hub=_FakeHub())


def test_clone_tolerates_missing_mcp_manager(monkeypatch):
    """If the V1 MCP manager isn't available for any reason, clone must
    still succeed — mcps falls back to []."""
    import app.mcp.manager as _mgr
    def _boom():
        raise RuntimeError("mcp manager unavailable")
    monkeypatch.setattr(_mgr, "get_mcp_manager", _boom)

    v2 = AgentV2.clone_from_v1("v1_abc", hub=_FakeHub())
    assert v2.capabilities.mcps == []
    assert v2.capabilities.skills == ["pptx-author", "web-search"]
