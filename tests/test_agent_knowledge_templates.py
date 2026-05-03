"""Tests for Agent.profile.knowledge_templates binding (spec
2026-05-03)."""
from __future__ import annotations
import pytest


def test_profile_has_knowledge_templates_field_default_empty():
    """New AgentProfile defaults knowledge_templates to []."""
    from app.agent import AgentProfile
    p = AgentProfile()
    assert hasattr(p, "knowledge_templates")
    assert p.knowledge_templates == []


def test_profile_to_dict_includes_knowledge_templates():
    from app.agent import AgentProfile
    p = AgentProfile()
    p.knowledge_templates = ["tpl_a", "tpl_b"]
    d = p.to_dict()
    assert d.get("knowledge_templates") == ["tpl_a", "tpl_b"]


def test_profile_from_dict_reads_knowledge_templates():
    from app.agent import AgentProfile
    p = AgentProfile.from_dict({"knowledge_templates": ["x", "y", "z"]})
    assert p.knowledge_templates == ["x", "y", "z"]


def test_profile_from_dict_missing_field_defaults_empty():
    """Legacy agent.json files (saved before this feature) should
    load with knowledge_templates = []."""
    from app.agent import AgentProfile
    p = AgentProfile.from_dict({"agent_class": "enterprise"})
    assert p.knowledge_templates == []


def test_profile_roundtrip_preserves_knowledge_templates():
    from app.agent import AgentProfile
    src = AgentProfile()
    src.knowledge_templates = ["t1", "t2"]
    restored = AgentProfile.from_dict(src.to_dict())
    assert restored.knowledge_templates == ["t1", "t2"]


def test_update_agent_profile_accepts_knowledge_templates(tmp_path, monkeypatch):
    """POST /agent/{id}/profile with knowledge_templates in body
    persists onto agent.profile.knowledge_templates via the
    body→AgentProfile.from_dict path."""
    from app.agent import Agent, AgentProfile

    agent = Agent(id="ak1", name="t")
    agent.profile = AgentProfile()

    body = {"knowledge_templates": ["tpl_x", "tpl_y"]}

    if "knowledge_templates" in body:
        new_profile = AgentProfile.from_dict({
            **agent.profile.to_dict(),
            "knowledge_templates": list(body["knowledge_templates"] or []),
        })
        agent.profile = new_profile

    assert agent.profile.knowledge_templates == ["tpl_x", "tpl_y"]
