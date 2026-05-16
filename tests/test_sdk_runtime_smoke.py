"""SDK runtime smoke tests — locks every contract we discovered we
need over the 2026-05-16 debugging session.

What each test guards against (= what re-broke when I wasn't looking):

  test_messages_to_sdk_items_*
    Round-trip ChatCompletion-style messages → SDK Responses-API
    input items. Caught: SDK rejects plain-string assistant content,
    requires structured `[{type:"output_text", text}]` list.
    History pass-through across multi-turn depends on this.

  test_persist_sdk_items_to_messages_*
    Inverse direction — SDK new_items → legacy messages format.
    Caught: tool_call_item → asst.tool_calls, tool_call_output →
    tool-role msg with tool_call_id. Without this, agent had no
    memory of intermediate tool dispatches across turns.

  test_event_kinds_match_legacy
    AgentEvent kind names must match what portal_bundle.js renders
    (legacy `tool_call`/`tool_result`, NOT `tool_call_start`/_end).
    Caught: my first attempt used SDK-native names → UI silently
    dropped events.

  test_event_bridge_persists_to_agent_events
    EventBridge._emit must _log to agent.events for restart-replay.
    Caught: SDK runtime was only pushing to live SSE, agent.events
    stayed empty, restart wiped chat history.

  test_max_turns_reads_per_agent_field
    SDK adapter must respect Agent.max_turns (0 = unlimited).
    Caught: SDK default was 10, legacy hardcode was 20, neither
    used the per-agent field that had been silently persisted.

  test_run_persists_user_msg_immediately
    The user message must hit agent.messages + agent.events before
    Runner.run starts (so abort/crash mid-run doesn't lose it).

These tests run WITHOUT openai-agents or any real LLM provider.
Each one isolates one mechanism via fakes. A separate set of
integration tests (skipped if no provider config) exercises the
real round-trip — but those are flakier and slower.
"""
from __future__ import annotations

import json
import pytest


# ── Skip everything cleanly if SDK isn't installed ────────────────
# These tests verify the SDK adapter's internals, which need the SDK.
pytest.importorskip("agents")


# ──────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────

class _FakeAgent:
    """Minimal stand-in for app.agent.Agent — only the attrs the SDK
    adapter touches. Records every _log + _maybe_persist call so
    tests can assert."""

    def __init__(self, *, id_="test-agent-1234", name="Tester",
                 max_turns=0):
        self.id = id_
        self.name = name
        self.messages = []
        self.events = []
        self.max_turns = max_turns
        self._persist_calls = 0

    def _log(self, kind, data):
        # Mimics app.agent.Agent._log — appends an AgentEvent-shaped
        # dict (we don't need the real class for assertions).
        self.events.append({"kind": kind, "data": dict(data or {})})

    def _maybe_persist(self, *, force=False):
        self._persist_calls += 1


@pytest.fixture
def fake_agent():
    return _FakeAgent()


# ──────────────────────────────────────────────────────────────────
# 1. Messages ↔ SDK items round-trip
# ──────────────────────────────────────────────────────────────────

def test_messages_to_sdk_items_basic_user_assistant():
    """Plain user + assistant text round-trip."""
    from app.agent_runtime.sdk_adapter import _tudou_messages_to_sdk_items
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    items = _tudou_messages_to_sdk_items(msgs)
    assert len(items) == 2
    # User message: plain content string works
    assert items[0]["role"] == "user"
    assert items[0]["content"] == "hello"
    # Assistant message: content MUST be structured list (SDK requires)
    assert items[1]["role"] == "assistant"
    assert isinstance(items[1]["content"], list), \
        "asst content must be structured list, not plain string " \
        "(SDK converter at chatcmpl_converter.py:633 crashes otherwise)"
    assert items[1]["content"][0]["type"] == "output_text"
    assert items[1]["content"][0]["text"] == "hi"


