"""Tests for app.session_log.SessionMarkdownLog — human-readable
per-agent Markdown transcript at
~/.tudou_claw/workspaces/<id>/sessions/YYYY-MM-DD.md.

Distinct from agent.json (LLM-facing). Append-only, time-stamped,
sequence-numbered. Tests cover format, sequence persistence across
init, session-break marker, body truncation, threading, fault
tolerance.
"""
from __future__ import annotations

import os
import re
import threading
import time
from datetime import datetime

import pytest

from app.session_log import SessionMarkdownLog, _BODY_CAP, _HEADER_RE


# ── Fixtures ──────────────────────────────────────────────────────────

def _today_path(log: SessionMarkdownLog) -> str:
    return os.path.join(log._dir, datetime.now().strftime("%Y-%m-%d") + ".md")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ── Format tests ──────────────────────────────────────────────────────

def test_user_message_format(tmp_path):
    log = SessionMarkdownLog("a1", "agent_x", str(tmp_path))
    log.append_message("user", "hello world")
    body = _read(_today_path(log))
    # Headers: ## [HH:MM:SS] #1 🧑 user
    assert re.search(r'## \[\d+:\d+:\d+\] #1 🧑 user', body)
    assert "hello world" in body


def test_assistant_message_uses_agent_name(tmp_path):
    log = SessionMarkdownLog("a1", "刘老师", str(tmp_path))
    log.append_message("assistant", "你好")
    body = _read(_today_path(log))
    assert "🤖 刘老师" in body
    assert "你好" in body


def test_tool_message_wraps_in_code_fence(tmp_path):
    log = SessionMarkdownLog("a1", "x", str(tmp_path))
    log.append_message("tool", "ok\n/tmp/foo", tool_name="Bash")
    body = _read(_today_path(log))
    assert "🔧 tool — `Bash`" in body
    assert "```\nok\n/tmp/foo\n```" in body


def test_tool_call_separate_kind(tmp_path):
    log = SessionMarkdownLog("a1", "x", str(tmp_path))
    log.append_tool_call("Read", {"file_path": "/etc/hosts"})
    body = _read(_today_path(log))
    assert "📞 tool call — `Read`" in body
    assert "```json" in body
    assert "/etc/hosts" in body


def test_unknown_role_uses_dot_emoji(tmp_path):
    log = SessionMarkdownLog("a1", "x", str(tmp_path))
    log.append_message("ghost", "boo")
    body = _read(_today_path(log))
    assert "• ghost" in body


# ── Sequence numbers ──────────────────────────────────────────────────

def test_sequence_increments(tmp_path):
    log = SessionMarkdownLog("a1", "x", str(tmp_path))
    for i in range(5):
        log.append_message("user", f"m{i}")
    body = _read(_today_path(log))
    nums = sorted(int(m.group(1)) for m in _HEADER_RE.finditer(body))
    assert nums == [1, 2, 3, 4, 5]


def test_sequence_persists_across_log_instance_recreation(tmp_path):
    """Restart simulation: new SessionMarkdownLog instance must pick up
    the counter where the previous run left off."""
    log1 = SessionMarkdownLog("a1", "x", str(tmp_path))
    for i in range(3):
        log1.append_message("user", f"m{i}")
    # Process "restart" — new instance from scratch
    log2 = SessionMarkdownLog("a1", "x", str(tmp_path))
    log2.append_message("user", "after restart")
    body = _read(_today_path(log2))
    nums = sorted(int(m.group(1)) for m in _HEADER_RE.finditer(body))
    # Should be 1,2,3,4 — no overlap, no skip
    assert nums == [1, 2, 3, 4]


def test_sequence_recovers_from_yesterday_too(tmp_path):
    """Cross-midnight: instantiation looks at the most-recent prior
    file too, so #N continues across day boundaries."""
    sessions_dir = os.path.join(str(tmp_path), "workspaces", "a1", "sessions")
    os.makedirs(sessions_dir, exist_ok=True)
    # Pre-seed yesterday's file with seq #42
    with open(os.path.join(sessions_dir, "2026-05-11.md"), "w") as f:
        f.write("\n## [22:00:00] #42 🧑 user\n\nmessage\n")
    log = SessionMarkdownLog("a1", "x", str(tmp_path))
    log.append_message("user", "today's first")
    body = _read(_today_path(log))
    # First message TODAY should be #43
    assert "#43" in body


# ── Body truncation ──────────────────────────────────────────────────

def test_body_truncated_at_cap(tmp_path):
    log = SessionMarkdownLog("a1", "x", str(tmp_path))
    big = "X" * (_BODY_CAP + 1000)
    log.append_message("user", big)
    body = _read(_today_path(log))
    assert "[truncated " in body
    # Both ends preserved
    assert body.count("X") < _BODY_CAP + 100  # not the full original


