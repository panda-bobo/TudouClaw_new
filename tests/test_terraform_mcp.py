"""Tests for app.mcp.builtins.terraform — the terraform-CLI MCP server.

We don't drive the terraform binary here (it isn't a CI dependency).
Instead we monkey-patch the ``_run`` subprocess wrapper so we can
exercise the JSON-RPC dispatch layer + safety gates without needing
hashicorp/terraform installed.
"""
from __future__ import annotations

import json
import os

import pytest

from app.mcp.builtins import terraform as tf_mcp


# ─────────────────────────────────────────────────────────────────────
# Test fixtures
# ─────────────────────────────────────────────────────────────────────

@pytest.fixture
def fake_module(tmp_path):
    """A real on-disk dir we'll pretend is a terraform module."""
    d = tmp_path / "mod"
    d.mkdir()
    (d / "main.tf").write_text(
        'resource "null_resource" "x" {}', encoding="utf-8"
    )
    return str(d)


@pytest.fixture
def captured_runs(monkeypatch):
    """Capture all _run() invocations + stub their return values."""
    calls = []

    def fake_run(cmd, cwd, timeout=600, extra_env=None):
        calls.append({"cmd": cmd, "cwd": cwd, "timeout": timeout})
        # Default: success no-output; tool-specific tests can swap this
        return {"ok": True, "exit_code": 0, "stdout": "", "stderr": "",
                "duration_ms": 1, "command": " ".join(cmd)}

    monkeypatch.setattr(tf_mcp, "_run", fake_run)
    # Bypass real binary lookup
    monkeypatch.setattr(tf_mcp, "_tf_bin", lambda: "/fake/terraform")
    return calls


@pytest.fixture(autouse=True)
def stable_secret(monkeypatch):
    """Lock the approval-token HMAC secret so token verification is
    deterministic across tests (otherwise the per-process random
    fallback drifts and tests interfere)."""
    monkeypatch.setattr(tf_mcp, "_APPROVAL_SECRET",
                        "test-secret-do-not-use-in-prod")


# ─────────────────────────────────────────────────────────────────────
# working_dir validation
# ─────────────────────────────────────────────────────────────────────

def test_validate_rejects_missing_dir():
    out = tf_mcp.tool_init(working_dir="/definitely/not/a/real/path")
    assert out["ok"] is False
    assert "does not exist" in out["error"]


def test_validate_rejects_relative_path():
    out = tf_mcp.tool_init(working_dir="relative/path")
    assert out["ok"] is False
    assert "absolute" in out["error"]


def test_validate_rejects_empty_dir():
    out = tf_mcp.tool_init(working_dir="")
    assert out["ok"] is False
    assert "required" in out["error"]


def test_allow_dirs_whitelist_blocks_outsiders(fake_module, monkeypatch,
                                                tmp_path, captured_runs):
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("TF_ALLOW_DIRS", str(other))
    out = tf_mcp.tool_init(working_dir=fake_module)
    assert out["ok"] is False
    assert "TF_ALLOW_DIRS" in out["error"]


def test_allow_dirs_whitelist_admits_insiders(fake_module, monkeypatch,
                                                captured_runs):
    monkeypatch.setenv("TF_ALLOW_DIRS",
                       os.path.dirname(fake_module))
    out = tf_mcp.tool_init(working_dir=fake_module)
    assert out["ok"] is True


# ─────────────────────────────────────────────────────────────────────
# Approval token plumbing
# ─────────────────────────────────────────────────────────────────────

def test_apply_refuses_without_token(fake_module, captured_runs):
    out = tf_mcp.tool_apply(working_dir=fake_module,
                            plan_id="abc1234567",
                            approval_token="")
    assert out["ok"] is False
    assert "approval_token" in out["error"]
    # Crucially: we never invoked terraform
    assert not captured_runs


def test_apply_refuses_with_wrong_token(fake_module, captured_runs):
    out = tf_mcp.tool_apply(working_dir=fake_module,
                            plan_id="abc1234567",
                            approval_token="totally-fabricated")
    assert out["ok"] is False
    assert not captured_runs


