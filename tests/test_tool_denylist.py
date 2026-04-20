"""Tests for the admin-editable global tool denylist.

Two bugs this tackles:

  1. **Revoke 不兜底**：内置 tool 的名字和 skill 撞车（比如
     ``create_pptx_advanced`` 同时是内置 tool 和 skill 名字），UI 上
     「撤销 skill」只清 ``agent.granted_skills``，**tool 还能被 LLM 调用**。
     denylist 是全局的底线：admin 可以直接把这类遗留 tool 全局拉黑。

  2. **审批持久化**：之前 session_approvals 存内存，进程重启就丢，
     用户看到的现象是 "每次进入都提示审批"。已经单独持久化到
     ``tool_approvals.json``（本测试顺带校验）。
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest


# ── ToolPolicy direct tests ───────────────────────────────────────────


def _fresh_policy(tmp_path):
    """Return a ToolPolicy bound to a tmpdir so load/save don't touch
    the real ~/.tudou_claw directory."""
    from app.auth import ToolPolicy
    p = ToolPolicy()
    persist = tmp_path / "tool_approvals.json"
    p.set_persist_path(str(persist))
    return p, tmp_path


def test_default_denylist_blocks_create_pptx_advanced(tmp_path):
    """Out of the box create_pptx_advanced is denied — covers the bug
    where users revoked the skill but the internal tool kept running."""
    p, _ = _fresh_policy(tmp_path)
    decision, reason = p.check_tool("create_pptx_advanced", {}, agent_id="a1")
    assert decision == "deny"
    assert "denylist" in reason.lower()


def test_admin_can_add_and_remove_denied_tool(tmp_path):
    p, _ = _fresh_policy(tmp_path)

    # Add an arbitrary tool, should now deny.
    assert p.add_global_denied_tool("legacy_web_search") is True
    assert "legacy_web_search" in p.list_global_denylist()
    decision, _ = p.check_tool("legacy_web_search", {})
    assert decision == "deny"

    # Remove it, should allow again (assuming it's low-risk).
    assert p.remove_global_denied_tool("legacy_web_search") is True
    assert "legacy_web_search" not in p.list_global_denylist()


def test_denylist_persists_across_reloads(tmp_path):
    """Save → re-load gives back the same set (no restart-forget bug)."""
    p1, _ = _fresh_policy(tmp_path)
    p1.add_global_denied_tool("weird_tool")
    assert "weird_tool" in p1.list_global_denylist()

    # Simulate process restart: create a fresh policy pointed at the same file.
    from app.auth import ToolPolicy
    p2 = ToolPolicy()
    p2.set_persist_path(str(tmp_path / "tool_approvals.json"))
    assert "weird_tool" in p2.list_global_denylist()
    # And still blocks it.
    decision, _ = p2.check_tool("weird_tool", {})
    assert decision == "deny"


def test_add_duplicate_returns_false(tmp_path):
    p, _ = _fresh_policy(tmp_path)
    # create_pptx_advanced is already in the factory default.
    assert p.add_global_denied_tool("create_pptx_advanced") is False


def test_remove_missing_returns_false(tmp_path):
    p, _ = _fresh_policy(tmp_path)
    assert p.remove_global_denied_tool("never_registered_tool") is False


def test_denylist_precedence_over_low_risk(tmp_path):
    """A tool classified as LOW risk still gets denied when on the list."""
    p, _ = _fresh_policy(tmp_path)
    # read_file is a classic LOW-risk tool; confirm the baseline.
    d0, _ = p.check_tool("read_file", {})
    assert d0 == "allow"

    p.add_global_denied_tool("read_file")
    d1, _ = p.check_tool("read_file", {})
    assert d1 == "deny"


# ── Session approvals persistence ─────────────────────────────────────


def test_session_approvals_persist(tmp_path):
    """Approve → restart → still approved (fixes 'please approve every time')."""
    from app.auth import ToolPolicy
    p1 = ToolPolicy()
    persist = str(tmp_path / "tool_approvals.json")
    p1.set_persist_path(persist)

    # Directly seed a session-scope approval (what ApprovalManager.approve does).
    p1.session_approvals.add(("agent_xyz", "weird_write_tool"))
    p1._save_session_approvals()

    # Re-instantiate (restart simulation).
    p2 = ToolPolicy()
    p2.set_persist_path(persist)
    assert ("agent_xyz", "weird_write_tool") in p2.session_approvals


# ── REST endpoint ─────────────────────────────────────────────────────


@pytest.fixture
def admin_client(monkeypatch, tmp_path):
    # Build a minimal app with admin router only.
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.routers import admin as admin_mod
    from app.api.deps.auth import get_current_user, CurrentUser

    # Point Auth at a tmp data dir so the denylist file is isolated.
    from app.auth import Auth, _AUTH_SINGLETON_KEY as _k
    import app.auth as _auth_mod
    auth = Auth(data_dir=str(tmp_path))
    # Reset factory-default denylist state to ensure clean tests.
    monkeypatch.setattr(_auth_mod, "_auth_singleton", auth, raising=False)
    monkeypatch.setattr(_auth_mod, "get_auth", lambda: auth)

    async def _admin():
        return CurrentUser(user_id="u1", role="superAdmin")

    app = FastAPI()
    app.dependency_overrides[get_current_user] = _admin
    app.include_router(admin_mod.router)

    with TestClient(app) as c:
        yield c, auth
