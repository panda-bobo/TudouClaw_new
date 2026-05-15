"""Tests for app/runtime/* — the shared pure-function helpers.

These functions are called from BOTH:
  - app/agent.py (legacy chat loop, via thin pass-through aliases)
  - app/agent_runtime/ (future OpenAI Agents SDK adapter — not yet
    written, but will share the exact same intent/nudge/stream logic
    so per-agent behavior stays identical when admins toggle runtime
    mode)

So: lock the contract here. If a regression breaks behavior in this
file, BOTH runtimes regress.
"""
from __future__ import annotations

import json

import pytest

from app.runtime import (
    user_explicitly_requests_retrieval,
    user_explicitly_requests_wiki_write,
    user_asked_for_verification,
    agent_claimed_completion,
    agent_ran_verification_this_turn,
    detect_recent_tool_error,
    contains_tool_call_xml,
    looks_like_narrator_stall,
)


# ── intent: retrieval (knowledge_lookup / memory_recall opt-in) ──

@pytest.mark.parametrize("text,expected", [
    # Triggers
    ("查一下华为云 LTS 怎么配", True),
    ("搜一下 huaweicloud_kms_key 用法", True),
    ("找一下相关的方案", True),
    ("找一下相关资料", True),
    ("记得我之前说过", True),
    ("回忆一下我们上次的方案", True),
    ("知识库里有没有 SSC 案例", True),
    ("调用 knowledge_lookup 查一下", True),
    ("search the wiki for terraform examples", True),
    ("lookup the previous incident report", True),
    ("did we mention this earlier", True),
    # Non-triggers (action verbs)
    ("继续 terraform validate", False),
    ("修复 outputs.tf 第 17 行的错误", False),
    ("把 kms_key 改成 default 引用", False),
    ("做一下 security 模块的检测", False),
    ("写一份 landing zone 报告", False),
    ("fix the validate error", False),
    ("how is the task going", False),
    ("华为云 LTS 怎么配置", False),  # general "how to" — no explicit retrieval
    ("帮我看下进度", False),
    ("ok continue", False),
    ("好的", False),
    ("", False),
])
def test_user_explicitly_requests_retrieval(text, expected):
    assert user_explicitly_requests_retrieval(text) is expected


# ── intent: wiki_ingest opt-in ──

@pytest.mark.parametrize("text,expected", [
    ("把这条记下来", True),
    ("记一下这个", True),
    ("存进 wiki", True),
    ("写进知识库", True),
    ("总结成 wiki 条目", True),
    ("做一下复盘", True),
    ("写个 retro", True),
    ("整理成经验", True),
    ("save this to wiki", True),
    ("save it into wiki", True),
    ("add this to memory", True),
    ("write a retro for this incident", True),
    ("persist this in kb", True),
    ("remember this", True),
    # Non-triggers
    ("继续 terraform validate", False),
    ("修复 outputs.tf", False),
    ("查一下相关资料", False),  # retrieval, not write
    ("找一下方案", False),
    ("ok continue", False),
    ("", False),
])
def test_user_explicitly_requests_wiki_write(text, expected):
    assert user_explicitly_requests_wiki_write(text) is expected


# ── intent: verification request ──

@pytest.mark.parametrize("text,expected", [
    ("继续 terraform validate", True),
    ("跑通所有模块的 validate", True),
    ("验证一下 outputs.tf", True),
    ("修复完后跑测试", True),
    ("run npm test after fix", True),
    ("check if it works", True),
    # Non-triggers
    ("继续修", False),
    ("把 lts_enabled 改成 true", False),
    ("写一份报告", False),
    ("ok continue", False),
])
def test_user_asked_for_verification(text, expected):
    assert user_asked_for_verification(text) is expected


# ── nudges: completion claim detection ──

@pytest.mark.parametrize("text,expected", [
    ("修复完成。现在验证所有模块：", True),
    ("全部修好了", True),
    ("已完成所有修改", True),
    ("All fixed!", True),
    ("Done.", True),
    # Non-triggers
    ("我下一步会读取文件", False),
    ("还在分析中", False),
    ("有问题需要确认", False),
])
def test_agent_claimed_completion(text, expected):
    assert agent_claimed_completion(text) is expected