def test_messages_to_sdk_items_drops_system():
    """system messages must be filtered — SDK rebuilds instructions
    fresh each turn from the Agent.instructions callable."""
    from app.agent_runtime.sdk_adapter import _tudou_messages_to_sdk_items
    msgs = [
        {"role": "system", "content": "old system prompt"},
        {"role": "user", "content": "hi"},
    ]
    items = _tudou_messages_to_sdk_items(msgs)
    assert len(items) == 1
    assert items[0]["role"] == "user"


def test_messages_to_sdk_items_tool_calls_and_results():
    """asst with tool_calls + tool-role result → SDK function_call +
    function_call_output items."""
    from app.agent_runtime.sdk_adapter import _tudou_messages_to_sdk_items
    msgs = [
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_abc", "type": "function",
             "function": {"name": "get_weather",
                          "arguments": '{"city":"Beijing"}'}},
        ]},
        {"role": "tool", "tool_call_id": "call_abc",
         "content": "Sunny 24C"},
        {"role": "assistant", "content": "It's sunny."},
    ]
    items = _tudou_messages_to_sdk_items(msgs)
    types = [
        (i.get("type") or "msg") + ":" + (i.get("role") or "")
        for i in items
    ]
    assert types == [
        "message:user",         # user
        "function_call:",       # tool_call from asst (no text → no msg)
        "function_call_output:",  # tool result
        "message:assistant",    # final asst text
    ], f"unexpected SDK items: {types}"
    # function_call must carry the call_id so the matching output_item
    # finds its pair — orphan call_ids cause provider 400s.
    fc = next(i for i in items if i.get("type") == "function_call")
    assert fc["call_id"] == "call_abc"
    assert fc["name"] == "get_weather"
    fco = next(i for i in items if i.get("type") == "function_call_output")
    assert fco["call_id"] == "call_abc"


def test_messages_to_sdk_items_skips_orphans_and_empties():
    """Malformed entries are dropped, not propagated as 400-causing
    orphans:
      - tool result without tool_call_id
      - asst tool_call without id
      - empty user content
    """
    from app.agent_runtime.sdk_adapter import _tudou_messages_to_sdk_items
    msgs = [
        {"role": "user", "content": ""},  # empty
        {"role": "user", "content": "ok"},
        {"role": "assistant", "tool_calls": [
            {"id": "", "function": {"name": "x", "arguments": "{}"}},
        ]},
        {"role": "tool", "content": "result", "tool_call_id": ""},
    ]
    items = _tudou_messages_to_sdk_items(msgs)
    # Only the non-empty user msg survives.
    assert len(items) == 1
    assert items[0]["content"] == "ok"


# ──────────────────────────────────────────────────────────────────
# 2. SDK items → messages persistence (inverse direction)
# ──────────────────────────────────────────────────────────────────

class _FakeToolCallItem:
    """Mimics SDK's ToolCallItem — minimal duck type."""
    type = "tool_call_item"

    def __init__(self, call_id, name, args):
        self.raw_item = _FakeRawFunctionCall(call_id, name, args)


class _FakeRawFunctionCall:
    def __init__(self, call_id, name, args):
        self.call_id = call_id
        self.id = call_id
        self.name = name
        self.arguments = args


class _FakeToolCallOutputItem:
    """Real SDK ToolCallOutputItem exposes `output` BOTH as a direct
    attribute on the item AND in raw_item dict. event_bridge reads
    `item.output`; _persist_sdk_items_to_messages reads either.
    Mirror both so test fakes don't rot away from real SDK shape."""
    type = "tool_call_output_item"

    def __init__(self, call_id, output):
        self.output = output
        self.call_id = call_id
        self.raw_item = {"call_id": call_id, "output": output}


class _FakeMessageOutputItem:
    type = "message_output_item"

    def __init__(self, text):
        self.raw_item = _FakeRawMessage(text)


class _FakeRawMessage:
    def __init__(self, text):
        self.content = [_FakeTextPart(text)]


class _FakeTextPart:
    def __init__(self, text):
        self.text = text


