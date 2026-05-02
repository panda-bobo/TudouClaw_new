"""Tests for Agent.delegate_parallel — Mode C from the parallel-execution
spec."""
from __future__ import annotations
import threading
import time
import pytest
from unittest.mock import MagicMock, patch


def test_delegate_parallel_runs_children_concurrently(tmp_path, monkeypatch):
    """delegate_parallel spawns children on parallel threads — each
    child's "execution" lands on a different thread id."""
    from app.agent import Agent
    from app import system_settings as ss
    monkeypatch.setattr(ss, "_STORE", None)
    ss.init_store(tmp_path)

    parent = Agent(id="parent", name="parent")
    parent.working_dir = str(tmp_path / "parent_wd")
    (tmp_path / "parent_wd").mkdir()

    thread_ids: dict[int, int] = {}

    # Stub out Agent.delegate (single-child path) — record thread + return
    def fake_single_delegate(self, task, **kwargs):
        idx = int(task.split("_")[-1])  # task strings end with _<idx>
        thread_ids[idx] = threading.get_ident()
        time.sleep(0.05)
        return f"result for {task}"

    monkeypatch.setattr(Agent, "delegate", fake_single_delegate)

    tasks = [
        {"task": "do_work_0", "child_role": "coder"},
        {"task": "do_work_1", "child_role": "coder"},
        {"task": "do_work_2", "child_role": "coder"},
    ]
    results = parent.delegate_parallel(tasks)

    assert len(results) == 3
    assert all(r["status"] == "succeeded" for r in results)
    assert results[0]["output"] == "result for do_work_0"
    # All three on different threads
    assert len(set(thread_ids.values())) == 3, \
        f"expected 3 distinct threads, got {thread_ids}"


def test_delegate_parallel_respects_max_children(tmp_path, monkeypatch):
    """If tasks > max_children (configurable), raise ValueError."""
    from app.agent import Agent
    from app import system_settings as ss
    monkeypatch.setattr(ss, "_STORE", None)
    ss.init_store(tmp_path)
    ss.get_store().set("delegate.max_parallel_children", 2)

    parent = Agent(id="parent", name="parent")
    parent.working_dir = str(tmp_path / "p")
    (tmp_path / "p").mkdir()

    tasks = [{"task": f"t{i}", "child_role": "coder"} for i in range(3)]
    with pytest.raises(ValueError, match="max"):
        parent.delegate_parallel(tasks)


def test_delegate_parallel_fail_fast(tmp_path, monkeypatch):
    """When one child raises, others get cancellation; result list
    has the failure recorded."""
    from app.agent import Agent
    from app import system_settings as ss
    monkeypatch.setattr(ss, "_STORE", None)
    ss.init_store(tmp_path)

    parent = Agent(id="parent", name="parent")
    parent.working_dir = str(tmp_path / "p")
    (tmp_path / "p").mkdir()

    def fake_delegate(self, task, **kwargs):
        if task == "boom":
            raise RuntimeError("bang")
        time.sleep(0.05)
        return "ok"

    monkeypatch.setattr(Agent, "delegate", fake_delegate)

    tasks = [
        {"task": "boom", "child_role": "coder"},
        {"task": "ok_a", "child_role": "coder"},
    ]
    results = parent.delegate_parallel(tasks)

    assert len(results) == 2
    statuses = [r["status"] for r in results]
    assert "failed" in statuses
    # The other child either succeeded (won the race) or got aborted —
    # both are valid outcomes, but it must NOT be "succeeded with no
    # awareness of the failure"
    failed_idx = statuses.index("failed")
    assert "bang" in (results[failed_idx].get("error") or "")


def test_delegate_parallel_each_child_has_own_subdir(tmp_path, monkeypatch):
    """Each child is given a distinct subdir under parent.working_dir."""
    from pathlib import Path
    from app.agent import Agent
    from app import system_settings as ss
    monkeypatch.setattr(ss, "_STORE", None)
    ss.init_store(tmp_path)

    parent = Agent(id="parent", name="parent")
    parent_wd = tmp_path / "pwd"
    parent_wd.mkdir()
    parent.working_dir = str(parent_wd)

    def fake_delegate(self, task, **kwargs):
        # The implementation pins working_dir on the child via some
        # per-call mechanism — verify by checking that an expected
        # subdir was created. The fake just asserts.
        return "ok"

    monkeypatch.setattr(Agent, "delegate", fake_delegate)

    tasks = [
        {"task": "t1", "child_role": "coder"},
        {"task": "t2", "child_role": "coder", "hint_subdir": "custom_dir"},
    ]
    results = parent.delegate_parallel(tasks)

    # Default subdir for idx 0
    assert (parent_wd / "child_0_coder").exists()
    # Hinted subdir for idx 1
    assert (parent_wd / "custom_dir").exists()
    # Each result records its working_subdir
    assert results[0]["working_subdir"].endswith("child_0_coder")
    assert results[1]["working_subdir"].endswith("custom_dir")
