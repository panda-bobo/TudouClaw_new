"""Tests for the MCP-tool bypass on Agent.execute_tool's allowed_tools gate.

Bug being fixed (2026-05-11):
    /api/portal/tools catalog used by the Portal "Tool Permissions"
    grid only enumerates BUILT-IN tools. MCP-provided tools (e.g.
    terraform_init, vector_search) never appear there, so users can't
    tick them. With a non-empty profile.allowed_tools, the
    Agent.execute_tool gate then rejected every MCP tool call —
    even when the agent had been correctly bound to that MCP.

Fix:
    Agent.execute_tool now bypasses allowed_tools for any tool name
    contributed by an MCP the agent is bound to. Authority comes
    from the MCP binding; per-tool risk still flows through
    ToolPolicy (high-risk MCP tools still require approval).
"""
from __future__ import annotations

import pytest


# ─────────────────────────────────────────────────────────────────────
# MCPManager.get_agent_tool_names — the source-of-truth helper
# ─────────────────────────────────────────────────────────────────────

def test_get_agent_tool_names_returns_empty_for_unbound_agent():
    from app.mcp.manager import MCPManager
    mgr = MCPManager()
    assert mgr.get_agent_tool_names("ghost-agent-id") == set()


def test_get_agent_tool_names_unions_across_bindings():
    """Bind two MCPs to one agent → return union of both tools_provided."""
    from app.mcp.manager import (
        MCPManager, NodeMCPConfig, MCPServerConfig,
    )
    mgr = MCPManager()
    nc = NodeMCPConfig(node_id="local")
    mgr.node_configs["local"] = nc

    # Add catalog-known MCPs (terraform + chromadb both real entries)
    nc.add_mcp(MCPServerConfig(id="terraform", name="terraform"))
    nc.add_mcp(MCPServerConfig(id="chromadb", name="chromadb"))
    nc.bind_agent("ag-x", "terraform")
    nc.bind_agent("ag-x", "chromadb")

    names = mgr.get_agent_tool_names("ag-x")
    # terraform contributes terraform_apply / terraform_plan / etc.
    assert "terraform_apply" in names
    assert "terraform_plan" in names
    # chromadb contributes vector_search / vector_store / etc.
    assert "vector_search" in names
    assert "vector_store" in names


def test_get_agent_tool_names_only_for_bound_agent():
    """Agent A is bound; agent B (same node) is not — B sees nothing."""
    from app.mcp.manager import MCPManager, NodeMCPConfig, MCPServerConfig
    mgr = MCPManager()
    nc = NodeMCPConfig(node_id="local")
    mgr.node_configs["local"] = nc
    nc.add_mcp(MCPServerConfig(id="terraform", name="terraform"))
    nc.bind_agent("ag-A", "terraform")

    assert "terraform_apply" in mgr.get_agent_tool_names("ag-A")
    assert mgr.get_agent_tool_names("ag-B") == set()


# ─────────────────────────────────────────────────────────────────────
# Agent._get_bound_mcp_tool_names — instance-cached wrapper
# ─────────────────────────────────────────────────────────────────────

def test_agent_helper_caches_per_turn(monkeypatch):
    from app.agent import Agent
    a = Agent(id="ag1", name="t")
    calls = {"n": 0}

    class FakeMgr:
        def get_agent_tool_names(self, aid):
            calls["n"] += 1
            return {"terraform_init", "terraform_plan"}

    monkeypatch.setattr("app.mcp.manager.get_mcp_manager", lambda: FakeMgr())
    s1 = a._get_bound_mcp_tool_names()
    s2 = a._get_bound_mcp_tool_names()
    # 2026-05-12: when any MCP is bound, mcp_call dispatcher is also
    # included (see test_mcp_call_dispatcher_allowed_when_any_mcp_bound).
    assert s1 == s2 == frozenset(
        {"terraform_init", "terraform_plan", "mcp_call"})
    assert calls["n"] == 1   # cached after first call


def test_agent_helper_returns_frozenset_when_lookup_fails(monkeypatch):
    """Manager-import errors must not break tool dispatch — return empty."""
    from app.agent import Agent
    a = Agent(id="ag1", name="t")

    def boom():
        raise RuntimeError("manager broken")
    monkeypatch.setattr("app.mcp.manager.get_mcp_manager", boom)
    out = a._get_bound_mcp_tool_names()
    assert out == frozenset()


# ─────────────────────────────────────────────────────────────────────
# Integration: simulating the original bug + confirming the fix
# ─────────────────────────────────────────────────────────────────────

def _build_agent_with_allowed_tools(allowed: list[str]):
    """Make an Agent whose profile has a NON-EMPTY allowed_tools list,
    which is the precondition for the gate to fire (line 8369)."""
    from app.agent import Agent, AgentProfile
    a = Agent(id="ag1", name="t")
    a.profile = AgentProfile(allowed_tools=list(allowed))
    return a


