"""SDK RunHooks — bind TudouClaw lifecycle behaviors into the
OpenAI Agents SDK Runner.

Why hooks (not in-loop):
  - SDK owns the chat-loop iteration; we get one chance per
    lifecycle event to do TudouClaw-specific bookkeeping
  - Keeps the SDK runtime symmetric with the legacy chat loop —
    same nudges fire, same L3 memory writes happen, same events
    reach the portal

Bound events (signatures match openai-agents 0.17.x):

    on_agent_start(ctx, agent)
        Reset per-turn counters. Snapshot user_text from messages.

    on_llm_end(ctx, agent, response)
        Pull the LLM's text reply out of the response, call
        B.evaluate_nudge, log + emit a "nudge" event if one fires.
        Actual nudge INJECTION (re-running the LLM with a system
        message added) is harder under SDK — see comments below.

    on_tool_start / on_tool_end(ctx, agent, tool[, result])
        Forward to portal as tool_call_start / tool_call_end events
        (matches legacy A so the UI doesn't notice the runtime
        change).

    on_agent_end(ctx, agent, output)
        Flush L3 action buffer (mimics legacy chat() finalization
        at agent.py:6864). Without this, agents on SDK runtime
        wouldn't accumulate long-term memory.

NOT bound (Phase 3+ work):
    on_handoff — TudouClaw multi-agent coordination doesn't go
                 through SDK Handoffs, it uses team_create. Hook
                 stays no-op.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def build_run_hooks(tudou_agent, event_bridge):
    """Construct a SDK RunHooks subclass instance bound to this
    TudouClaw Agent + EventBridge. Returns None if the SDK isn't
    importable (caller will skip passing hooks=)."""
    try:
        from agents import RunHooks
    except ImportError:
        return None

    class _TudouClawHooks(RunHooks):
        def __init__(self, tudou_agent, event_bridge):
            self.tudou_agent = tudou_agent
            self.event_bridge = event_bridge
            # Per-turn nudge cap — matches legacy A's _MAX_NUDGES_PER_TURN
            self._nudge_count = 0
            self._max_nudges_per_turn = 3
            # User text snapshot for nudge evaluation
            self._user_text = ""

        async def on_agent_start(self, context, agent) -> None:
            """Reset per-turn state, snapshot user message."""
            self._nudge_count = 0
            try:
                msgs = getattr(self.tudou_agent, "messages", []) or []
                for m in reversed(msgs):
                    if m.get("role") == "user":
                        c = m.get("content")
                        self._user_text = (
                            c if isinstance(c, str) else str(c or ""))
                        break
            except Exception:
                pass

        async def on_llm_end(self, context, agent, response) -> None:
            """No-op now. Nudge evaluation + injection lives in
            ``SDKAgentRunner._run_with_nudges`` (outer loop wrapping
            Runner.run_streamed). Was a "would_fire pending Phase 3"
            log here, but with the outer loop wired (2026-05-16)
            doing this here too would double-evaluate per intra-run
            LLM call AND emit duplicate nudge events to UI.

            Kept as a hook stub for symmetry / future per-call
            telemetry (token usage, response timing) if needed."""
            pass

        async def on_tool_start(self, context, agent, tool) -> None:
            """Tool dispatch start — emit ``tool_call`` event to the
            portal IN REAL TIME so the chat UI shows the tool-call
            card as soon as the dispatch begins, not at end-of-run.

            Event kind MATCHES legacy (agent.py:12617 emits ``tool_call``
            with ``{name, arguments}``); portal_bundle.js:7944 keys on
            this exact name. Using ``tool_call_start`` here would silently
            no-op in the UI — that's the bug @user spotted ("中间是否
            调用了哪些工具，也没有看到").

            ``context`` is typically a ``ToolContext`` for function-tool
            calls, exposing tool_name + tool_arguments + tool_call_id.
            For other tool families it's a plain RunContextWrapper —
            fall back gracefully.

            ── Persist for replay (2026-05-16) ──
            Legacy at agent.py:12620 does BOTH ``self._log(...)`` (writes
            to agent.events for post-restart UI replay via /events
            endpoint) AND ``_emit(evt)`` (live SSE push). SDK runtime
            previously only did the live emit — restart wiped the UI
            chat history. Now mirror legacy: _log + _emit.
            """
            tool_name = (
                getattr(context, "tool_name", None)
                or getattr(tool, "name", "?"))
            tool_args = getattr(context, "tool_arguments", "") or ""
            logger.debug(
                "SDK on_tool_start: %s (agent=%s)",
                tool_name,
                getattr(self.tudou_agent, "id", "?")[:8])
            # event_bridge._emit also _log's to agent.events for restart
            # replay (2026-05-16 fix). No need to _log here.
            try:
                self.event_bridge._emit("tool_call", {
                    "name": tool_name,
                    "arguments": tool_args,
                })
            except Exception as e:
                logger.debug("tool_call emit failed: %s", e)

        async def on_tool_end(self, context, agent, tool, result) -> None:
            """Tool finished — bookkeeping + REAL-TIME portal emit
            + persist to agent.events (for restart replay).

            Emits ``tool_result`` (matching legacy agent.py:12755) so the
            portal updates the tool-call card with the result. Event shape
            ``{name, result}`` — portal_bundle.js:7951 keys on these
            fields exactly. ``result`` not ``output``.
            """
            tool_name = (
                getattr(context, "tool_name", None)
                or getattr(tool, "name", "?"))
            result_str = str(result) if result is not None else ""
            # event_bridge._emit also _log's to agent.events for restart
            # replay (2026-05-16 fix). No need to _log here.
            try:
                self.event_bridge._emit("tool_result", {
                    "name": tool_name,
                    "result": result_str[:1000],
                })
            except Exception as e:
                logger.debug("tool_result emit failed: %s", e)
            try:
                # Buffer the action so the L3 flush at on_agent_end
                # has something to summarize. Mirrors legacy A's
                # tool-result handling.
                from app.core import memory as _mem
                mm = _mem.get_memory_manager()
                summary = result_str[:200]
                mm.buffer_agent_action(
                    self.tudou_agent.id,
                    tool_name=tool_name,
                    summary=summary,
                )
            except Exception as e:
                logger.debug(
                    "on_tool_end action buffer skipped: %s", e)

        async def on_agent_end(self, context, agent, output) -> None:
            """Run completed — flush L3 action buffer, save events.
            Mirrors legacy chat() finalization at agent.py:6864."""
            try:
                from app.core import memory as _mem
                mm = _mem.get_memory_manager()
                llm_call = self.tudou_agent._make_summary_llm_call()
                outcome = mm.flush_action_buffer(
                    self.tudou_agent.id, llm_call=llm_call)
                if outcome:
                    self.tudou_agent._log("memory", {
                        "action": "flush_action_buffer",
                        "outcome": outcome.content[:100],
                    })
            except Exception as e:
                logger.debug(
                    "on_agent_end L3 flush skipped: %s", e)

        async def on_handoff(self, context, from_agent, to_agent) -> None:
            """Multi-agent dispatch hook — TudouClaw uses team_create
            (cross-process), not SDK Handoffs (in-process), so this
            hook is a no-op for now. Kept defined so the SDK doesn't
            warn about a missing hook."""
            pass

    return _TudouClawHooks(tudou_agent, event_bridge)


# Backward-compat alias for app/agent_runtime/sdk_adapter.py which
# imports TudouClawRunHooks. Calling it as a class works the same
# way — user constructs an instance with (tudou_agent, event_bridge).
def TudouClawRunHooks(tudou_agent, event_bridge):
    """Compat shim: construct a real RunHooks instance via the
    factory. Returns None if SDK isn't installed."""
    return build_run_hooks(tudou_agent, event_bridge)
