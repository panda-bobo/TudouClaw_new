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


def test_cancel_propagation_aborts_sibling(tmp_path, monkeypatch):
    """When one parallel branch fails, the sibling's poll loop checks
    run._cancel_event and raises _NodeAbortedSibling, which maps to
    NodeState.ABORTED. Run state ends ABORTED.

    Critical path test for Task 5's fail-fast mechanism.
    """
    import threading
    import time
    from app import canvas_executor as ce
    from app.canvas_executor import (
        WorkflowEngine, WorkflowRun, RunState, NodeState, RunStore,
        _NodeAbortedSibling,
    )

    engine = WorkflowEngine(RunStore(tmp_path))
    run = WorkflowRun(id="r-cancel", state=RunState.RUNNING)

    # Custom _execute_node that simulates the real fail-fast contract:
    # - "agent_fail" raises immediately → _drive_loop's as_completed
    #   handler should call cancel_event.set()
    # - "agent_slow" loops checking run._cancel_event; when set, raises
    #   _NodeAbortedSibling, which the REAL _execute_node maps to
    #   NodeState.ABORTED. We re-implement that mapping here in the
    #   monkey-patched version so the test exercises the right state
    #   transitions.
    def fake_execute(self_engine, run_arg, node, edges):
        nid = node["id"]
        if nid == "start":
            run_arg.node_states[nid] = NodeState.SUCCEEDED
            return
        if nid == "end":
            run_arg.node_states[nid] = NodeState.SUCCEEDED
            return
        if nid == "agent_fail":
            # Mark FAILED and raise so _drive_loop sets cancel_event
            run_arg.node_states[nid] = NodeState.FAILED
            raise RuntimeError("intentional failure")
        if nid == "agent_slow":
            # Simulate _exec_agent's poll loop checking cancel_event
            deadline = time.time() + 5
            while time.time() < deadline:
                ev = getattr(run_arg, "_cancel_event", None)
                if ev is not None and ev.is_set():
                    # Real _execute_node would catch _NodeAbortedSibling
                    # and set NodeState.ABORTED. Replicate that here so
                    # the test's monkeypatched flow matches the real one.
                    run_arg.node_states[nid] = NodeState.ABORTED
                    return
                time.sleep(0.05)
            # Should never reach here in this test — cancel should fire
            run_arg.node_states[nid] = NodeState.SUCCEEDED

    monkeypatch.setattr(WorkflowEngine, "_execute_node", fake_execute)

    workflow = {
        "id": "wf-cancel-test",
        "nodes": [
            {"id": "start", "type": "start"},
            {"id": "agent_fail", "type": "agent", "config": {"agent_id": "ax"}},
            {"id": "agent_slow", "type": "agent", "config": {"agent_id": "bx"}},
            {"id": "end", "type": "end"},
        ],
        "edges": [
            {"from": "start", "to": "agent_fail"},
            {"from": "start", "to": "agent_slow"},
            {"from": "agent_fail", "to": "end"},
            {"from": "agent_slow", "to": "end"},
        ],
    }
    for n in workflow["nodes"]:
        run.node_states[n["id"]] = NodeState.PENDING

    engine._drive_loop(run, workflow)

    assert run.node_states["agent_fail"] == NodeState.FAILED, (
        f"agent_fail expected FAILED, got {run.node_states['agent_fail']}"
    )
    assert run.node_states["agent_slow"] == NodeState.ABORTED, (
        f"agent_slow expected ABORTED (sibling cancel), got {run.node_states['agent_slow']}"
    )
    # Run ends ABORTED (not FAILED) per spec
    assert run.state == RunState.ABORTED, (
        f"run expected RunState.ABORTED, got {run.state}"
    )
    # End node should be SKIPPED (cascade from upstream FAILED/ABORTED)
    assert run.node_states["end"] == NodeState.SKIPPED, (
        f"end expected SKIPPED (cascade), got {run.node_states['end']}"
    )
