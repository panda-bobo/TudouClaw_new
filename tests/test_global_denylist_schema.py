"""Regression: global tool denylist must strip schemas (not just block exec).

Bug history: ~/.tudou_claw/tool_denylist.json listed create_pptx +
create_pptx_advanced, but the LLM still received both schemas — ~750
tokens wasted per globally-denied tool × however many the user denies.
Fix: _get_effective_tools() now also consults
AuthManager.tool_policy.global_denylist.

Also covers: AuthManager default data_dir resolves to ~/.tudou_claw,
not the source-tree app/ directory. Previously the default bit tests
+ any code path that called get_auth() before init_auth().
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest


@pytest.fixture
def isolated_tudou_home(tmp_path, monkeypatch):
    """Point TUDOU_CLAW_HOME at a tmp dir and reset the auth singleton."""
    monkeypatch.setenv("TUDOU_CLAW_HOME", str(tmp_path))
    # Reset the module-level _auth singleton so next get_auth() picks
    # up the new env var.
    from app import auth as _auth_mod
    monkeypatch.setattr(_auth_mod, "_auth", None)
    return tmp_path


# ── AuthManager default data_dir ─────────────────────────────────────

def test_auth_default_data_dir_uses_user_home(isolated_tudou_home):
    """Before fix: fell back to app/ (source tree). After: ~/.tudou_claw
    or $TUDOU_CLAW_HOME."""
    from app.auth import AuthManager
    auth = AuthManager()
    assert auth._data_dir == str(isolated_tudou_home)


def test_auth_explicit_data_dir_wins(tmp_path, monkeypatch):
    """Explicit arg must still override env."""
    monkeypatch.setenv("TUDOU_CLAW_HOME", str(tmp_path / "from_env"))
    from app.auth import AuthManager
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    auth = AuthManager(data_dir=str(explicit))
    assert auth._data_dir == str(explicit)


def test_tool_policy_global_denylist_file_points_to_data_dir(
    isolated_tudou_home,
):
    """Default AuthManager must place tool_denylist.json in the user
    home data dir, not in the source tree."""
    from app.auth import AuthManager
    auth = AuthManager()
    # ToolPolicy.set_persist_path(...tool_approvals.json) infers the
    # denylist path as its sibling.
    expected = str(isolated_tudou_home / "tool_denylist.json")
    assert auth.tool_policy._global_denylist_file == expected


# ── Global denylist strips schemas ───────────────────────────────────

def test_global_denylist_strips_tool_schemas(isolated_tudou_home):
    """Write a denylist with 2 tools, verify _get_effective_tools()
    drops them from the schema list."""
    # Pre-seed the denylist file BEFORE constructing AuthManager so
    # _load_global_denylist picks it up on set_persist_path.
    (isolated_tudou_home / "tool_denylist.json").write_text(
        json.dumps({"denied": ["create_pptx", "web_screenshot"]}),
        encoding="utf-8",
    )

    from app.agent import Agent, AgentProfile
    agent = Agent(
        id="test-agent",
        name="tester",
        role="general",
        profile=AgentProfile(),
    )
    effective = agent._get_effective_tools()
    names = {t["function"]["name"] for t in effective}

    assert "create_pptx" not in names
    assert "web_screenshot" not in names
    # Other tools still present.
    assert "read_file" in names
    assert "web_search" in names


def test_global_denylist_empty_leaves_all_tools(isolated_tudou_home):
    """Edge case: empty denylist file → no filtering."""
    (isolated_tudou_home / "tool_denylist.json").write_text(
        json.dumps({"denied": []}), encoding="utf-8",
    )

    from app.agent import Agent, AgentProfile
    agent = Agent(
        id="test-agent",
        name="tester",
        role="general",
        profile=AgentProfile(),
    )
    effective = agent._get_effective_tools()
    # All base tools present.
    names = {t["function"]["name"] for t in effective}
    assert "create_pptx" in names
    assert "read_file" in names


def test_per_agent_deny_and_global_deny_compose(isolated_tudou_home):
    """Per-agent denied_tools AND global denylist should both apply."""
    (isolated_tudou_home / "tool_denylist.json").write_text(
        json.dumps({"denied": ["create_pptx"]}), encoding="utf-8",
    )

    from app.agent import Agent, AgentProfile
    agent = Agent(
        id="test-agent",
        name="tester",
        role="general",
        profile=AgentProfile(denied_tools=["bash", "edit_file"]),
    )
    effective = agent._get_effective_tools()
    names = {t["function"]["name"] for t in effective}

    # Per-agent denies.
    assert "bash" not in names
    assert "edit_file" not in names
    # Global deny.
    assert "create_pptx" not in names
    # Untouched.
    assert "read_file" in names