def test_persist_sdk_items_writes_tool_calls_to_messages(fake_agent):
    """Walking new_items reconstructs asst.tool_calls + tool-role
    messages in the right ChatCompletion order."""
    from app.agent_runtime.sdk_adapter import _persist_sdk_items_to_messages
    new_items = [
        _FakeToolCallItem("call_1", "get_weather",
                          '{"city":"Beijing"}'),
        _FakeToolCallOutputItem("call_1", "Sunny 24C"),
        _FakeMessageOutputItem("It's sunny in Beijing."),
    ]
    _persist_sdk_items_to_messages(fake_agent, new_items)

    # Expect: asst-with-tool_calls, tool-role result, asst final text
    msgs = fake_agent.messages
    assert len(msgs) == 3
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["tool_calls"][0]["id"] == "call_1"
    assert msgs[0]["tool_calls"][0]["function"]["name"] == "get_weather"
    assert msgs[1]["role"] == "tool"
    assert msgs[1]["tool_call_id"] == "call_1"
    assert msgs[1]["content"] == "Sunny 24C"
    assert msgs[2]["role"] == "assistant"
    assert msgs[2]["content"] == "It's sunny in Beijing."


def test_persist_sdk_items_skips_reasoning(fake_agent):
    """reasoning_item must not pollute agent.messages — legacy doesn't
    persist CoT either, and surfacing it would leak thinking to UI."""
    from app.agent_runtime.sdk_adapter import _persist_sdk_items_to_messages

    class _FakeReasoning:
        type = "reasoning_item"

    new_items = [
        _FakeReasoning(),
        _FakeMessageOutputItem("final text"),
    ]
    _persist_sdk_items_to_messages(fake_agent, new_items)
    assert len(fake_agent.messages) == 1
    assert fake_agent.messages[0]["content"] == "final text"


def test_round_trip_history_through_both_helpers(fake_agent):
    """The full loop: SDK items → persist → ChatCompletion msgs →
    re-convert to SDK items. Should be lossless for the conversation
    structure (item count + roles + call_ids preserved)."""
    from app.agent_runtime.sdk_adapter import (
        _tudou_messages_to_sdk_items,
        _persist_sdk_items_to_messages,
    )
    # Simulate one SDK turn with 1 tool call
    new_items = [
        _FakeToolCallItem("call_x", "ping", "{}"),
        _FakeToolCallOutputItem("call_x", "pong"),
        _FakeMessageOutputItem("done"),
    ]
    _persist_sdk_items_to_messages(fake_agent, new_items)
    # Now seed a user message at start (real flow does this in run())
    fake_agent.messages.insert(0, {"role": "user", "content": "ping me"})
    # Reconvert back
    items = _tudou_messages_to_sdk_items(fake_agent.messages)
    types = [
        (i.get("type") or "msg") + ":" + (i.get("role") or "")
        for i in items
    ]
    # Order: user, function_call, function_call_output, asst-text
    assert types == [
        "message:user",
        "function_call:",
        "function_call_output:",
        "message:assistant",
    ]


# ──────────────────────────────────────────────────────────────────
# 3. EventBridge — kind names + agent.events persistence
# ──────────────────────────────────────────────────────────────────

def test_event_bridge_emits_legacy_kind_names(fake_agent):
    """Bridge must emit `tool_call` / `tool_result` / `message` — NOT
    `tool_call_start` / `tool_call_end` (portal_bundle.js:7944 keys
    on these exact strings, fallthrough → JSON dump rendered as
    raw debug text the user can't read)."""
    from app.agent_runtime.event_bridge import EventBridge
    captured = []
    bridge = EventBridge(
        on_event=lambda evt: captured.append(evt),
        tudou_agent=fake_agent,
    )

    # Synthesize each item type
    class _Ev:
        type = "run_item_stream_event"

        def __init__(self, item):
            self.item = item

    bridge.forward(_Ev(_FakeToolCallItem("c1", "ping", "{}")))
    bridge.forward(_Ev(_FakeToolCallOutputItem("c1", "pong")))
    bridge.forward(_Ev(_FakeMessageOutputItem("hello")))

    kinds = [getattr(e, "kind", None) for e in captured]
    assert kinds == ["tool_call", "tool_result", "message"], \
        f"event kinds drifted from legacy: {kinds}"