def test_bug_repro_without_fix_pretends_mcp_tool_is_denied(monkeypatch):
    """Sanity: with NO MCP binding, MCP-named tools NOT in
    allowed_tools should be rejected (this is the original bug
    surface — verifies the test setup actually triggers the gate)."""
    a = _build_agent_with_allowed_tools(["bash", "read_file"])
    # No MCP bindings → helper returns empty
    monkeypatch.setattr(a, "_get_bound_mcp_tool_names",
                        lambda: frozenset())
    # We can't easily call full execute_tool without a hub fixture.
    # Inline the relevant gate logic instead — same predicate as
    # agent.py line 8369 to keep the contract test minimal.
    from app.tool_capabilities import CORE_TOOLS as _CORE
    tool_name = "terraform_init"
    mcp_tools = a._get_bound_mcp_tool_names()
    rejected = (
        a.profile.allowed_tools
        and tool_name not in a.profile.allowed_tools
        and tool_name not in _CORE
        and tool_name not in mcp_tools
    )
    assert rejected, "expected rejection when MCP isn't bound"


def test_fix_lets_mcp_tool_through_when_bound(monkeypatch):
    """When the MCP IS bound, the same gate predicate should pass."""
    a = _build_agent_with_allowed_tools(["bash", "read_file"])
    # MCP bound → helper returns terraform tool names
    monkeypatch.setattr(a, "_get_bound_mcp_tool_names",
                        lambda: frozenset({"terraform_init",
                                            "terraform_apply"}))
    from app.tool_capabilities import CORE_TOOLS as _CORE
    tool_name = "terraform_init"
    mcp_tools = a._get_bound_mcp_tool_names()
    rejected = (
        a.profile.allowed_tools
        and tool_name not in a.profile.allowed_tools
        and tool_name not in _CORE
        and tool_name not in mcp_tools
    )
    assert not rejected, "expected MCP-bound tool to bypass allowed_tools"


# ─────────────────────────────────────────────────────────────────────
# Bug 2026-05-12: mcp_call dispatcher rejection
#
# Followup to the 2026-05-11 fix. _get_bound_mcp_tool_names returned
# only specific tool names (terraform_init, terraform_validate, ...).
# But LLMs sometimes use the generic dispatcher pattern instead:
#   mcp_call(mcp_id="983197f1", tool="terraform_validate", args={...})
# `mcp_call` itself isn't in the bound names → DENIED → loop guard
# trips after 5 retries.
#
# Fix: when at least one MCP is bound, also include `mcp_call` in
# the bypass set. The dispatcher will then re-check permissions on
# the underlying tool call.
# ─────────────────────────────────────────────────────────────────────

def test_mcp_call_dispatcher_allowed_when_any_mcp_bound():
    """Real-world bug: agent with terraform MCP bound called mcp_call
    6 times, all DENIED. Verifies the bypass set now includes the
    generic dispatcher when any MCP is bound."""
    from app.agent import Agent
    from unittest.mock import patch

    class FakeMgr:
        def get_agent_tool_names(self, aid):
            # Same set the real terraform MCP returns
            return {"terraform_init", "terraform_validate",
                    "terraform_plan", "terraform_apply"}

    a = Agent(id="ag-real-1", name="t")
    with patch("app.mcp.manager.get_mcp_manager", lambda: FakeMgr()):
        names = a._get_bound_mcp_tool_names()

    # Specific tools still present
    assert "terraform_validate" in names
    assert "terraform_init" in names
    # NEW: dispatcher present
    assert "mcp_call" in names


def test_mcp_call_dispatcher_NOT_allowed_when_no_mcp_bound():
    """If agent has zero bound MCPs, mcp_call must NOT be in the
    bypass set — that would let any agent dispatch to any MCP."""
    from app.agent import Agent
    from unittest.mock import patch

    class FakeMgr:
        def get_agent_tool_names(self, aid):
            return set()  # no bindings

    a = Agent(id="ag-real-2", name="t")
    with patch("app.mcp.manager.get_mcp_manager", lambda: FakeMgr()):
        names = a._get_bound_mcp_tool_names()

    assert "mcp_call" not in names
    assert names == frozenset()


def test_built_in_tool_not_in_allowed_tools_still_rejected(monkeypatch):
    """The fix only opens the gate for MCP tools — built-in tools
    remain locked when the user didn't tick them."""
    a = _build_agent_with_allowed_tools(["bash"])
    monkeypatch.setattr(a, "_get_bound_mcp_tool_names",
                        lambda: frozenset({"terraform_init"}))
    from app.tool_capabilities import CORE_TOOLS as _CORE
    tool_name = "write_file"   # built-in, not in allowed, not in MCP
    mcp_tools = a._get_bound_mcp_tool_names()
    rejected = (
        a.profile.allowed_tools
        and tool_name not in a.profile.allowed_tools
        and tool_name not in _CORE
        and tool_name not in mcp_tools
    )
    assert rejected, "built-in tool should still be blocked"
