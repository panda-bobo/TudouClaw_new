"""Stage 5 smoke tests for the Deliver phase (PRD §8.5).

Two layers:

* Unit tests on ``app.v2.core.deliver.deliver_artifact`` — exercise each
  kind's dispatcher (``file`` happy/missing, ``email`` / ``rag`` stub,
  ``message`` / ``api_call`` ok, unknown degraded), plus the 3-attempt
  retry for a flaky dispatcher.
* Handler test on ``TaskLoop._deliver`` — assert receipt creation,
  degraded bookkeeping, the empty-handle retry path, and "no artifacts
  → accept".
"""
from __future__ import annotations

import types

import pytest

from app.v2.core.task import (
    Task, Plan, PlanStep, Artifact, TaskPhase, TaskContext,
)
from app.v2.core.deliver import deliver_artifact
from app.v2.core.task_loop import TaskLoop


# ── fakes ─────────────────────────────────────────────────────────────


class FakeBus:
    def __init__(self):
        self.events: list[dict] = []

    def publish(self, task_id, phase, event_type, payload):
        self.events.append({
            "task_id": task_id,
            "phase": phase.value if hasattr(phase, "value") else phase,
            "type": event_type,
            "payload": dict(payload or {}),
        })

    def flush_and_close(self, task_id=None):
        pass


class FakeStore:
    def __init__(self):
        self.saves = 0

    def save(self, task):
        self.saves += 1


class FakeAgent:
    def __init__(self):
        self.id = "av2_test"
        self.capabilities = types.SimpleNamespace(llm_tier="default")


# ── helpers ───────────────────────────────────────────────────────────


def _make_task(artifacts: list[Artifact] | None = None) -> Task:
    task = Task(
        id="t_d",
        agent_id="av2_test",
        template_id="conversation",
        intent="test",
        phase=TaskPhase.DELIVER,
        context=TaskContext(),
        plan=Plan(),
    )
    for a in artifacts or []:
        task.artifacts.append(a)
    return task


def _make_loop(task: Task) -> tuple[TaskLoop, FakeBus]:
    bus = FakeBus()
    loop = TaskLoop(task=task, agent=FakeAgent(), bus=bus, store=FakeStore(), template={})
    return loop, bus


# ── deliver_artifact unit tests ───────────────────────────────────────


def test_dispatch_file_ok(tmp_path):
    f = tmp_path / "out.txt"
    f.write_text("hello")
    art = Artifact(id="a1", kind="file", handle=str(f))
    ok, handle, note = deliver_artifact(art, _make_task())
    assert ok is True
    assert handle == str(f)
    assert "bytes" in note


def test_dispatch_file_missing_retries_then_fails(tmp_path):
    art = Artifact(id="a1", kind="file", handle=str(tmp_path / "nope.txt"))
    ok, handle, note = deliver_artifact(art, _make_task())
    assert ok is False
    assert handle == ""
    assert "not found" in note


def test_dispatch_message_ok():
    art = Artifact(id="a1", kind="message", handle="")
    ok, handle, note = deliver_artifact(art, _make_task())
    assert ok is True
    assert handle.startswith("inline:")


def test_dispatch_api_call_ok():
    art = Artifact(id="a1", kind="api_call", handle="rpc_abc")
    ok, handle, note = deliver_artifact(art, _make_task())
    assert ok is True
    assert handle == "rpc_abc"


def test_dispatch_email_without_template_fails_degraded():
    """Without a template-declared delivery config, email kind degrades
    rather than silently pretending to send."""
    art = Artifact(id="a1", kind="email", handle="msg_123")
    ok, handle, note = deliver_artifact(art, _make_task())
    assert ok is False
    assert "not configured" in note


def test_dispatch_rag_stub_fails():
    art = Artifact(id="a1", kind="rag_entry", handle="doc_1")
    ok, handle, note = deliver_artifact(art, _make_task())
    assert ok is False


def test_dispatch_unknown_kind_degrades():
    art = Artifact(id="a1", kind="quantum_foo", handle="?")
    ok, handle, note = deliver_artifact(art, _make_task())
    assert ok is False
    assert "unknown" in note


def test_dispatch_retries_until_success(monkeypatch):
    """Happy-path verification of the retry loop: dispatcher returns False
    twice then True → final outcome is ok, retry count matches PRD (≤2)."""
    calls = {"n": 0}

    def flaky(artifact, task):
        calls["n"] += 1
        if calls["n"] < 3:
            return False, "", "transient"
        return True, "handle-x", "finally"

    monkeypatch.setitem(
        __import__("app.v2.core.deliver", fromlist=["_DISPATCH"])._DISPATCH,
        "file", flaky,
    )
    art = Artifact(id="a1", kind="file", handle="/ignored")
    ok, handle, note = deliver_artifact(art, _make_task())
    assert ok is True
    assert handle == "handle-x"
    assert calls["n"] == 3  # 1 initial + 2 retries


