"""Tests for the canvas-executor deliverable variable contract.

Companion to docs/superpowers/specs/2026-05-02-canvas-deliverable-design.md.
"""
from __future__ import annotations
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app import canvas_artifacts as ca
from app import canvas_executor as ce
from app.canvas_executor import WorkflowEngine, WorkflowRun, RunState
from app.chat_task import ChatTaskStatus


@pytest.fixture
def fake_canvas_env(tmp_path, monkeypatch):
    """Pre-configured (engine, run, agent, task, store) tuple for
    _exec_agent unit tests. Caller customizes the agent's chat_async
    behavior (what files it writes, what status it ends in) and the
    node config, then invokes ce._exec_agent.

    The fake agent has all the attributes _exec_agent touches:
    _lock, _active_context_id, _messages_by_context, _switch_context,
    working_dir (mutated by executor before chat). chat_async defaults
    to writing nothing + returning a COMPLETED task; tests override
    it via fake_agent.chat_async = ...
    """
    monkeypatch.setattr(ca, "_STORE", None)
    ca.init_store(tmp_path)
    store = ca.get_store()

    run = WorkflowRun(
        id="run-test",
        workflow_id="wf-test",
        workflow_name="t",
        state=RunState.RUNNING,
        started_at=time.time(),
    )
    engine = MagicMock(spec=WorkflowEngine)
    engine.hub = MagicMock()
    engine.artifact_store = store

    fake_task = MagicMock()
    fake_task.status = ChatTaskStatus.COMPLETED
    fake_task.id = "fake-task-id"
    fake_task.result = "fake reply"
    fake_task.created_at = time.time()
    fake_task.updated_at = time.time()
    fake_task.error = None

    fake_agent = MagicMock()
    fake_agent.id = "ag-x"
    fake_agent.name = "test-agent"
    fake_agent._lock = MagicMock()
    fake_agent._lock.__enter__ = MagicMock(return_value=None)
    fake_agent._lock.__exit__ = MagicMock(return_value=None)
    fake_agent._active_context_id = "solo"
    fake_agent._messages_by_context = {}
    fake_agent._switch_context = MagicMock()
    fake_agent.working_dir = ""

    def default_chat_async(prompt, source=""):
        # Default: write a real file so EMPTY_DELIVERABLE check (Task 3,
        # not yet landed) won't trip. Tests can override this.
        Path(fake_agent.working_dir, "x.txt").write_text("y")
        return fake_task

    fake_agent.chat_async = default_chat_async
    engine.hub.get_agent = MagicMock(return_value=fake_agent)

    return engine, run, fake_agent, fake_task, store


def test_outputs_dict_has_deliverable_no_legacy_keys(fake_canvas_env):
    """Behavior test: outputs returned by _exec_agent contain
    `deliverable` and `deliverable_relative` but NOT the legacy
    `deliverable_type` or `success_marker_file` keys.

    Drives _exec_agent end-to-end with a mocked agent + chat_async to
    verify the actual returned dict shape — not just source-grep.
    """
    engine, run, fake_agent, fake_task, store = fake_canvas_env
    node = {"id": "n1", "label": "test", "type": "agent", "config": {
        "agent_id": "ag-x", "prompt": "go", "timeout": 5,
    }}

    outputs = ce._exec_agent(engine, run, node, node["config"])

    assert "deliverable" in outputs
    assert "deliverable_relative" in outputs
    assert "deliverable_type" not in outputs
    assert "success_marker_file" not in outputs


def test_success_when_file_glob_is_canonical_no_alias(fake_canvas_env):
    """When config has success_when.file_glob (no deliverable.file_glob),
    early-termination still triggers on the marker file. This locks in
    that success_when is read directly — no alias hop through
    deliverable_cfg."""
    engine, run, fake_agent, fake_task, store = fake_canvas_env

    # Override chat_async: stay THINKING + write the marker so the
    # poll loop's marker-scan triggers abort (not the LLM-COMPLETED path).
    fake_task.status = ChatTaskStatus.THINKING
    fake_task.result = ""
    fake_task.abort = MagicMock(side_effect=lambda: setattr(fake_task, "status", ChatTaskStatus.ABORTED))

    def fake_chat_async(prompt, source=""):
        # Drop the marker file into the agent's now-set working_dir.
        # The marker scan should pick it up and abort the task.
        Path(fake_agent.working_dir, "marker.md").write_text("done")
        return fake_task

    fake_agent.chat_async = fake_chat_async

    node = {
        "id": "n_alias_test",
        "label": "test",
        "config": {
            "agent_id": "ag-x",
            "prompt": "produce marker.md",
            "timeout": 5,
            "success_when": {"file_glob": "marker.md"},
        },
    }

    outputs = ce._exec_agent(engine, run, node, node["config"])

    # Verify deliverable variable is set correctly
    assert "deliverable" in outputs
    assert outputs["deliverable"].endswith("/n_alias_test")
    # marker.md was registered + abort was called
    assert outputs.get("artifact_count", 0) >= 1
    fake_task.abort.assert_called()


def test_empty_deliverable_check_helper(tmp_path):
    """The helper that decides whether a node produced something.
    Returns False when the subdir is empty or contains only _meta.json."""
    # Empty dir
    empty = tmp_path / "n_empty"
    empty.mkdir()
    assert ce._has_real_deliverable(empty) is False

    # Only _meta.json — still empty
    only_meta = tmp_path / "n_only_meta"
    only_meta.mkdir()
    (only_meta / "_meta.json").write_text("{}")
    assert ce._has_real_deliverable(only_meta) is False

    # Real file
    has_file = tmp_path / "n_real"
    has_file.mkdir()
    (has_file / "report.md").write_text("hello")
    assert ce._has_real_deliverable(has_file) is True

    # Nested file
    nested = tmp_path / "n_nested"
    (nested / "app/backend").mkdir(parents=True)
    (nested / "app/backend/server.py").write_text("# code")
    assert ce._has_real_deliverable(nested) is True

    # Nonexistent dir
    missing = tmp_path / "n_never"
    assert ce._has_real_deliverable(missing) is False


def test_exec_agent_raises_on_empty_subdir(fake_canvas_env):
    """When an agent leaves its subdir empty (only _meta.json),
    _exec_agent raises RuntimeError with error_code: EMPTY_DELIVERABLE.
    Reuses the fake_canvas_env fixture; overrides chat_async to write
    NOTHING (default fixture writes x.txt; here we override to no-op).
    """
    engine, run, fake_agent, fake_task, store = fake_canvas_env

    # Override default_chat_async — return COMPLETED but write no file
    def empty_chat_async(prompt, source=""):
        return fake_task

    fake_agent.chat_async = empty_chat_async

    node = {"id": "n_empty", "label": "test", "config": {
        "agent_id": "ag-x", "prompt": "do nothing", "timeout": 5,
    }}

    with pytest.raises(RuntimeError, match="EMPTY_DELIVERABLE"):
        ce._exec_agent(engine, run, node, node["config"])