# ── nudges: did agent actually run verification this turn? ──

def _msg(role, content="", tool_calls=None):
    m = {"role": role, "content": content}
    if tool_calls is not None:
        m["tool_calls"] = tool_calls
    return m


def _bash_tc(cmd):
    return [{"id": "x", "type": "function", "function": {
        "name": "bash", "arguments": json.dumps({"command": cmd})}}]


def test_ran_verification_true_when_validate_in_bash():
    msgs = [
        _msg("user", "继续 terraform validate"),
        _msg("assistant", tool_calls=_bash_tc("cd /x && terraform validate")),
        _msg("tool", "Success"),
        _msg("assistant", "修复完成"),
    ]
    assert agent_ran_verification_this_turn(msgs) is True


def test_ran_verification_false_when_only_sed_runs():
    msgs = [
        _msg("user", "继续 terraform validate"),
        _msg("assistant", tool_calls=_bash_tc("sed -i 's/x/y/' main.tf")),
        _msg("tool", "ok"),
        _msg("assistant", "修复完成"),
    ]
    assert agent_ran_verification_this_turn(msgs) is False


def test_ran_verification_stops_at_turn_boundary():
    """Validation run in PREVIOUS turn should NOT count for current."""
    msgs = [
        _msg("user", "first task"),
        _msg("assistant", tool_calls=_bash_tc("terraform validate")),
        _msg("tool", "Success"),
        _msg("user", "second task — 验证"),  # boundary
        _msg("assistant", tool_calls=_bash_tc("sed -i ...")),
        _msg("tool", "ok"),
        _msg("assistant", "修复完成"),
    ]
    assert agent_ran_verification_this_turn(msgs) is False


# ── nudges: detect recent tool error ──

def test_detect_tool_error_terraform():
    msgs = [
        _msg("user", "validate"),
        _msg("tool", "│ Error: Unsupported argument\n│ on main.tf line 23"),
    ]
    out = detect_recent_tool_error(msgs)
    assert out is not None
    assert "Error" in out


def test_detect_tool_error_clean_tool_returns_none():
    msgs = [
        _msg("user", "version"),
        _msg("tool", "10.2.4"),
    ]
    assert detect_recent_tool_error(msgs) is None


def test_detect_tool_error_cross_turn_returns_none():
    msgs = [
        _msg("user", "task 1"),
        _msg("tool", "Error: nope"),
        _msg("assistant", "I see it"),
        _msg("user", "task 2"),
        _msg("assistant", "ok"),
    ]
    assert detect_recent_tool_error(msgs) is None


def test_detect_tool_error_permission_denied():
    msgs = [
        _msg("user", "run x"),
        _msg("tool", "bash: /tmp/x: Permission denied"),
    ]
    out = detect_recent_tool_error(msgs)
    assert out is not None and "Permission denied" in out


# ── stream_filters: XML tool_call leak detection ──

@pytest.mark.parametrize("tail,expected", [
    ("<tool_call>foo", True),
    ("blah blah <function=bash>", True),
    ("<tool_call>\n<function=read_file>", True),
    ("hello world", False),
    ("", False),
    ("the format is `<tool_call>` documented", True),  # honest false positive
])
def test_contains_tool_call_xml(tail, expected):
    assert contains_tool_call_xml(tail) is expected


# ── narrator: stall detection ──

@pytest.mark.parametrize("text,expected", [
    ("Let me check the file:", True),
    ("让我看一下：", True),
    ("我会读取这个文件：", True),
    ("Now let me see what's in it:", True),
    # Non-triggers (no commitment colon)
    ("Let me check the file.", False),  # period not colon
    ("修复完成", False),
    ("", False),
    ("Here is the answer", False),
])
def test_looks_like_narrator_stall(text, expected):
    assert looks_like_narrator_stall(text) is expected