def test_apply_refuses_when_plan_file_missing(fake_module, captured_runs):
    plan_id = "ghost12345"
    token = tf_mcp._approval_token(plan_id)
    out = tf_mcp.tool_apply(working_dir=fake_module,
                            plan_id=plan_id,
                            approval_token=token)
    assert out["ok"] is False
    assert "not found" in out["error"]


def test_apply_runs_when_token_valid_and_plan_present(fake_module,
                                                       captured_runs):
    plan_id = "real123abc"
    plan_path = os.path.join(fake_module, ".plans", f"{plan_id}.tfplan")
    os.makedirs(os.path.dirname(plan_path), exist_ok=True)
    open(plan_path, "w").close()
    token = tf_mcp._approval_token(plan_id)
    out = tf_mcp.tool_apply(working_dir=fake_module,
                            plan_id=plan_id,
                            approval_token=token)
    assert out["ok"] is True
    assert any("apply" in c["cmd"] for c in captured_runs)
    # Successful apply consumes the plan file
    assert not os.path.isfile(plan_path)


def test_plan_returns_id_and_token(fake_module, captured_runs, monkeypatch):
    # Override fake_run for this one to simulate a plan with changes
    def with_changes(cmd, cwd, timeout=600, extra_env=None):
        captured_runs.append({"cmd": cmd, "cwd": cwd, "timeout": timeout})
        return {"ok": False, "exit_code": 2, "stdout": "Plan: 3 to add",
                "stderr": "", "duration_ms": 50, "command": " ".join(cmd)}
    monkeypatch.setattr(tf_mcp, "_run", with_changes)

    out = tf_mcp.tool_plan(working_dir=fake_module)
    assert out["ok"] is True  # exit-code 2 → has_changes, surfaced as ok
    assert out["has_changes"] is True
    assert "plan_id" in out
    # Verify the token actually validates for the returned plan_id
    assert tf_mcp._verify_token(out["plan_id"],
                                out["approval_token_for_operator"])


def test_plan_no_changes_still_ok(fake_module, captured_runs, monkeypatch):
    def no_changes(cmd, cwd, timeout=600, extra_env=None):
        captured_runs.append({"cmd": cmd, "cwd": cwd, "timeout": timeout})
        return {"ok": True, "exit_code": 0,
                "stdout": "No changes. Your infrastructure matches.",
                "stderr": "", "duration_ms": 30, "command": " ".join(cmd)}
    monkeypatch.setattr(tf_mcp, "_run", no_changes)

    out = tf_mcp.tool_plan(working_dir=fake_module)
    assert out["ok"] is True
    assert out["has_changes"] is False
    assert "plan_id" in out


# ─────────────────────────────────────────────────────────────────────
# Destroy: belt-and-suspenders gating
# ─────────────────────────────────────────────────────────────────────

def test_destroy_refuses_without_confirm_phrase(fake_module, captured_runs):
    destroy_id = "destroy:" + os.path.realpath(fake_module)
    token = tf_mcp._approval_token(destroy_id)
    out = tf_mcp.tool_destroy(working_dir=fake_module,
                              approval_token=token,
                              confirm_phrase="")
    assert out["ok"] is False
    assert "confirm_phrase" in out["error"]
    assert not captured_runs


def test_destroy_refuses_wrong_confirm_phrase(fake_module, captured_runs):
    destroy_id = "destroy:" + os.path.realpath(fake_module)
    token = tf_mcp._approval_token(destroy_id)
    out = tf_mcp.tool_destroy(working_dir=fake_module,
                              approval_token=token,
                              confirm_phrase="please destroy")
    assert out["ok"] is False
    assert not captured_runs


def test_destroy_refuses_apply_token_reuse(fake_module, captured_runs):
    # An apply token is HMAC over the plan_id, NOT over the destroy_id.
    # This catches the case where an LLM scrapes a plan-approval token
    # from chat history and tries to use it for destroy.
    plan_id = "abc1234567"
    apply_token = tf_mcp._approval_token(plan_id)
    out = tf_mcp.tool_destroy(working_dir=fake_module,
                              approval_token=apply_token,
                              confirm_phrase=("destroy "
                                              + os.path.basename(fake_module)))
    assert out["ok"] is False
    assert "destroy" in out["error"].lower()
    assert not captured_runs