def test_dispatch_retries_exhausted(monkeypatch):
    """Dispatcher that never succeeds is retried exactly 2 extra times."""
    calls = {"n": 0}

    def always_fails(artifact, task):
        calls["n"] += 1
        return False, "", "nope"

    monkeypatch.setitem(
        __import__("app.v2.core.deliver", fromlist=["_DISPATCH"])._DISPATCH,
        "file", always_fails,
    )
    art = Artifact(id="a1", kind="file", handle="/ignored")
    ok, _, _ = deliver_artifact(art, _make_task())
    assert ok is False
    assert calls["n"] == 3  # 1 initial + 2 retries, per PRD


def test_dispatch_exception_is_retried(monkeypatch):
    calls = {"n": 0}

    def boom(artifact, task):
        calls["n"] += 1
        raise RuntimeError("kaboom")

    monkeypatch.setitem(
        __import__("app.v2.core.deliver", fromlist=["_DISPATCH"])._DISPATCH,
        "file", boom,
    )
    art = Artifact(id="a1", kind="file", handle="/ignored")
    ok, _, note = deliver_artifact(art, _make_task())
    assert ok is False
    assert calls["n"] == 3
    assert "RuntimeError" in note


# ── TaskLoop._deliver handler tests ───────────────────────────────────


def test_deliver_no_artifacts_passes():
    task = _make_task()
    loop, bus = _make_loop(task)
    assert loop._deliver() is True
    assert task.artifacts == []


def test_deliver_file_creates_receipt(tmp_path):
    f = tmp_path / "r.txt"
    f.write_text("x")
    art = Artifact(id="a1", kind="file", handle=str(f))
    task = _make_task([art])
    loop, bus = _make_loop(task)

    assert loop._deliver() is True

    # Original + receipt.
    assert len(task.artifacts) == 2
    receipt = task.artifacts[-1]
    assert receipt.kind == "delivery_receipt"
    assert receipt.handle == str(f)
    assert receipt.produced_by_tool == "deliver/file"

    # Event with delivered_ok=True emitted for the receipt.
    art_events = [e for e in bus.events if e["type"] == "artifact_created"]
    assert len(art_events) == 1
    assert art_events[0]["payload"]["delivered_ok"] is True
    assert art_events[0]["payload"]["for_artifact_id"] == "a1"


def test_deliver_missing_file_marks_degraded(tmp_path):
    art = Artifact(id="a1", kind="file", handle=str(tmp_path / "ghost.txt"))
    task = _make_task([art])
    loop, bus = _make_loop(task)

    assert loop._deliver() is True  # PRD: degraded ≠ blocking

    receipt = task.artifacts[-1]
    assert receipt.kind == "delivery_receipt"
    assert receipt.handle == "degraded:a1"
    assert "not found" in receipt.summary

    # phase_error summary emitted for observability.
    errs = [e for e in bus.events if e["type"] == "phase_error"]
    assert len(errs) == 1
    assert "1/1" in errs[0]["payload"]["error"]


def test_deliver_empty_handle_returns_false_for_retry():
    """PRD exit condition: all original handles must be non-empty.
    Empty handle is treated as a malformed artifact → retry the phase."""
    art = Artifact(id="a1", kind="file", handle="")
    task = _make_task([art])
    loop, bus = _make_loop(task)

    assert loop._deliver() is False
    # No receipt created because we bailed early.
    assert all(a.kind != "delivery_receipt" for a in task.artifacts)
    errs = [e for e in bus.events if e["type"] == "phase_error"]
    assert len(errs) == 1
    assert "empty handle" in errs[0]["payload"]["error"]


def test_deliver_mixed_ok_and_degraded(tmp_path):
    """A mix of deliverable + undeliverable artifacts. Phase passes (True),
    degraded count is tracked, both get receipts."""
    good = tmp_path / "good.txt"
    good.write_text("ok")
    task = _make_task([
        Artifact(id="a1", kind="file",  handle=str(good)),
        Artifact(id="a2", kind="email", handle="msg_1"),      # stub → degraded
        Artifact(id="a3", kind="message", handle="just-text"),
    ])
    loop, bus = _make_loop(task)

    assert loop._deliver() is True
    receipts = [a for a in task.artifacts if a.kind == "delivery_receipt"]
    assert len(receipts) == 3
    by_for = {
        e["payload"]["for_artifact_id"]: e["payload"]["delivered_ok"]
        for e in bus.events if e["type"] == "artifact_created"
    }
    assert by_for == {"a1": True, "a2": False, "a3": True}

    errs = [e for e in bus.events if e["type"] == "phase_error"]
    assert len(errs) == 1
    assert "1/3" in errs[0]["payload"]["error"]


def test_deliver_ignores_pre_existing_receipts(tmp_path):
    """Re-running Deliver on a task that already has a receipt should
    not re-dispatch the receipt itself (it's not an original artifact)."""
    f = tmp_path / "r.txt"
    f.write_text("x")
    task = _make_task([
        Artifact(id="a1", kind="file", handle=str(f)),
        Artifact(id="R-0", kind="delivery_receipt", handle="prev"),
    ])
    loop, bus = _make_loop(task)
    assert loop._deliver() is True
    # Only one NEW receipt (for a1); original receipt untouched.
    receipts = [a for a in task.artifacts if a.kind == "delivery_receipt"]
    assert len(receipts) == 2  # 1 pre-existing + 1 new
