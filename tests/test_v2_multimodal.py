"""Tests for V2 multimodal gate in Intake + attachment bridge."""
from __future__ import annotations

import os
import tempfile

import pytest

from app.v2.core.task import Task, TaskContext, TaskPhase, TaskStatus
from app.v2.core.task_loop import TaskLoop
from app.v2.bridges import attachment_bridge


class FakeBus:
    def __init__(self): self.events = []
    def publish(self, tid, phase, et, payload):
        self.events.append({"type": et, "payload": dict(payload or {})})
    def flush_and_close(self, tid=None): pass


class FakeStore:
    def save(self, task): pass


class FakeAgent:
    def __init__(self, tier="default"):
        import types as _t
        self.id = "av2"
        self.capabilities = _t.SimpleNamespace(llm_tier=tier)


# ── Intake multimodal gate ────────────────────────────────────────────


def _intake_with_attachments(monkeypatch, *, supports_mm: bool):
    """Return (task, bus) after a single Intake dispatch on a task that
    carries one image attachment. ``supports_mm`` fakes the registry
    response for multimodal support."""
    task = Task(
        id="t_mm", agent_id="av2",
        template_id="conversation",
        intent="describe this",
        phase=TaskPhase.INTAKE, status=TaskStatus.RUNNING,
        context=TaskContext(
            attachments=[{"kind": "image", "handle": "/tmp/x.png",
                          "mime": "image/png", "name": "x.png"}],
        ),
    )
    bus = FakeBus()

    # Stub tier routing so we don't need a real provider.
    from app.v2.bridges import llm_tier_routing
    monkeypatch.setattr(llm_tier_routing, "resolve_tier",
                        lambda _t: ("fake_provider", "fake-model"))

    # Stub registry.provider_supports_multimodal.
    class _FakeReg:
        def provider_supports_multimodal(self, pid):
            return supports_mm
    import app.llm as _llm
    monkeypatch.setattr(_llm, "get_registry", lambda: _FakeReg())

    loop = TaskLoop(task, FakeAgent(tier="default"), bus, FakeStore(),
                    template={"id": "conversation", "required_slots": [],
                              "verify_rules": []})
    loop._intake()
    return task, bus


def test_multimodal_task_pauses_when_provider_unsupported(monkeypatch):
    task, bus = _intake_with_attachments(monkeypatch, supports_mm=False)
    # Task should pause, emit clarification, not advance to Plan.
    assert task.status == TaskStatus.PAUSED
    assert task.phase == TaskPhase.INTAKE
    clar = [e for e in bus.events if e["type"] == "intake_clarification"]
    assert len(clar) == 1
    p = clar[0]["payload"]
    assert "image" in p.get("attachment_kinds", [])
    assert "多模态" in p.get("question", "")
    assert "multimodal_provider" in p.get("missing_slots", [])


def test_multimodal_task_proceeds_when_provider_supports(monkeypatch):
    """If the provider advertises multimodal, Intake continues as normal
    (no clarification event for multimodal)."""
    # Stub the LLM slot extraction so Intake doesn't actually hit a model.
    from app.v2.bridges import llm_bridge
    monkeypatch.setattr(llm_bridge, "call_llm",
                        lambda **_k: {"role": "assistant",
                                      "content": '```json\n{"filled":{},"missing":[],"clarification":""}\n```',
                                      "tool_calls": []})
    task, bus = _intake_with_attachments(monkeypatch, supports_mm=True)
    assert task.status == TaskStatus.RUNNING
    # No multimodal clarification was emitted.
    assert not any(
        e["type"] == "intake_clarification"
        and "multimodal_provider" in e["payload"].get("missing_slots", [])
        for e in bus.events
    )


def test_no_attachments_bypasses_multimodal_gate(monkeypatch):
    """Tasks without attachments don't touch the multimodal check at all."""
    task = Task(
        id="t_text", agent_id="av2",
        template_id="conversation", intent="pure text",
        phase=TaskPhase.INTAKE, status=TaskStatus.RUNNING,
    )
    bus = FakeBus()
    from app.v2.bridges import llm_bridge
    monkeypatch.setattr(llm_bridge, "call_llm",
                        lambda **_k: {"role": "assistant",
                                      "content": '```json\n{"filled":{},"missing":[],"clarification":""}\n```',
                                      "tool_calls": []})
    loop = TaskLoop(task, FakeAgent(), bus, FakeStore(),
                    template={"id": "conversation", "required_slots": []})
    loop._intake()
    # No clarification emitted, no pause.
    assert task.status == TaskStatus.RUNNING
    assert not any(e["type"] == "intake_clarification" for e in bus.events)


# ── attachment_bridge ─────────────────────────────────────────────────


def test_save_attachment_writes_file_with_sane_name(tmp_path):
    wd = str(tmp_path)
    desc = attachment_bridge.save_attachment(
        agent_working_dir=wd, task_id="t1",
        filename="../../etc/passwd",  # path-traversal attempt
        content=b"hello", mime="image/png",
    )
    assert os.path.isfile(desc["handle"])
    # Traversal components stripped; file ended up under agent/attachments/t1.
    assert desc["handle"].startswith(os.path.join(wd, "attachments", "t1"))
    assert desc["kind"] == "image"
    assert desc["size"] == 5
    # Filename sanitised (no slashes, original basename preserved).
    assert "/" not in os.path.basename(desc["handle"])


def test_save_attachment_refuses_empty(tmp_path):
    with pytest.raises(ValueError):
        attachment_bridge.save_attachment(
            agent_working_dir=str(tmp_path), task_id="t1",
            filename="x.png", content=b"",
        )


def test_save_attachment_refuses_bad_agent_dir():
    with pytest.raises(ValueError):
        attachment_bridge.save_attachment(
            agent_working_dir="/does/not/exist/anywhere",
            task_id="t1", filename="x.png", content=b"x",
        )


def test_infer_kind_from_mime_and_extension(tmp_path):
    wd = str(tmp_path)
    d1 = attachment_bridge.save_attachment(
        agent_working_dir=wd, task_id="t", filename="a.png",
        content=b"x", mime="image/png",
    )
    assert d1["kind"] == "image"
    d2 = attachment_bridge.save_attachment(
        agent_working_dir=wd, task_id="t", filename="a.mp3",
        content=b"x", mime="",
    )
    assert d2["kind"] == "audio"
    d3 = attachment_bridge.save_attachment(
        agent_working_dir=wd, task_id="t", filename="a.unknown",
        content=b"x", mime="",
    )
    assert d3["kind"] == "file"


def test_resolve_path_blocks_traversal(tmp_path):
    wd = str(tmp_path)
    os.makedirs(os.path.join(wd, "attachments"), exist_ok=True)
    good = attachment_bridge.save_attachment(
        agent_working_dir=wd, task_id="t", filename="a.png",
        content=b"x", mime="image/png",
    )
    # Legitimate handle resolves.
    assert attachment_bridge.resolve_path_for_serve(
        agent_working_dir=wd, handle=good["handle"],
    ) == good["handle"]
    # Path outside attachments/ refused.
    outside = os.path.join(wd, "other_file.txt")
    with open(outside, "w") as f: f.write("nope")
    with pytest.raises(ValueError):
        attachment_bridge.resolve_path_for_serve(
            agent_working_dir=wd, handle=outside,
        )
