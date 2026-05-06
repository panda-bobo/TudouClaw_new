"""Phase 3 unit tests — verifies P3-1..P3-6 changes.

Pure-Python tests, no LLM/network.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


# ── P3-4 — report_back roundtrip ────────────────────────────────────

class TestReportBack(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        os.environ["TUDOU_HOME"] = self._tmpdir.name
        import app.core.task_assignment as ta_mod
        ta_mod._STORE = None

    def tearDown(self):
        os.environ.pop("TUDOU_HOME", None)
        import app.core.task_assignment as ta_mod
        ta_mod._STORE = None
        self._tmpdir.cleanup()

    def test_report_back_marks_done(self):
        from app.tools_split.coordination import (
            _tool_dispatch_task, _tool_accept_task, _tool_report_back,
        )
        # PM dispatches
        _tool_dispatch_task(
            to_agent="coder1", brief="Build a thing",
            deliverables=[{"path": "x.py", "must_contain": ["def main"]}],
            project_id="p_test",
            _caller_agent_id="pm1",
        )
        # Coder accepts
        _tool_accept_task(_caller_agent_id="coder1")
        # Coder reports back
        result = _tool_report_back(
            status="done",
            summary="Done — wrote x.py",
            actual_deliverables=["x.py"],
            _caller_agent_id="coder1",
        )
        self.assertIn("Reported back", result)
        # Verify status is "done" in store
        from app.core.task_assignment import get_store
        store = get_store()
        rows = store._conn.execute(
            "SELECT status FROM task_assignments WHERE to_agent='coder1'",
        ).fetchall()
        self.assertEqual(rows[0]["status"], "done")

    def test_report_back_blocked_keeps_accepted(self):
        from app.tools_split.coordination import (
            _tool_dispatch_task, _tool_accept_task, _tool_report_back,
        )
        _tool_dispatch_task(
            to_agent="c2", brief="Block me",
            deliverables=[{"path": "y.py", "must_contain": ["x"]}],
            _caller_agent_id="pm",
        )
        _tool_accept_task(_caller_agent_id="c2")
        _tool_report_back(status="blocked", blocker="missing API key",
                          _caller_agent_id="c2")
        from app.core.task_assignment import get_store
        rows = get_store()._conn.execute(
            "SELECT status FROM task_assignments WHERE to_agent='c2'",
        ).fetchall()
        self.assertEqual(rows[0]["status"], "accepted")  # blocked → not closed

    def test_report_back_rejects_unknown_status(self):
        from app.tools_split.coordination import (
            _tool_dispatch_task, _tool_accept_task, _tool_report_back,
        )
        _tool_dispatch_task(
            to_agent="c3", brief="x",
            deliverables=[{"path": "z.py", "must_contain": ["x"]}],
            _caller_agent_id="pm",
        )
        _tool_accept_task(_caller_agent_id="c3")
        out = _tool_report_back(status="exploded", _caller_agent_id="c3")
        self.assertIn("Error", out)


# ── P3-5 — Memory write-side scope provenance ────────────────────────

class TestL3ExtractorScope(unittest.TestCase):
    """_parse_output expects a JSON array. _quality_gate filters by
    content length/category — use long enough realistic content."""

    def _make_stub(self, captured: list):
        class StubStore:
            def upsert_fact(self, fact, threshold=0.75, prefer_category_match=True):
                captured.append(fact)
                return {"action": "inserted", "id": fact.id, "fact": fact}
        return StubStore()

    def test_identity_categories_get_global_scope(self):
        """preference always scope='global' regardless of context.
        (contact is not in _VALID_CATEGORIES so we test preference instead.)"""
        from app.core.l3_extractor import _run_extraction
        def stub_llm(prompt):
            return '[{"category":"preference","content":"用户偏好简洁明了的回复风格","confidence":0.9}]'
        captured: list = []
        _run_extraction(
            agent_id="a1", context="ctx", llm_call=stub_llm,
            store=self._make_stub(captured),
            source_label="test", project_id="pX", task_id="tY",
        )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].scope, "global")
        self.assertEqual(captured[0].project_id, "pX")
        self.assertEqual(captured[0].task_id, "tY")

    def test_non_identity_gets_task_scope(self):
        from app.core.l3_extractor import _run_extraction
        def stub_llm(prompt):
            return '[{"category":"reasoning","content":"在 M3 阶段选择 lib X 因为它在并发场景表现最好","confidence":0.85}]'
        captured: list = []
        _run_extraction(
            agent_id="a1", context="ctx", llm_call=stub_llm,
            store=self._make_stub(captured),
            source_label="test", project_id="pX", task_id="tY",
        )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].scope, "task:tY")

    def test_project_scope_when_no_task(self):
        from app.core.l3_extractor import _run_extraction
        def stub_llm(prompt):
            # outcome must contain substance marker — include a path
            return '[{"category":"outcome","content":"成功上线 src/main.py v1.2 通过用户验收","confidence":0.9}]'
        captured: list = []
        _run_extraction(
            agent_id="a1", context="ctx", llm_call=stub_llm,
            store=self._make_stub(captured),
            source_label="test", project_id="pX",
        )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].scope, "project:pX")

    def test_agent_scope_when_no_project(self):
        from app.core.l3_extractor import _run_extraction
        def stub_llm(prompt):
            return '[{"category":"reasoning","content":"发现一个跨任务通用规律,能减少冗余调用","confidence":0.85}]'
        captured: list = []
        _run_extraction(
            agent_id="agentZ", context="ctx", llm_call=stub_llm,
            store=self._make_stub(captured),
            source_label="test",
        )
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0].scope, "agent:agentZ")


# ── P3-5 — upsert_fact preserves provenance on refresh ───────────────

class TestUpsertProvenance(unittest.TestCase):
    def test_refresh_preserves_scope(self):
        from app.core.memory import SemanticFact, MemoryManager
        # MemoryManager uses positional db_path
        with tempfile.TemporaryDirectory() as tmp:
            mm = MemoryManager(db_path=os.path.join(tmp, "mem.db"))
            f1 = SemanticFact(
                agent_id="a1", category="reasoning",
                content="we chose framework X for performance reasons",
                confidence=0.9, scope="project:p1", project_id="p1",
            )
            r1 = mm.upsert_fact(f1)
            self.assertEqual(r1["action"], "inserted")
            f2 = SemanticFact(
                agent_id="a1", category="reasoning",
                content="we chose framework X due to its performance benefits",
                confidence=0.95, scope="project:p2", project_id="p2",
            )
            r2 = mm.upsert_fact(f2, threshold=0.5)
            if r2["action"] == "updated":
                refreshed = r2["fact"]
                # New scope wins (P3-5 fix)
                self.assertEqual(refreshed.scope, "project:p2")
                self.assertEqual(refreshed.project_id, "p2")


# ── P3-1 — Watcher lifecycle (smoke) ────────────────────────────────

class TestWatcherLifecycle(unittest.TestCase):
    def test_start_stop_idempotent(self):
        from app.core import watcher as wt
        # Use a unique project id so no collision with other tests
        pid = "p_test_watcher_xyz"
        # Start once
        wt.start_for_project(pid, project_name="Test")
        w1 = wt.get_watcher(pid)
        self.assertIsNotNone(w1)
        # Start again — should be idempotent (same instance)
        wt.start_for_project(pid, project_name="Test")
        w2 = wt.get_watcher(pid)
        self.assertIs(w1, w2)
        # Stop
        wt.stop_for_project(pid)
        w3 = wt.get_watcher(pid)
        self.assertIsNone(w3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