def test_destroy_runs_when_all_gates_pass(fake_module, captured_runs):
    destroy_id = "destroy:" + os.path.realpath(fake_module)
    token = tf_mcp._approval_token(destroy_id)
    out = tf_mcp.tool_destroy(
        working_dir=fake_module,
        approval_token=token,
        confirm_phrase="destroy " + os.path.basename(fake_module),
    )
    assert out["ok"] is True
    assert any("destroy" in c["cmd"] for c in captured_runs)


# ─────────────────────────────────────────────────────────────────────
# Read-only tools (sanity)
# ─────────────────────────────────────────────────────────────────────

def test_validate_invokes_terraform_validate(fake_module, captured_runs):
    out = tf_mcp.tool_validate(working_dir=fake_module)
    assert out["ok"] is True
    assert captured_runs[0]["cmd"][1:3] == ["validate", "-no-color"]


def test_fmt_check_does_not_pass_write_flag(fake_module, captured_runs):
    out = tf_mcp.tool_fmt(working_dir=fake_module, write=False)
    assert out["ok"] is True
    assert "-check" in captured_runs[0]["cmd"]


def test_fmt_write_omits_check_flag(fake_module, captured_runs):
    out = tf_mcp.tool_fmt(working_dir=fake_module, write=True)
    assert out["ok"] is True
    assert "-check" not in captured_runs[0]["cmd"]


def test_state_show_requires_address(fake_module, captured_runs):
    out = tf_mcp.tool_state_show(working_dir=fake_module, address="")
    assert out["ok"] is False
    assert "address" in out["error"]


# ─────────────────────────────────────────────────────────────────────
# JSON-RPC dispatch
# ─────────────────────────────────────────────────────────────────────

def test_jsonrpc_initialize():
    resp = tf_mcp._handle_request({
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {},
    })
    assert resp["result"]["serverInfo"]["name"] == "tudou-terraform"
    assert "tools" in resp["result"]["capabilities"]


def test_jsonrpc_tools_list_advertises_all_tools():
    resp = tf_mcp._handle_request({
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    })
    tools = {t["name"] for t in resp["result"]["tools"]}
    assert "terraform_init" in tools
    assert "terraform_plan" in tools
    assert "terraform_apply" in tools
    assert "terraform_destroy" in tools
    # Each tool has a schema
    for t in resp["result"]["tools"]:
        assert "inputSchema" in t
        assert t["inputSchema"]["type"] == "object"


def test_jsonrpc_tools_call_returns_text_content(fake_module, captured_runs):
    resp = tf_mcp._handle_request({
        "jsonrpc": "2.0", "id": 3, "method": "tools/call",
        "params": {
            "name": "terraform_validate",
            "arguments": {"working_dir": fake_module},
        },
    })
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["ok"] is True


def test_jsonrpc_unknown_tool_returns_error():
    resp = tf_mcp._handle_request({
        "jsonrpc": "2.0", "id": 4, "method": "tools/call",
        "params": {"name": "terraform_nuke_universe", "arguments": {}},
    })
    assert "error" in resp
    assert "Unknown tool" in resp["error"]["message"]


def test_jsonrpc_initialized_notification_returns_none():
    resp = tf_mcp._handle_request({
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })
    assert resp is None


def test_jsonrpc_unknown_method():
    resp = tf_mcp._handle_request({
        "jsonrpc": "2.0", "id": 5, "method": "wat", "params": {},
    })
    assert resp["error"]["code"] == -32601


# ─────────────────────────────────────────────────────────────────────
# Catalog entry
# ─────────────────────────────────────────────────────────────────────

def test_catalog_registers_terraform_capability():
    from app.mcp.manager import MCP_CATALOG
    assert "terraform" in MCP_CATALOG
    cap = MCP_CATALOG["terraform"]
    assert cap.transport == "stdio"
    assert "terraform_apply" in cap.tools_provided
    assert "terraform_destroy" in cap.tools_provided
    assert cap.scope == "node"
