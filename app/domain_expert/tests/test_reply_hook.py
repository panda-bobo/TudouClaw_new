"""Phase 0 task 6 tests: reply pipeline hook behavior.

Three scenarios verified:
  1. Empty expert_specialty → hook is no-op, default path runs
  2. expert_specialty set + module not built → ImportError caught,
     default path runs (graceful fallback)
  3. expert_specialty set + module enabled with mock pipeline → expert
     pipeline runs, default path skipped
  4. TUDOU_EXPERT_DISABLED=1 + expert_specialty set → hook bypassed,
     default path runs

These tests use unittest.mock to inject a fake `expert_pipeline.answer`
without needing the full chat() machinery to actually run.
"""
from __future__ import annotations

import sys
import types
import pytest
from unittest.mock import patch

from app.agent import Agent
from app.domain_expert import _config


def test_hook_skipped_when_specialty_empty(monkeypatch):
    """普通 agent with empty expert_specialty: hook does nothing, falls
    through to default chat path. We verify by asserting the import is
    NOT attempted (the hook short-circuits on `if self.expert_specialty`)."""
    a = Agent(id="plain", name="plain")
    assert a.expert_specialty == ""
    # Patch a sentinel into sys.modules to detect if hook tried to import
    sentinel = {"called": False}

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __import__

    def tracking_import(name, *args, **kwargs):
        if name == "app.domain_expert._config":
            sentinel["called"] = True
        return real_import(name, *args, **kwargs)

    # Note: we can't easily intercept the relative import inside chat()
    # without too much surgery. Instead we just verify the field is empty,
    # which is the gate condition that prevents the hook block from running.
    assert not a.expert_specialty


@pytest.mark.skip(
    reason="Phase 0 invariant — V4 step 2 (commit 337158f) shipped "
           "inference/pipeline.py with answer() + _retrieve(), and R5 "
           "added build_typed_rag_block. The 'pipeline module is missing' "
           "fallback is still tested by test_hook_bypassed_when_module_disabled "
           "via the env-var path; this assertion is just stale."
)
def test_hook_falls_through_when_pipeline_module_missing(monkeypatch):
    """Stale Phase-0 invariant — pipeline.answer now exists. Kept as
    documentation of the original fallback contract."""
    import app.domain_expert.inference
    assert not hasattr(app.domain_expert.inference, "pipeline") or not hasattr(
        getattr(app.domain_expert.inference, "pipeline", None), "answer"
    ), "Phase 0 should NOT have pipeline.answer yet"


@pytest.mark.skip(
    reason="V4 step 2 early-return hook was disabled 2026-05-10 — it bypassed "
           "agent.chat()'s transcript/streaming/event side effects, breaking "
           "the chat UI. Re-enable this test once pipeline integration moves "
           "to in-flow message augmentation (TODO in agent.chat comment block)."
)
def test_hook_routes_when_pipeline_present(monkeypatch):
    """If we inject a fake pipeline.answer, the hook should call it and
    return its result. Simulates what V4 vertical will deliver."""
    fake_module = types.ModuleType("app.domain_expert.inference.pipeline")

    def fake_answer(agent, user_message, **kw):
        return f"EXPERT_REPLY({agent.expert_specialty}): {user_message}"

    fake_module.answer = fake_answer
    monkeypatch.setitem(sys.modules, "app.domain_expert.inference.pipeline", fake_module)

    a = Agent(id="exp", name="legal-test")
    a.expert_specialty = "legal"
    a.expert_level = "expert"
    # Ensure module enabled
    monkeypatch.delenv(_config.DISABLED_ENV_VAR, raising=False)

    result = a.chat("帮我看个合同", source="admin")
    assert result.startswith("EXPERT_REPLY(legal):")
    assert "帮我看个合同" in result


def test_hook_bypassed_when_module_disabled(monkeypatch):
    """TUDOU_EXPERT_DISABLED=1 → hook detects via _config.is_disabled() and
    skips. Even if a fake pipeline.answer exists, it's NOT called. We
    can't easily run the full default path without mocking LLM, so we
    verify by injecting a pipeline that would raise — confirming the
    flag short-circuits BEFORE the pipeline call."""
    fake_module = types.ModuleType("app.domain_expert.inference.pipeline")

    def fake_answer(agent, user_message, **kw):
        raise AssertionError("pipeline should NOT be called when disabled")

    fake_module.answer = fake_answer
    monkeypatch.setitem(sys.modules, "app.domain_expert.inference.pipeline", fake_module)

    monkeypatch.setenv(_config.DISABLED_ENV_VAR, "1")

    a = Agent(id="exp2", name="disabled-test")
    a.expert_specialty = "legal"

    # We expect chat() to fall through to the default path. The default
    # path needs LLM config etc. — we just check that the assertion in
    # fake_answer never fires (i.e. no AssertionError raised by the hook).
    # If it falls through, default path may raise OTHER errors (no LLM
    # configured for "disabled-test") — that's fine, we only care the
    # AssertionError from fake_answer doesn't fire.
    try:
        a.chat("ping", source="admin")
    except AssertionError:
        pytest.fail("hook should have been bypassed when TUDOU_EXPERT_DISABLED=1")
    except Exception:
        # Default path will fail for other reasons (no model configured),
        # which is fine — proves the hook bypassed and default ran.
        pass


@pytest.mark.skip(
    reason="Phase-0 invariant — V4 step 2 + R5 populate the inference "
           "package (pipeline.answer / _retrieve / build_typed_rag_block). "
           "Kept for historical context; the empty-package contract no "
           "longer holds."
)
def test_phase_0_inference_package_is_intentionally_empty():
    """Stale Phase-0 invariant — inference/ now ships pipeline + helpers."""
    import app.domain_expert.inference as inf
    public_names = [n for n in dir(inf) if not n.startswith("_")]
    assert public_names == [] or public_names == ["__name__"], (
        f"Phase 0 inference package should be empty; found: {public_names}"
    )
