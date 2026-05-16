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
            """Run B's nudge evaluator. Currently fires-and-logs;
            actual injection (re-prompting the LLM) requires the SDK
            to expose mid-run input mutation, which 0.17 does only
            via the Runner's input_items parameter on the NEXT call.
            For now we log so admins can see the would-be nudge in
            traces; injection wiring deferred to Phase 3."""
            try:
                from app.runtime import evaluate_nudge
                import os

                # Extract LLM text reply
                agent_reply = ""
                try:
                    # ModelResponse has .output (list of items) and
                    # .output_text helper on newer SDK
                    output_text = getattr(response, "output_text", "")
                    if output_text:
                        agent_reply = str(output_text)
                except Exception:
                    pass

                msgs = getattr(self.tudou_agent, "messages", []) or []
                has_tools = bool(getattr(agent, "tools", None) or [])

                nudge = evaluate_nudge(
                    user_text=self._user_text,
                    agent_reply=agent_reply,
                    messages=msgs,
                    has_tools=has_tools,
                    iteration=0,         # SDK doesn't expose iter
                                          # count cleanly; treat as 0
                                          # (cap is enforced via
                                          # _nudge_count below)
                    max_iterations=10,
                    nudge_count=self._nudge_count,
                    max_nudges_per_turn=self._max_nudges_per_turn,
                    stop_reason="",
                    enable_narrator=os.environ.get(
                        "TUDOU_NUDGE_WEAK_MODELS", "1") != "0",
                    enable_tool_error=os.environ.get(
                        "TUDOU_TOOL_ERROR_NUDGE", "1") != "0",
                    enable_must_verify=os.environ.get(
                        "TUDOU_VERIFY_NUDGE", "1") != "0",
                )
                if nudge is not None:
                    self._nudge_count += 1
                    logger.info(
                        "SDK runtime nudge would fire: kind=%s "
                        "reason=%r agent=%s (Phase 3 will wire the "
                        "actual injection)",
                        nudge.kind, nudge.reason_detail,
                        getattr(self.tudou_agent, "id", "?")[:8])
                    # Surface to portal so admins see when nudges
                    # WOULD have fired even before injection works.
                    try:
                        self.event_bridge._emit("nudge", {
                            "reason": nudge.kind,
                            "detail": nudge.reason_detail[:120],
                            "phase": "would_fire_pending_injection",
                        })
                    except Exception:
                        pass
            except Exception as e:
                logger.debug("on_llm_end nudge eval skipped: %s", e)

        async def on_tool_start(self, context, agent, tool) -> None:
            """Tool dispatch start — already covered by EventBridge's
            run_item_stream_event handling, but having this hook lets
            us add per-tool budget tracking / approval gates if
            needed later."""
            tool_name = getattr(tool, "name", "?")
            logger.debug(
                "SDK on_tool_start: %s (agent=%s)",
                tool_name,
                getattr(self.tudou_agent, "id", "?")[:8])

        async def on_tool_end(self, context, agent, tool, result) -> None:
            """Tool finished — bookkeeping point for L3 action
            buffer + turn query cache (knowledge_lookup dedup, etc.).
            Without this, the SDK runtime would skip these
            TudouClaw-specific behaviors that legacy A relies on."""
            tool_name = getattr(tool, "name", "?")
            try:
                # Buffer the action so the L3 flush at on_agent_end
                # has something to summarize. Mirrors legacy A's
                # tool-result handling.
                from app.core import memory as _mem
                mm = _mem.get_memory_manager()
                summary = str(result)[:200] if result else ""
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
