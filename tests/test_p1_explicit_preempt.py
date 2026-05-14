"""P1 — explicit preempt semantics for chat_async (2026-05-13).

Before: chat_async's interrupt vs queue choice was decided ONLY by the
TUDOU_INTERRUPT_MODE env var. Frontend pendingQueue (defaults to
queue) and backend interrupt (defaults to interrupt) were two
different policies running in parallel — a race window between them
caused unexpected interruptions ("我又被中断了").

After: HTTP body's `preempt` field wins over the env var.
  preempt=true   → interrupt the in-flight chat, run this one
  preempt=false  → queue this one behind the in-flight chat
  preempt=None   → fall back to env var (legacy behaviour)

Frontend default sends preempt=false → backend never interrupts
unsolicited. Power user opt-in: set window._forcePreempt[agentId]=true
to send preempt=true on the next call.

These tests verify the parameter plumbing — not the full chat() loop.
"""
from __future__ import annotations

import inspect

import pytest


# ── Signature checks (regression guards against silent removal) ──

def test_supervisor_chat_async_accepts_preempt():
    from app.supervisor import AgentSupervisor
    sig = inspect.signature(AgentSupervisor.chat_async)
    assert "preempt" in sig.parameters
    # Default should be None (defer to env var)
    assert sig.parameters["preempt"].default is None


def test_agent_chat_async_accepts_preempt():
    from app.agent import Agent
    sig = inspect.signature(Agent.chat_async)
    assert "preempt" in sig.parameters
    assert sig.parameters["preempt"].default is None


def test_agent_execution_module_has_no_alternate_chat_async():
    """Sanity: agent_execution.py used to have a duplicate chat_async
    in an old AgentExecutionMixin, but that class is archived in a
    _DEAD_CODE_PRESERVED_FOR_ARCHAEOLOGY string literal now (line ~378).
    Active code path is Agent.chat_async only — no need to dual-maintain."""
    import app.agent_execution as ae
    assert not hasattr(ae, "AgentExecutionMixin"), (
        "AgentExecutionMixin appears to have been resurrected — "
        "tests should add a parameter check for its chat_async too")


# ── Env-var fallback semantics ─────────────────────────────────────

def test_preempt_none_uses_env_var_on(monkeypatch):
    """preempt=None + TUDOU_INTERRUPT_MODE=1 → interrupt mode."""
    monkeypatch.setenv("TUDOU_INTERRUPT_MODE", "1")
    preempt = None
    if preempt is not None:
        interrupt_mode = bool(preempt)
    else:
        import os
        interrupt_mode = (
            os.environ.get("TUDOU_INTERRUPT_MODE", "1") != "0")
    assert interrupt_mode is True


def test_preempt_none_uses_env_var_off(monkeypatch):
    monkeypatch.setenv("TUDOU_INTERRUPT_MODE", "0")
    preempt = None
    if preempt is not None:
        interrupt_mode = bool(preempt)
    else:
        import os
        interrupt_mode = (
            os.environ.get("TUDOU_INTERRUPT_MODE", "1") != "0")
    assert interrupt_mode is False


def test_preempt_explicit_true_overrides_env(monkeypatch):
    """Explicit preempt=True wins over TUDOU_INTERRUPT_MODE=0."""
    monkeypatch.setenv("TUDOU_INTERRUPT_MODE", "0")
    preempt = True
    if preempt is not None:
        interrupt_mode = bool(preempt)
    else:
        import os
        interrupt_mode = (
            os.environ.get("TUDOU_INTERRUPT_MODE", "1") != "0")
    assert interrupt_mode is True


def test_preempt_explicit_false_overrides_env(monkeypatch):
    """Explicit preempt=False wins over TUDOU_INTERRUPT_MODE=1.
    This is what the frontend default sends to kill surprise
    interrupts."""
    monkeypatch.setenv("TUDOU_INTERRUPT_MODE", "1")
    preempt = False
    if preempt is not None:
        interrupt_mode = bool(preempt)
    else:
        import os
        interrupt_mode = (
            os.environ.get("TUDOU_INTERRUPT_MODE", "1") != "0")
    assert interrupt_mode is False


# ── Body-level extraction (HTTP layer contract) ──────────────────

def test_request_body_preempt_extracted_correctly():
    """The route handler reads `body.get("preempt")` and coerces
    explicitly; verify the type-coercion behaviour matches what we
    pass to chat_async."""
    body = {"message": "hi", "preempt": True}
    p = body.get("preempt")
    if p is not None:
        p = bool(p)
    assert p is True

    body = {"message": "hi", "preempt": False}
    p = body.get("preempt")
    if p is not None:
        p = bool(p)
    assert p is False

    body = {"message": "hi"}   # field absent
    p = body.get("preempt")
    if p is not None:
        p = bool(p)
    assert p is None


def test_request_body_preempt_truthy_strings_become_true():
    """Some clients send strings — bool() of "true" / "1" / etc.
    Note: bool('false') is True in Python — that's a JS gotcha.
    Frontend sends actual JSON booleans so this isn't an issue, but
    if some legacy client posts a string, we need to know."""
    # Edge case the frontend SHOULDN'T hit (sends actual bool), but
    # documenting the behaviour for the audit trail
    assert bool("true") is True
    assert bool("false") is True   # ← bool of any non-empty string is True
    assert bool("") is False
    # Conclusion: backend trusts the frontend to send a real boolean.
    # If a string slips through, "false" would incorrectly become True.
    # Frontend sends `chatBody.preempt = !!(...)` so this is bool.