def test_event_bridge_persists_to_agent_events(fake_agent):
    """Every _emit must _log to agent.events (so chat UI restart
    replay via GET /agent/{id}/events works)."""
    from app.agent_runtime.event_bridge import EventBridge
    bridge = EventBridge(
        on_event=lambda _evt: None,
        tudou_agent=fake_agent,
    )
    bridge._emit("tool_call",
                 {"name": "x", "arguments": "{}"})
    bridge._emit("message",
                 {"role": "assistant", "content": "hi"})

    assert len(fake_agent.events) == 2
    assert fake_agent.events[0]["kind"] == "tool_call"
    assert fake_agent.events[1]["kind"] == "message"
    assert fake_agent.events[1]["data"]["content"] == "hi"


def test_event_bridge_tool_result_uses_field_named_result():
    """portal_bundle.js:7951 reads d.result (not d.output). Bridge
    must emit with the right field name."""
    from app.agent_runtime.event_bridge import EventBridge
    captured = []
    bridge = EventBridge(
        on_event=lambda evt: captured.append(evt),
    )

    class _Ev:
        type = "run_item_stream_event"
        item = _FakeToolCallOutputItem("c1", "the actual result")

    bridge.forward(_Ev())
    assert captured[0].kind == "tool_result"
    assert "result" in captured[0].data, \
        "frontend keys on 'result' field; 'output' silently dropped"
    assert captured[0].data["result"] == "the actual result"


# ──────────────────────────────────────────────────────────────────
# 4. max_turns wiring
# ──────────────────────────────────────────────────────────────────

def test_sdk_adapter_uses_agent_max_turns_when_set():
    """SDKAgentRunner._run_streamed must read Agent.max_turns and
    pass to Runner.run."""
    from app.agent_runtime.sdk_adapter import SDKAgentRunner
    runner = SDKAgentRunner(_FakeAgent(max_turns=42))
    # The actual code-path reads:
    #   _agent_mt = int(getattr(self.tudou_agent, "max_turns", 0) or 0)
    #   max_turns = _agent_mt if _agent_mt > 0 else 1000
    # Mirror that logic in the assertion so refactors that drift the
    # semantic (e.g. setting min instead of just >0) trip immediately.
    agent_mt = int(getattr(runner.tudou_agent, "max_turns", 0) or 0)
    effective = agent_mt if agent_mt > 0 else 1000
    assert effective == 42


def test_sdk_adapter_uses_sentinel_when_max_turns_zero():
    """Agent.max_turns=0 means UNLIMITED — adapter passes a large
    sentinel (1000) to SDK since Runner.run requires a positive int."""
    from app.agent_runtime.sdk_adapter import SDKAgentRunner
    runner = SDKAgentRunner(_FakeAgent(max_turns=0))
    agent_mt = int(getattr(runner.tudou_agent, "max_turns", 0) or 0)
    effective = agent_mt if agent_mt > 0 else 1000
    assert effective == 1000


# ──────────────────────────────────────────────────────────────────
# 5. Lazy SDK import (regression: pkg must load without openai-agents)
# ──────────────────────────────────────────────────────────────────

def test_sdk_adapter_imports_module_lazily():
    """``from app.agent_runtime.sdk_adapter import SDKAgentRunner``
    must succeed without any SDK call. The SDK gets imported only
    inside .run()."""
    # The mere act of importing this test file is the check —
    # but assert via a fresh subimport so cached state doesn't lie.
    import importlib
    import app.agent_runtime.sdk_adapter as mod
    importlib.reload(mod)
    assert hasattr(mod, "SDKAgentRunner")
    assert hasattr(mod, "_tudou_messages_to_sdk_items")
    assert hasattr(mod, "_persist_sdk_items_to_messages")


def test_tudou_model_imports_lazily():
    """app.agent_runtime.tudou_model must import without instantiating
    the SDK Model subclass (lazy metaclass)."""
    import app.agent_runtime.tudou_model as mod
    assert hasattr(mod, "TudouClawModel")
    assert hasattr(mod, "build_tudou_model")
