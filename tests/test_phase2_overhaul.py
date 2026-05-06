"""Phase 2 unit tests — covers P2-1 through P2-7.

Stand-alone (no LLM, no preview server). Tests run in <1s.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── P2-1 — agent_runner status JSON intercept ────────────────────────

class TestAgentRunner(unittest.TestCase):
    def test_parse_status_extracts_json(self):
        from app.core.agent_runner import parse_status
        text = '{"done":["read foo"], "current":"writing", "next":"test", "budget_remaining":3, "blocked_by":""}\nMore text.'
        s = parse_status(text)
        self.assertIsNotNone(s)
        self.assertEqual(s["current"], "writing")
        self.assertEqual(s["budget_remaining"], 3)

    def test_parse_status_extracts_from_code_fence(self):
        from app.core.agent_runner import parse_status
        text = '```json\n{"done":[], "current":"x", "next":"y", "budget_remaining":1, "blocked_by":""}\n```\nReady to continue.'
        s = parse_status(text)
        self.assertIsNotNone(s)
        self.assertEqual(s["current"], "x")

    def test_parse_status_returns_none_on_normal_text(self):
        from app.core.agent_runner import parse_status
        self.assertIsNone(parse_status("This is the final answer to your question."))
        self.assertIsNone(parse_status('{"foo": "bar"}'))  # not enough status keys

    def test_render_status_strips_json(self):
        from app.core.agent_runner import render_status_for_user
        text = '{"done":[], "current":"reading", "next":"write", "budget_remaining":1, "blocked_by":""}\nNext I will write the file.'
        rendered = render_status_for_user(text)
        self.assertNotIn('"done"', rendered)
        self.assertIn("Next I will write", rendered)

    def test_render_status_synthesizes_when_no_human_text(self):
        from app.core.agent_runner import render_status_for_user
        text = '{"done":[], "current":"reading", "next":"write", "budget_remaining":1, "blocked_by":""}'
        rendered = render_status_for_user(text)
        self.assertIn("still working", rendered)

    def test_auto_continue_loops_until_final(self):
        from app.core.agent_runner import run_with_auto_continue
        # Fake chat fn that emits 2 status JSONs then a final answer
        responses = [
            '{"done":["a"],"current":"b","next":"c","budget_remaining":2,"blocked_by":""}',
            '{"done":["a","b"],"current":"c","next":"d","budget_remaining":1,"blocked_by":""}',
            "All done — here's the result.",
        ]
        calls = []
        def fake(prompt):
            calls.append(prompt)
            return responses[len(calls) - 1]
        result = run_with_auto_continue(fake, "do the thing", max_rounds=5)
        self.assertEqual(result, "All done — here's the result.")
        self.assertEqual(len(calls), 3)
        # First call uses initial prompt; subsequent use auto-continue
        self.assertEqual(calls[0], "do the thing")
        self.assertIn("auto-continue", calls[1])

    def test_auto_continue_stops_on_blocked(self):
        from app.core.agent_runner import run_with_auto_continue
        def fake(prompt):
            return '{"done":[],"current":"x","next":"y","budget_remaining":3,"blocked_by":"need user input"}'
        result = run_with_auto_continue(fake, "go", max_rounds=5)
        self.assertIn("need user input", result.lower())


# ── P2-3 — Role gating in system prompt ──────────────────────────────

class TestRoleGating(unittest.TestCase):
    def test_pm_role_gets_dispatch_guidance(self):
        from app.system_prompt import build_handoff_role_block
        block = build_handoff_role_block("pm", use_zh=False)
        self.assertIn("dispatch_task", block)
        self.assertIn("DO NOT", block)
        # Chinese version
        block_zh = build_handoff_role_block("ceo", use_zh=True)
        self.assertIn("dispatch_task", block_zh)
        self.assertIn("禁止", block_zh)

    def test_coder_role_gets_accept_guidance(self):
        from app.system_prompt import build_handoff_role_block
        block = build_handoff_role_block("coder")
        self.assertIn("inbox_assignments", block)
        self.assertIn("accept_task", block)
        self.assertIn("read ONLY", block)

    def test_unknown_role_gets_empty(self):
        from app.system_prompt import build_handoff_role_block
        self.assertEqual("", build_handoff_role_block("unicornwrangler"))
        self.assertEqual("", build_handoff_role_block(""))

    def test_pm_substring_match(self):
        # "product manager" matches "pm" substring? No — we check token-style
        # Actually our impl uses 'in' over token strings, so "manager" in
        # "product manager" matches.
        from app.system_prompt import build_handoff_role_block
        block = build_handoff_role_block("product manager")
        self.assertIn("dispatch_task", block)


# ── P2-4 — acceptance_cmd whitelist ──────────────────────────────────

class TestAcceptanceCmdWhitelist(unittest.TestCase):
    def test_safe_cmds_allowed(self):
        from app.core.deliverable_check import _acceptance_cmd_allowed
        for c in ("pytest tests/foo.py", "npm test", "go test ./...",
                  "make check", "ruff check .", "tsc --noEmit"):
            self.assertTrue(_acceptance_cmd_allowed(c), msg=f"{c!r}")

    def test_unsafe_cmds_rejected(self):
        from app.core.deliverable_check import _acceptance_cmd_allowed
        for c in ("rm -rf /", "curl http://evil", "pytest && rm",
                  "pytest; whoami", "/bin/sh -c rm",
                  "ENV=x pytest", "-help",
                  "cat /etc/passwd", "echo test", ""):
            self.assertFalse(_acceptance_cmd_allowed(c), msg=f"{c!r}")


# ── P2-5 — Watcher detection rules ───────────────────────────────────

class TestWatcher(unittest.TestCase):
    def test_no_progress_soft_nudge_at_threshold(self):
        from app.core.watcher import ProjectWatcher, WatcherThresholds
        nudges = []
        w = ProjectWatcher(
            project_id="p1",
            thresholds=WatcherThresholds(
                poll_interval=999, no_progress_soft_after=2,
                no_progress_notify_pm_after=999, no_progress_escalate_after=999),
            send_to_agent=lambda aid, msg: nudges.append((aid, msg)),
        )
        w.begin_agent_run("a1", task_id="t1")
        # Backdate progress
        w._stats["a1"].last_progress_at = time.time() - 10
        emitted = w.poll_once()
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["kind"], "soft_nudge")
        self.assertEqual(len(nudges), 1)
        self.assertEqual(nudges[0][0], "a1")
        self.assertIn("stuck", nudges[0][1].lower())

    def test_high_read_ratio_triggers_nudge(self):
        from app.core.watcher import ProjectWatcher, WatcherThresholds
        nudges = []
        w = ProjectWatcher(
            project_id="p1",
            thresholds=WatcherThresholds(
                poll_interval=999, no_progress_soft_after=999,
                no_progress_notify_pm_after=999, no_progress_escalate_after=999,
                read_write_ratio_threshold=4.0,
                read_write_ratio_min_samples=5),
            send_to_agent=lambda aid, msg: nudges.append((aid, msg)),
        )
        w.begin_agent_run("a1")
        for _ in range(10):
            w.record_tool_call("a1", "read_file")
        emitted = w.poll_once()
        self.assertEqual(emitted[0]["kind"], "soft_nudge")

    def test_write_resets_read_counter(self):
        from app.core.watcher import ProjectWatcher
        w = ProjectWatcher(project_id="p1")
        w.begin_agent_run("a1")
        for _ in range(10):
            w.record_tool_call("a1", "read_file")
        self.assertEqual(w._stats["a1"].read_count, 10)
        w.record_tool_call("a1", "write_file", succeeded=True)
        self.assertEqual(w._stats["a1"].read_count, 0)

    def test_intervention_throttling(self):
        from app.core.watcher import ProjectWatcher, WatcherThresholds
        nudges = []
        w = ProjectWatcher(
            project_id="p1",
            thresholds=WatcherThresholds(
                poll_interval=10, no_progress_soft_after=1,
                no_progress_notify_pm_after=999, no_progress_escalate_after=999),
            send_to_agent=lambda aid, msg: nudges.append((aid, msg)),
        )
        w.begin_agent_run("a1")
        w._stats["a1"].last_progress_at = time.time() - 100
        emitted_first = w.poll_once()
        emitted_second = w.poll_once()  # throttled
        self.assertEqual(len(emitted_first), 1)
        self.assertEqual(len(emitted_second), 0)


# ── P2-6 — Team Dashboard ────────────────────────────────────────────

class TestTeamDashboard(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        os.environ["TUDOU_HOME"] = self._tmp.name
        import app.core.team_dashboard as _td
        _td._DASH = None

    def tearDown(self):
        os.environ.pop("TUDOU_HOME", None)
        import app.core.team_dashboard as _td
        _td._DASH = None
        self._tmp.cleanup()

    def test_update_then_query(self):
        from app.core.team_dashboard import update_status, get_dashboard
        update_status("a1", "p1", task_id="t1", task_title="M3",
                      current_action="reading", scenario_kind="project")
        update_status("a2", "p1", task_id="t2",
                      current_action="writing", scenario_kind="project")
        rows = get_dashboard().query_project("p1")
        self.assertEqual(len(rows), 2)
        agents = {r["agent_id"] for r in rows}
        self.assertEqual(agents, {"a1", "a2"})

    def test_query_team_status_tool(self):
        from app.core.team_dashboard import update_status
        from app.tools_split.coordination import _tool_query_team_status
        update_status("a1", "p_demo", current_action="reading")
        out = _tool_query_team_status(project_id="p_demo")
        self.assertIn("Team status", out)
        self.assertIn("a1", out)

    def test_query_agent_status_tool(self):
        from app.core.team_dashboard import update_status
        from app.tools_split.coordination import _tool_query_agent_status
        update_status("aX", "pX", task_id="tZ", task_title="M5",
                      current_action="testing")
        out = _tool_query_agent_status(agent_id="aX", project_id="pX")
        self.assertIn("aX", out)
        self.assertIn("testing", out)

    def test_progress_resets_timer(self):
        from app.core.team_dashboard import get_dashboard, update_status
        update_status("a1", "p1", current_action="step 1")
        first = get_dashboard().query_agent("a1", "p1")
        first_lpa = first["last_progress_at"]
        time.sleep(0.05)
        update_status("a1", "p1", current_action="step 2", progress=True)
        second = get_dashboard().query_agent("a1", "p1")
        self.assertGreater(second["last_progress_at"], first_lpa)


# ── P2-7 — Workflow contract present ─────────────────────────────────

class TestWorkflowContractsFilled(unittest.TestCase):
    def test_product_dev_steps_have_contracts(self):
        from app.data.workflow_catalog import WORKFLOW_CATALOG
        product = next((t for t in WORKFLOW_CATALOG if t["id"] == "catalog_product_dev"), None)
        self.assertIsNotNone(product)
        steps_by_id = {s["id"]: s for s in product["steps"]}
        for sid in ("s_architecture", "s_development", "s_testing"):
            st = steps_by_id[sid]
            self.assertTrue(st.get("output_files"), f"{sid} missing output_files")
            self.assertTrue(st.get("must_contain"), f"{sid} missing must_contain")
            self.assertGreater(st.get("min_lines", 0), 0,
                               f"{sid} missing min_lines")

    def test_step_template_carries_contract_through_round_trip(self):
        from app.workflow import StepTemplate
        from app.data.workflow_catalog import WORKFLOW_CATALOG
        product = next(t for t in WORKFLOW_CATALOG if t["id"] == "catalog_product_dev")
        step_dict = next(s for s in product["steps"] if s["id"] == "s_development")
        st = StepTemplate.from_dict(step_dict)
        self.assertEqual(st.output_files, ["development-report.md"])
        self.assertGreater(len(st.must_contain), 0)
        # Round-trip
        d2 = st.to_dict()
        st2 = StepTemplate.from_dict(d2)
        self.assertEqual(st2.must_contain, st.must_contain)


if __name__ == "__main__":
    unittest.main(verbosity=2)