def test_body_under_cap_unchanged(tmp_path):
    log = SessionMarkdownLog("a1", "x", str(tmp_path))
    msg = "short message"
    log.append_message("user", msg)
    body = _read(_today_path(log))
    assert msg in body
    assert "[truncated" not in body


# ── Session-break marker ─────────────────────────────────────────────

def test_no_break_marker_on_first_user_message(tmp_path):
    log = SessionMarkdownLog("a1", "x", str(tmp_path))
    log.append_message("user", "first")
    body = _read(_today_path(log))
    assert "Session resumed" not in body


def test_session_break_marker_after_long_idle(tmp_path):
    log = SessionMarkdownLog("a1", "x", str(tmp_path),
                             session_break_minutes=0)  # any gap triggers
    log.append_message("user", "first")
    # Hand-rewind the timestamp so the next message looks idle
    log._last_user_at = time.time() - 60
    log.append_message("user", "second")
    body = _read(_today_path(log))
    assert "Session resumed after" in body
    assert "---" in body


def test_session_break_only_for_user_role(tmp_path):
    """Long idle then assistant message: no break marker (assistant
    messages don't open new sessions, only user messages do)."""
    log = SessionMarkdownLog("a1", "x", str(tmp_path),
                             session_break_minutes=0)
    log.append_message("user", "first")
    log._last_user_at = time.time() - 60
    log.append_message("assistant", "still part of the same turn")
    body = _read(_today_path(log))
    assert "Session resumed" not in body


# ── Manual session marker ────────────────────────────────────────────

def test_session_marker(tmp_path):
    log = SessionMarkdownLog("a1", "x", str(tmp_path))
    log.session_marker("Agent loaded — process restart")
    body = _read(_today_path(log))
    assert "---" in body
    assert "Agent loaded — process restart" in body


# ── Edge cases ───────────────────────────────────────────────────────

def test_empty_message_skipped(tmp_path):
    log = SessionMarkdownLog("a1", "x", str(tmp_path))
    log.append_message("user", "")
    log.append_message("user", None)
    # Tool with empty content is allowed (some tools return ""), but
    # user/assistant empties get skipped.
    today = _today_path(log)
    if os.path.exists(today):
        body = _read(today)
        assert "user" not in body or "#" not in body
    # Sequence counter shouldn't have advanced
    assert log._seq == 1


def test_empty_tool_name_skipped_for_tool_call(tmp_path):
    log = SessionMarkdownLog("a1", "x", str(tmp_path))
    log.append_tool_call("", {"x": 1})
    today = _today_path(log)
    assert not os.path.exists(today) or "tool call" not in _read(today)


def test_disk_failure_does_not_raise(tmp_path):
    """Read-only filesystem simulation: writes silently fail, never
    propagate. Chat loop must keep running."""
    log = SessionMarkdownLog("a1", "x", str(tmp_path))
    # Replace write path with a directory (unwritable)
    bad_dir = os.path.join(str(tmp_path), "fake_dir")
    os.makedirs(bad_dir, exist_ok=True)
    log._path_for_today = lambda: bad_dir   # writing to a dir fails
    # Must not raise
    log.append_message("user", "this write will fail silently")


def test_thread_safety(tmp_path):
    """Multi-threaded concurrent appends — no interleaved garbage in
    the file."""
    log = SessionMarkdownLog("a1", "x", str(tmp_path))
    N = 20

    def worker(i):
        log.append_message("user", f"thread-{i}-msg")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for t in threads: t.start()
    for t in threads: t.join()

    body = _read(_today_path(log))
    # Each message should appear intact (no interleaved chars)
    for i in range(N):
        assert f"thread-{i}-msg" in body
    # Seq numbers should be 1..N (some order — threads race)
    nums = sorted(int(m.group(1)) for m in _HEADER_RE.finditer(body))
    assert nums == list(range(1, N + 1))


# ── Anthropic-style list content ─────────────────────────────────────

def test_list_content_serialised_via_str(tmp_path):
    """Multi-part Anthropic content (list of dicts) shouldn't crash —
    the agent's _log forwarder flattens it before passing to us, but
    we're defensive too."""
    log = SessionMarkdownLog("a1", "x", str(tmp_path))
    log.append_message("user", str([{"type": "text", "text": "hi"}]))
    body = _read(_today_path(log))
    assert "hi" in body


# ── Path layout ───────────────────────────────────────────────────────

def test_layout_per_agent_per_day(tmp_path):
    log = SessionMarkdownLog("agent-abc-123", "x", str(tmp_path))
    log.append_message("user", "hi")
    expected = os.path.join(
        str(tmp_path), "workspaces", "agent-abc-123",
        "sessions", datetime.now().strftime("%Y-%m-%d") + ".md")
    assert os.path.exists(expected)


def test_agent_name_falls_back_when_empty(tmp_path):
    log = SessionMarkdownLog("a1", "", str(tmp_path))
    log.append_message("assistant", "hello")
    body = _read(_today_path(log))
    assert "🤖 agent" in body
