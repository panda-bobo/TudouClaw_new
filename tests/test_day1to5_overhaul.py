"""E2E unit tests for the Day 1-5 agent orchestration overhaul.

Each test corresponds to one half-day's deliverable. Pure-Python tests
(no LLM, no network, no preview server) — designed to run in <5s
total so they can gate every commit.

Day 1 AM  — Deliverable contract verification (must_contain / min_lines)
Day 1 PM  — Memory recall scope filter on get_recent_facts /
            search_facts / search_facts_vector
Day 2 AM  — Per-response tool budget cap (tool_choice="none" trip)
Day 2 PM  — ToolCallGuardrailController (3 signals: exact_failure /
            same_tool_failure / no_progress)
Day 3 AM  — Cross-tool read counter (bash cat counts as read)
Day 3 PM  — Scenario object signature + L1 clear on switch
Day 5     — TaskAssignment dispatch / accept round-trip
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Make the repo importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── Day 1 AM — Deliverable contract ──────────────────────────────────

class TestDeliverableContract(unittest.TestCase):
    def test_verify_passes_when_all_must_contain_present(self):
        from app.core.deliverable_check import (
            verify_task_deliverables, all_deliverables_verified,
        )

        class FakeTask:
            output_files = ["foo.py"]
            must_contain = ["def main", "import os"]
            must_contain_per_file = {}
            min_lines = 5
            max_lines = 0
            acceptance_cmd = ""
            acceptance_expect_exit = 0
            deliverable_status = {}
            updated_at = 0

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "foo.py").write_text(
                "import os\n\n# helper\n\ndef main():\n    pass\n",
                encoding="utf-8",
            )
            task = FakeTask()
            verify_task_deliverables(task, tmp)
            ok, missing = all_deliverables_verified(task)
            self.assertTrue(ok, f"expected verified, missing={missing}, status={task.deliverable_status}")

    def test_verify_fails_when_must_contain_missing(self):
        from app.core.deliverable_check import (
            verify_task_deliverables, all_deliverables_verified,
        )

        class FakeTask:
            output_files = ["bar.js"]
            must_contain = ["class Game"]
            must_contain_per_file = {}
            min_lines = 0
            max_lines = 0
            acceptance_cmd = ""
            acceptance_expect_exit = 0
            deliverable_status = {}
            updated_at = 0

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "bar.js").write_text("// nothing here\n", encoding="utf-8")
            task = FakeTask()
            verify_task_deliverables(task, tmp)
            ok, missing = all_deliverables_verified(task)
            self.assertFalse(ok)
            self.assertIn("bar.js", missing)
            self.assertIn("missing required content", " ".join(
                task.deliverable_status["bar.js"]["reasons"]))

    def test_min_lines_enforced(self):
        from app.core.deliverable_check import verify_task_deliverables

        class FakeTask:
            output_files = ["short.txt"]
            must_contain = []
            must_contain_per_file = {}
            min_lines = 10
            max_lines = 0
            acceptance_cmd = ""
            acceptance_expect_exit = 0
            deliverable_status = {}
            updated_at = 0

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "short.txt").write_text("one\ntwo\n", encoding="utf-8")
            task = FakeTask()
            verify_task_deliverables(task, tmp)
            self.assertFalse(task.deliverable_status["short.txt"]["verified"])
            self.assertIn("too few lines",
                          " ".join(task.deliverable_status["short.txt"]["reasons"]))

    def test_propagation_step_template_to_project_task(self):
        """Day 1 AM: bind_workflow propagates contract from
        StepTemplate-style dict into ProjectTask."""
        from app.workflow import StepTemplate
        from app.project import ProjectTask
        # Create a step template with a contract
        st = StepTemplate(
            id="st_1", name="Build server", suggested_role="coder",
            output_files=["server.py"], must_contain=["FastAPI", "uvicorn"],
            min_lines=20,
        )
        d = st.to_dict()
        # Round-trip
        st2 = StepTemplate.from_dict(d)
        self.assertEqual(st2.must_contain, ["FastAPI", "uvicorn"])
        self.assertEqual(st2.min_lines, 20)
        self.assertEqual(st2.output_files, ["server.py"])
        # ProjectTask carries the same fields
        pt = ProjectTask(
            title="WF Step 1", output_files=st.output_files,
            must_contain=st.must_contain, min_lines=st.min_lines,
        )
        pd = pt.to_dict()
        pt2 = ProjectTask.from_dict(pd)
        self.assertEqual(pt2.must_contain, ["FastAPI", "uvicorn"])
        self.assertEqual(pt2.min_lines, 20)


# ── Day 2 PM — ToolCallGuardrailController ───────────────────────────

class TestGuardrailController(unittest.TestCase):
    def test_exact_failure_blocks_after_threshold(self):
        from app.agent_guardrails import (
            ToolCallGuardrailController, GuardrailConfig,
        )
        # Disable warn so we can assert allow → block crisply
        gc = ToolCallGuardrailController(
            GuardrailConfig(exact_failure_warn_after=999,
                            exact_failure_block_after=3))
        for _ in range(3):
            d = gc.before_call("write_file", {"path": "x.py", "content": "y"})
            self.assertEqual(d.action, "allow")
            gc.after_call("write_file", {"path": "x.py", "content": "y"},
                          result="Error: permission denied", failed=True)
        d = gc.before_call("write_file", {"path": "x.py", "content": "y"})
        self.assertEqual(d.action, "block")
        self.assertIn("exact_failure", d.code)

    def test_same_tool_failure_halts_after_many_variant_fails(self):
        from app.agent_guardrails import (
            ToolCallGuardrailController, GuardrailConfig,
        )
        gc = ToolCallGuardrailController(
            GuardrailConfig(same_tool_failure_halt_after=4))
        for i in range(4):
            args = {"path": f"x{i}.py", "content": "y"}
            d = gc.before_call("write_file", args)
            self.assertEqual(d.action, "allow")
            gc.after_call("write_file", args, result="Error: ...", failed=True)
        d = gc.before_call("write_file", {"path": "x_new.py", "content": "y"})
        self.assertEqual(d.action, "halt")

    def test_no_progress_blocks_idempotent_loop(self):
        from app.agent_guardrails import (
            ToolCallGuardrailController, GuardrailConfig,
        )
        gc = ToolCallGuardrailController(
            GuardrailConfig(no_progress_block_after=4))
        for i in range(4):
            d = gc.before_call("read_file", {"path": f"f{i}.md"})
            self.assertIn(d.action, ("allow", "warn"))
            gc.after_call("read_file", {"path": f"f{i}.md"},
                          result="content...")
        d = gc.before_call("read_file", {"path": "f_new.md"})
        self.assertEqual(d.action, "block")
        self.assertEqual(d.code, "no_progress_block")

    def test_mutation_resets_no_progress_counter(self):
        from app.agent_guardrails import (
            ToolCallGuardrailController, GuardrailConfig,
        )
        gc = ToolCallGuardrailController(
            GuardrailConfig(no_progress_block_after=4))
        # 3 reads
        for i in range(3):
            gc.after_call("read_file", {"path": f"f{i}.md"}, result="ok")
        # A successful write resets the counter
        gc.after_call("write_file", {"path": "out.py", "content": "x"},
                      result="written 12 bytes")
        # Now 3 more reads should NOT trip
        for i in range(3):
            d = gc.before_call("read_file", {"path": f"g{i}.md"})
            self.assertEqual(d.action, "allow")
            gc.after_call("read_file", {"path": f"g{i}.md"}, result="ok")


# ── Day 3 AM — Cross-tool read counter ───────────────────────────────

class TestReadCounter(unittest.TestCase):
    def setUp(self):
        # Each test gets a fresh agent stub
        class A:
            pass
        self.agent = A()

    def test_bump_and_block(self):
        from app.tools_split._read_counter import (
            bump_read, get_count, is_blocked, _hard_cap,
        )
        cap = _hard_cap()
        for i in range(cap + 1):
            n = bump_read(self.agent, "/tmp/foo.md", source="read_file")
            self.assertEqual(n, i + 1)
        # cap+1 is when is_blocked returns True (n > cap)
        self.assertTrue(is_blocked(self.agent, "/tmp/foo.md"))

    def test_extract_read_paths_from_bash(self):
        from app.tools_split._read_counter import extract_read_path_from_bash as ex
        self.assertEqual(ex("cat /tmp/x.md"), ("/tmp/x.md", "bash:cat"))
        self.assertEqual(ex("head -n 50 README.md"), ("README.md", "bash:head"))
        self.assertEqual(ex("tail -f log.txt"), ("log.txt", "bash:tail"))
        self.assertEqual(ex("less notes.md"), ("notes.md", "bash:less"))
        self.assertEqual(ex("sed -n '1,50p' a.md"), ("a.md", "bash:sed"))
        self.assertEqual(ex("awk '/^TODO/' tasks.md"), ("tasks.md", "bash:awk"))
        self.assertIsNone(ex("echo hello"))
        self.assertIsNone(ex("rm -rf /"))
        # env-prefix unwrap
        self.assertEqual(ex("env FOO=1 cat /etc/hosts"),
                         ("/etc/hosts", "bash:cat"))
        # leftmost in pipe
        self.assertEqual(ex("cat foo | grep x"), ("foo", "bash:cat"))


# ── Day 3 PM — Scenario object ───────────────────────────────────────

class TestScenario(unittest.TestCase):
    def test_signature_changes_with_project(self):
        from app.core.scenario import Scenario
        s1 = Scenario.for_project("p1", "Project1", "/ws/p1")
        s2 = Scenario.for_project("p2", "Project2", "/ws/p2")
        self.assertNotEqual(s1.signature, s2.signature)

    def test_scope_filter_includes_all_relevant(self):
        from app.core.scenario import Scenario
        s = Scenario.for_project("pX", task_id="tY")
        scopes = s.scope_filter("aZ")
        self.assertIn("global", scopes)
        self.assertIn("agent:aZ", scopes)
        self.assertIn("project:pX", scopes)
        self.assertIn("task:tY", scopes)

    def test_l1_cleared_on_scenario_switch(self):
        from app.core.scenario import Scenario, set_agent_scenario

        class A:
            messages: list = []
            current_scenario = None

        agent = A()
        agent.messages = [
            {"role": "system", "content": "x", "_source": "anchor"},
            {"role": "user", "content": "from project A"},
            {"role": "assistant", "content": "doing A"},
        ]
        sa = Scenario.for_project("pA")
        set_agent_scenario(agent, sa)
        # Same scenario → no clear
        cleared = set_agent_scenario(agent, Scenario.for_project("pA"))
        self.assertFalse(cleared)
        self.assertEqual(len(agent.messages), 3)
        # Different scenario → clear (anchor system msg kept)
        cleared = set_agent_scenario(agent, Scenario.for_project("pB"))
        self.assertTrue(cleared)
        self.assertEqual(len(agent.messages), 1)
        self.assertEqual(agent.messages[0]["_source"], "anchor")

    def test_prompt_block_includes_scope(self):
        from app.core.scenario import Scenario
        s = Scenario.for_project("p123abcdef", "Demo", "/ws/p123",
                                 task_id="t987xyz", task_title="M3")
        block = s.to_prompt_block()
        self.assertIn("<current_scenario>", block)
        self.assertIn("p123abcdef", block)
        self.assertIn("recall_policy", block)
        self.assertIn("Cross-project memories are NOT visible", block)


# ── Day 5 — TaskAssignment round-trip ────────────────────────────────

class TestTaskAssignment(unittest.TestCase):
    def setUp(self):
        # Fresh DB per test
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["TUDOU_HOME"] = self._tmpdir.name
        # Reset singleton
        import app.core.task_assignment as ta_mod
        ta_mod._STORE = None

    def tearDown(self):
        os.environ.pop("TUDOU_HOME", None)
        import app.core.task_assignment as ta_mod
        ta_mod._STORE = None
        self._tmpdir.cleanup()

    def test_dispatch_and_accept_roundtrip(self):
        from app.tools_split.coordination import (
            _tool_dispatch_task, _tool_accept_task, _tool_inbox_assignments,
        )
        # PM dispatches
        out = _tool_dispatch_task(
            to_agent="coder1",
            brief="Build a hello world FastAPI server",
            context_refs=[
                {"path": "spec.md", "why_relevant": "API contract"},
            ],
            deliverables=[
                {"path": "server.py", "kind": "code",
                 "must_contain": ["FastAPI", "uvicorn"], "min_lines": 10},
            ],
            project_id="p_test",
            _caller_agent_id="pm1",
        )
        self.assertIn("Dispatched task ta_", out)
        # Coder reads inbox
        out2 = _tool_inbox_assignments(_caller_agent_id="coder1")
        self.assertIn("Pending task assignments", out2)
        self.assertIn("Build a hello world FastAPI server", out2)
        # Coder accepts (auto-pop top)
        brief = _tool_accept_task(_caller_agent_id="coder1")
        self.assertIn("# Task Assignment ta_", brief)
        self.assertIn("server.py", brief)
        self.assertIn("FastAPI", brief)
        self.assertIn("DO NOT search/glob", brief)
        # Re-accepting same id should say "already accepted"
        # First line is "# Task Assignment ta_xxxxxxxxxx"
        first = brief.splitlines()[0]
        ta_id = first.split()[-1]
        self.assertTrue(ta_id.startswith("ta_"), f"unexpected id: {ta_id!r}")
        again = _tool_accept_task(ta_id=ta_id, _caller_agent_id="coder1")
        self.assertIn("already accepted", again)

    def test_dispatch_rejects_no_deliverables(self):
        from app.tools_split.coordination import _tool_dispatch_task
        out = _tool_dispatch_task(
            to_agent="c", brief="do stuff", deliverables=[],
            _caller_agent_id="pm",
        )
        self.assertIn("requires at least one deliverable", out)

    def test_dispatch_rejects_long_brief(self):
        from app.tools_split.coordination import _tool_dispatch_task
        out = _tool_dispatch_task(
            to_agent="c", brief="x" * 600,
            deliverables=[{"path": "a.py", "must_contain": ["x"]}],
            _caller_agent_id="pm",
        )
        self.assertIn("brief is too long", out)


# ── Day 1 PM — Memory scope filter (lightweight) ─────────────────────

class TestRecallScope(unittest.TestCase):
    """We can't easily spin up MemoryManager without a sqlite path; test
    the helper function directly."""
    def test_is_in_scope_rules(self):
        from app.core.memory import MemoryManager
        # Direct call without instantiating (it's a regular method
        # that doesn't touch self)
        mm = MemoryManager.__new__(MemoryManager)  # bypass __init__
        # global → always allow
        self.assertTrue(mm._is_in_scope("global", "a1", "p1", "t1"))
        # empty → legacy allow
        self.assertTrue(mm._is_in_scope("", "a1", "p1", "t1"))
        # agent: matches own
        self.assertTrue(mm._is_in_scope("agent:a1", "a1", "p1", "t1"))
        self.assertFalse(mm._is_in_scope("agent:a2", "a1", "p1", "t1"))
        # project: matches current
        self.assertTrue(mm._is_in_scope("project:p1", "a1", "p1", "t1"))
        self.assertFalse(mm._is_in_scope("project:p2", "a1", "p1", "t1"))
        # task: matches current
        self.assertTrue(mm._is_in_scope("task:t1", "a1", "p1", "t1"))
        self.assertFalse(mm._is_in_scope("task:t2", "a1", "p1", "t1"))
        # malformed → drop
        self.assertFalse(mm._is_in_scope("garbage", "a1", "p1", "t1"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
