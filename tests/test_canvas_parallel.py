"""Tests for canvas parallel execution (Mode A) and prerequisites."""
from __future__ import annotations
import pytest

from app.canvas_executor import NodeState, RunState, TERMINAL_NODE_STATES


def test_aborted_is_terminal_node_state():
    """ABORTED is a new terminal state alongside FAILED/SKIPPED/SUCCEEDED."""
    assert hasattr(NodeState, "ABORTED")
    assert NodeState.ABORTED in TERMINAL_NODE_STATES
    assert NodeState.ABORTED.value == "aborted"


def test_run_state_has_aborted():
    """RunState.ABORTED already exists; sanity-check it for the spec."""
    assert RunState.ABORTED.value == "aborted"


def test_pick_all_ready_returns_list_with_independent_branches(tmp_path):
    """When two nodes have no inter-dep and start has SUCCEEDED, both
    are returned by _pick_all_ready in one call."""
    from app.canvas_executor import (
        WorkflowEngine, WorkflowRun, RunState, NodeState, RunStore,
    )
    engine = WorkflowEngine(RunStore(tmp_path))
    run = WorkflowRun(id="r1", state=RunState.RUNNING)
    nodes_by_id = {
        "s": {"id": "s", "type": "start"},
        "a": {"id": "a", "type": "agent"},
        "b": {"id": "b", "type": "agent"},
    }
    deps = {"s": [], "a": ["s"], "b": ["s"]}
    # All pending initially
    run.node_states = {nid: NodeState.PENDING for nid in nodes_by_id}

    # Before s is succeeded — only s is ready
    ready = engine._pick_all_ready(run, nodes_by_id, deps)
    assert ready == ["s"]

    # Mark s succeeded — both a and b ready
    run.node_states["s"] = NodeState.SUCCEEDED
    ready = engine._pick_all_ready(run, nodes_by_id, deps)
    assert sorted(ready) == ["a", "b"]


def test_drive_loop_runs_branches_concurrently(tmp_path, monkeypatch):
    """Smoke: a workflow with two parallel agent branches actually
    runs them on separate threads (we patch _execute_node to record
    thread ids and assert they differ)."""
    import threading
    import time
    from app.canvas_executor import (
        WorkflowEngine, WorkflowRun, RunState, NodeState, RunStore,
    )

    engine = WorkflowEngine(RunStore(tmp_path))
    run = WorkflowRun(id="r2", state=RunState.RUNNING)

    # Track which threads ran which nodes
    thread_ids: dict[str, int] = {}

    def fake_execute(self, run, node, edges):
        thread_ids[node["id"]] = threading.get_ident()
        time.sleep(0.05)   # let the other thread also start
        run.node_states[node["id"]] = NodeState.SUCCEEDED

    monkeypatch.setattr(WorkflowEngine, "_execute_node", fake_execute)

    workflow = {
        "id": "wf-par-test",
        "nodes": [
            {"id": "s", "type": "start"},
            {"id": "a", "type": "agent", "config": {"agent_id": "ax"}},
            {"id": "b", "type": "agent", "config": {"agent_id": "bx"}},
            {"id": "e", "type": "end"},
        ],
        "edges": [
            {"from": "s", "to": "a"},
            {"from": "s", "to": "b"},
            {"from": "a", "to": "e"},
            {"from": "b", "to": "e"},
        ],
    }
    # Init node_states
    for n in workflow["nodes"]:
        run.node_states[n["id"]] = NodeState.PENDING

    engine._drive_loop(run, workflow)

    # Both a and b ran; their thread ids differ
    assert "a" in thread_ids and "b" in thread_ids
    assert thread_ids["a"] != thread_ids["b"], (
        "a and b ran on the same thread — _drive_loop is still serial"
    )
    # Run finished SUCCEEDED
    assert run.state == RunState.SUCCEEDED
