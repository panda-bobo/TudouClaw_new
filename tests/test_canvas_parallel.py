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
