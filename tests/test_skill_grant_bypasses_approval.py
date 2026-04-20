"""Skill-grant shortcut: tools declared by a skill granted to an agent
must auto-allow (no admin approval required per-call)."""
from __future__ import annotations

import sys
import types

import pytest


def _fake_hub_with_skill(agent_id: str, skill_name: str, tools: list[str]):
    """Build an in-memory hub with one granted skill that whitelists ``tools``."""
    class FakeManifest:
        def __init__(self, name, tools):
            self.name = name
            self.tools = tools
    class FakeInstall:
        def __init__(self, manifest):
            self.manifest = manifest
    class FakeRegistry:
        def __init__(self):
            self._granted = {agent_id: [FakeInstall(FakeManifest(skill_name, tools))]}
        def list_for_agent(self, aid):
            return self._granted.get(aid, [])
    class FakeHub:
        skill_registry = FakeRegistry()
    return FakeHub()


def _install_fake_hub(hub):
    """Attach a fake hub so ``get_auth().tool_policy.check_tool`` sees it."""
    mod = sys.modules.setdefault("app.llm", types.ModuleType("app.llm"))
    mod._active_hub = hub
    return mod


def test_granted_skill_tool_auto_allows(monkeypatch):
    """A tool in a granted skill's whitelist returns ('allow', ...)."""
    from app.auth import ToolPolicy
    policy = ToolPolicy()
    # `get_skill_guide` is now low-risk so test with a MODERATE one.
    # `http_request` is moderate by default — needs approval unless skill-granted.
    hub = _fake_hub_with_skill(
        agent_id="agent_A",
        skill_name="my-skill",
        tools=["http_request"],
    )
    _install_fake_hub(hub)

    verdict, reason = policy.check_tool(
        tool_name="http_request", arguments={"url": "https://x"},
        agent_id="agent_A", agent_priority=3,
    )
    assert verdict == "allow", f"expected allow, got {verdict} ({reason})"
    assert "granted skill" in reason.lower()


def test_non_granted_agent_still_needs_approval(monkeypatch):
    """A different agent (no grant) doesn't get the shortcut."""
    from app.auth import ToolPolicy
    policy = ToolPolicy()
    hub = _fake_hub_with_skill(
        agent_id="agent_A",
        skill_name="my-skill",
        tools=["http_request"],
    )
    _install_fake_hub(hub)

    verdict, _ = policy.check_tool(
        tool_name="http_request", arguments={"url": "https://x"},
        agent_id="agent_B", agent_priority=3,  # different agent
    )
    # http_request is moderate → agent_approvable or needs_approval
    # depending on auto_approve_moderate default. Either way NOT
    # "allow" with our shortcut reason.
    assert verdict != "allow" or "granted skill" not in _


def test_skill_not_covering_tool_still_goes_through_risk(monkeypatch):
    """Skill whitelists only {bash}; unrelated tool is still checked."""
    from app.auth import ToolPolicy
    policy = ToolPolicy()
    hub = _fake_hub_with_skill(
        agent_id="agent_A",
        skill_name="my-skill",
        tools=["bash"],  # doesn't cover http_request
    )
    _install_fake_hub(hub)

    verdict, _ = policy.check_tool(
        tool_name="http_request", arguments={"url": "https://x"},
        agent_id="agent_A", agent_priority=3,
    )
    assert verdict != "allow" or "granted skill" not in _


def test_get_skill_guide_is_low_risk(monkeypatch):
    """get_skill_guide is low-risk globally — no approval ever needed."""
    from app.auth import ToolPolicy
    policy = ToolPolicy()
    verdict, _ = policy.check_tool(
        tool_name="get_skill_guide", arguments={"name": "pptx-author"},
        agent_id="agent_whoever", agent_priority=3,
    )
    assert verdict == "allow"
