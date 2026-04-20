"""Tests for template-driven Deliver dispatch (PRD §8.5).

Template's ``expected_artifacts[].delivery`` declares how to ship each
produced artifact: via MCP tool, via skill, or ``none`` (no-op). The
dispatcher selects per-artifact and falls back to the legacy kind-based
dispatcher when no template config matches.
"""
from __future__ import annotations

from app.v2.core.task import Task, Artifact, TaskContext, TaskPhase
from app.v2.core.deliver import deliver_artifact


def _mk_task(slots=None, intent="x"):
    return Task(
        id="t_d", agent_id="av2", template_id="x",
        intent=intent, phase=TaskPhase.DELIVER,
        context=TaskContext(filled_slots=dict(slots or {})),
    )


# ── via: none ─────────────────────────────────────────────────────────


def test_template_via_none_succeeds_without_side_effects():
    art = Artifact(id="a1", kind="email", handle="msg_1")
    tmpl = {
        "expected_artifacts": [
            {"kind": "email", "delivery": {"via": "none"}}
        ]
    }
    ok, handle, note = deliver_artifact(art, _mk_task(), template=tmpl)
    assert ok is True
    assert handle == "msg_1"
    assert "disabled" in note


# ── via: mcp (happy path) ─────────────────────────────────────────────


def test_template_via_mcp_calls_bridge(monkeypatch):
    calls = []

    def fake_invoke(agent_id, tool, args):
        calls.append({"agent": agent_id, "tool": tool, "args": args})
        return "email_msg_xyz"

    from app.v2.bridges import mcp_bridge
    monkeypatch.setattr(mcp_bridge, "invoke_mcp", fake_invoke)

    art = Artifact(id="a1", kind="email", handle="/tmp/report.pdf",
                   summary="final report")
    task = _mk_task(slots={"recipient": "boss@example.com", "topic": "Q3"})
    tmpl = {
        "expected_artifacts": [{
            "kind": "email",
            "delivery": {
                "via": "mcp",
                "tool": "send_email",
                "args_template": {
                    "to": "{recipient}",
                    "subject": "{topic} 报告",
                    "attachment": "{artifact_handle}",
                    "body": "摘要：{artifact_summary}",
                },
            },
        }]
    }

    ok, handle, note = deliver_artifact(art, task, template=tmpl)
    assert ok is True
    assert handle == "email_msg_xyz"
    assert note == "mcp:send_email"

    assert len(calls) == 1
    c = calls[0]
    assert c["agent"] == "av2"
    assert c["tool"] == "send_email"
    # Interpolation correctness.
    assert c["args"] == {
        "to": "boss@example.com",
        "subject": "Q3 报告",
        "attachment": "/tmp/report.pdf",
        "body": "摘要：final report",
    }


def test_template_via_skill_calls_bridge(monkeypatch):
    called = {}

    def fake_invoke(agent_id, skill, args):
        called["skill"] = skill
        called["args"] = args
        return "rag_doc_42"

    from app.v2.bridges import skill_bridge
    monkeypatch.setattr(skill_bridge, "invoke_skill", fake_invoke)

    art = Artifact(id="a1", kind="rag_entry", handle="/tmp/note.md")
    tmpl = {
        "expected_artifacts": [{
            "kind": "rag_entry",
            "delivery": {
                "via": "skill",
                "tool": "rag_ingest",
                "args_template": {"path": "{artifact_handle}"},
            },
        }]
    }
    ok, handle, note = deliver_artifact(art, _mk_task(), template=tmpl)
    assert ok is True
    assert handle == "rag_doc_42"
    assert called["skill"] == "rag_ingest"
    assert called["args"] == {"path": "/tmp/note.md"}


# ── error paths ───────────────────────────────────────────────────────


def test_template_missing_tool_fails():
    art = Artifact(id="a1", kind="email", handle="x")
    tmpl = {
        "expected_artifacts": [
            {"kind": "email", "delivery": {"via": "mcp"}}   # no tool
        ]
    }
    ok, handle, note = deliver_artifact(art, _mk_task(), template=tmpl)
    assert ok is False
    assert "no tool" in note


def test_template_unknown_via_fails():
    art = Artifact(id="a1", kind="email", handle="x")
    tmpl = {
        "expected_artifacts": [
            {"kind": "email", "delivery": {"via": "smoke_signal", "tool": "x"}}
        ]
    }
    ok, handle, note = deliver_artifact(art, _mk_task(), template=tmpl)
    assert ok is False
    assert "unknown delivery.via" in note


def test_template_tool_returning_empty_is_degraded(monkeypatch):
    from app.v2.bridges import mcp_bridge
    monkeypatch.setattr(mcp_bridge, "invoke_mcp",
                        lambda *_a, **_k: "")

    art = Artifact(id="a1", kind="email", handle="x")
    tmpl = {
        "expected_artifacts": [
            {"kind": "email",
             "delivery": {"via": "mcp", "tool": "send_email"}}
        ]
    }
    ok, _, note = deliver_artifact(art, _mk_task(), template=tmpl)
    assert ok is False
    assert "empty" in note


# ── fallback path ─────────────────────────────────────────────────────


def test_no_template_match_falls_back_to_kind_dispatch(tmp_path):
    """If the template doesn't declare delivery for this artifact kind,
    we use the built-in kind-based dispatcher. For ``file`` that means
    existence check."""
    f = tmp_path / "a.txt"
    f.write_text("hello")
    art = Artifact(id="a1", kind="file", handle=str(f))
    tmpl = {
        "expected_artifacts": [
            # Only declares delivery for email, not file.
            {"kind": "email",
             "delivery": {"via": "mcp", "tool": "send_email"}}
        ]
    }
    ok, handle, note = deliver_artifact(art, _mk_task(), template=tmpl)
    assert ok is True
    assert handle == str(f)


# ── pattern matching ──────────────────────────────────────────────────


def test_template_pattern_matches_by_filename(tmp_path):
    f = tmp_path / "report.pptx"
    f.write_text("x")
    art = Artifact(id="a1", kind="file", handle=str(f))
    tmpl = {
        "expected_artifacts": [
            # Matches *.pptx file → uses delivery.via=none (succeeds).
            {"kind": "file", "pattern": "*.pptx",
             "delivery": {"via": "none"}},
        ]
    }
    ok, handle, note = deliver_artifact(art, _mk_task(), template=tmpl)
    assert ok is True
    assert "disabled" in note  # took the via=none branch, not file-existence


def test_template_pattern_misses_falls_back(tmp_path):
    f = tmp_path / "report.txt"
    f.write_text("x")
    art = Artifact(id="a1", kind="file", handle=str(f))
    tmpl = {
        "expected_artifacts": [
            {"kind": "file", "pattern": "*.pptx",
             "delivery": {"via": "none"}},
        ]
    }
    ok, handle, note = deliver_artifact(art, _mk_task(), template=tmpl)
    assert ok is True
    # Fell back to file-existence check, not the via=none branch.
    assert "bytes" in note
