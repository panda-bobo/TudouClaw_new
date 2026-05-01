"""RAG-only hard route: chat-header toggle forces [knowledge_lookup] only.

Design:
  * Frontend chat-header "🔍 RAG" toggle sets chatBody.rag_only = true.
  * Backend handler stamps agent._rag_only_mode = bool(body.rag_only).
  * agent_llm._get_effective_tools short-circuits to [knowledge_lookup]
    when the flag is True.

Tests lock this contract so no future refactor silently reintroduces
bash/read_file during a kb-only turn.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

# Skipped: this test isolated AgentLLMMixin._get_effective_tools by
# binding it to a stub object via __get__. As of 2026-04-30, that mixin
# has been confirmed dead-in-production (Agent does not inherit from
# it) and the file is wrapped as archaeology. The production behavior
# is exercised by test_today_e2e.py instead.
pytest.skip(
    "AgentLLMMixin retired (2026-04-30); see app/agent_llm.py tombstone.",
    allow_module_level=True,
)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


def _harness(flag):
    """Minimal stand-in bound to the real _get_effective_tools method."""
    from app.agent_llm import AgentLLMMixin
    h = SimpleNamespace()
    h.profile = SimpleNamespace(allowed_tools=[], denied_tools=[])
    h._rag_only_mode = flag
    h._get_effective_tools = AgentLLMMixin._get_effective_tools.__get__(h)
    return h


def test_rag_only_flag_restricts_to_knowledge_lookup():
    h = _harness(True)
    tools_list = h._get_effective_tools()
    assert len(tools_list) == 1
    assert tools_list[0]["function"]["name"] == "knowledge_lookup"


def test_rag_only_off_returns_full_toolset():
    h = _harness(False)
    tools_list = h._get_effective_tools()
    names = {t["function"]["name"] for t in tools_list}
    assert "knowledge_lookup" in names
    # Sanity: the full set includes lots of other tools
    assert len(tools_list) > 5
    assert "bash" in names or "read_file" in names


def test_rag_only_missing_attribute_defaults_to_off():
    """If the flag was never set (legacy code path), behave as OFF."""
    from app.agent_llm import AgentLLMMixin
    h = SimpleNamespace()
    h.profile = SimpleNamespace(allowed_tools=[], denied_tools=[])
    # Deliberately NO _rag_only_mode attribute
    h._get_effective_tools = AgentLLMMixin._get_effective_tools.__get__(h)
    tools_list = h._get_effective_tools()
    names = {t["function"]["name"] for t in tools_list}
    assert len(tools_list) > 5
    assert "bash" in names or "read_file" in names


def test_rag_only_short_circuit_is_in_real_agent_implementation():
    """The Agent class overrides _get_effective_tools (agent_llm.py's
    mixin version gets shadowed). Make sure THIS override carries the
    RAG-only short circuit — otherwise the chat-header toggle is UI
    theater and the LLM sees all 40 tools anyway."""
    import inspect
    from app.agent import Agent
    src = inspect.getsource(Agent._get_effective_tools)
    assert "_rag_only_mode" in src, (
        "Agent._get_effective_tools must short-circuit on _rag_only_mode; "
        "otherwise the chat-header RAG toggle has no effect on the real agent.")
    assert "knowledge_lookup" in src


def test_normal_chat_turn_iteration_cap_unchanged():
    """Non-RAG-only turns keep the full 20-iteration budget. We
    intentionally DON'T cap RAG-only further at the iteration level
    — user feedback made clear that's not the right lever; the real
    fix is tool-list restriction in _get_effective_tools (so bash etc
    are literally not offered). This test just guards the budget."""
    import pathlib
    src = pathlib.Path(
        "/Users/pangwanchun/AIProjects/TudouClaw_new/app/agent.py").read_text()
    assert "max_iters = 20" in src, (
        "Default max_iters must remain 20 so normal agents aren't "
        "accidentally constrained.")


def test_rag_only_still_respects_denied_when_knowledge_lookup_denied():
    """Edge case: if knowledge_lookup is denied, don't lock the chat up
    — fall back to normal filtering. (The chat header should refuse to
    enable the toggle in the first place, but defend in depth.)"""
    from app.agent_llm import AgentLLMMixin
    h = SimpleNamespace()
    h.profile = SimpleNamespace(allowed_tools=[],
                                denied_tools=["knowledge_lookup"])
    h._rag_only_mode = True
    h._get_effective_tools = AgentLLMMixin._get_effective_tools.__get__(h)
    tools_list = h._get_effective_tools()
    # Fell back — knowledge_lookup itself is denied, so result is empty OR full-minus-denied.
    # The contract is just "don't crash and don't return [knowledge_lookup] after deny".
    names = [t["function"]["name"] for t in tools_list]
    assert "knowledge_lookup" not in names


# ── Handler wires flag onto agent ─────────────────────────────


def test_chat_handler_stamps_rag_only_flag():
    """The /chat handler must set agent._rag_only_mode every request,
    including False, so prior state can't leak across turns."""
    # Inline verification of the source — the handler imports and mutates
    # are hard to harness without the full FastAPI+hub setup.
    import app.api.routers.agents as _ar
    with open(_ar.__file__) as f:
        src = f.read()
    assert "_rag_only_mode = bool(body.get(\"rag_only\"" in src, \
        "handler must stamp agent._rag_only_mode from body.rag_only"
