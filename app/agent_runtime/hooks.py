"""SDK RunHooks — call into B's nudge evaluator + L3 memory hooks.

The SDK's ``RunHooks`` lifecycle:
  - on_agent_start / on_agent_end
  - on_llm_start / on_llm_end
  - on_tool_start / on_tool_end
  - on_handoff

We use these to plug TudouClaw's framework-level behaviors that
SDK doesn't know about:
  - on_llm_end → call B.evaluate_nudge; if a nudge fires, inject
    it as a user message into the SDK conversation (next turn sees it)
  - on_tool_end → bookkeeping (turn_query_cache, action buffer)
  - on_agent_end → flush L3 action buffer, extract facts, save events

All these were inlined in the legacy chat loop; here they're
hooks so the SDK Runner stays the orchestrator.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


class TudouClawRunHooks:
    """SDK RunHooks subclass that bridges to TudouClaw framework
    behaviors.

    Constructed per Runner.run_streamed invocation. Holds references
    to the live TudouClaw Agent and the EventBridge so hooks can
    write back state and emit events.

    NOTE: This is a SCAFFOLD. Phase 1 will:
      1. Subclass agents.RunHooks (or AgentHooks) properly with
         async signatures the SDK expects
      2. Implement the actual nudge injection mechanism (SDK has
         input modification points; need to find the right one)
      3. Wire L3 memory + action buffer flush at on_agent_end
    """

    def __init__(self, tudou_agent, event_bridge):
        self.tudou_agent = tudou_agent
        self.event_bridge = event_bridge
        # Track per-turn state for nudge cap enforcement
        self._nudge_count = 0
        self._max_nudges_per_turn = 3
        # Snapshot of user message for this turn (so on_llm_end has
        # it available without re-walking messages)
        self._user_text: str = ""

    # ── Lifecycle hooks (signatures will be adjusted to match
    #    actual SDK API in Phase 1; current shape is provisional) ──

    async def on_agent_start(self, ctx, agent) -> None:
        """Called once per agent run. Reset per-turn state."""
        self._nudge_count = 0
        # Snapshot user text from messages (last user message)
        try:
            msgs = getattr(self.tudou_agent, "messages", []) or []
            for m in reversed(msgs):
                if m.get("role") == "user":
                    c = m.get("content")
                    self._user_text = c if isinstance(c, str) else ""
                    break
        except Exception:
            pass

    async def on_llm_end(self, ctx, agent, output) -> None:
        """Called after each LLM call within an agent run. Decide
        whether a nudge should fire."""
        try:
            from app.runtime import evaluate_nudge

            # Extract the LLM's text reply from the SDK output.
            # Actual field name TBD per SDK API surface.
            agent_reply = ""
            try:
                agent_reply = str(getattr(output, "text", "") or "")
            except Exception:
                pass

            messages = getattr(self.tudou_agent, "messages", []) or []
            has_tools = bool(getattr(agent, "tools", None) or [])

            nudge = evaluate_nudge(
                user_text=self._user_text,
                agent_reply=agent_reply,
                messages=messages,
                has_tools=has_tools,
                iteration=0,  # TBD: get from SDK ctx
                max_iterations=10,
                nudge_count=self._nudge_count,
                max_nudges_per_turn=self._max_nudges_per_turn,
                stop_reason="",  # TBD: extract from output
            )
            if nudge is not None:
                self._nudge_count += 1
                # Inject nudge into the conversation (SDK API for
                # this TBD — Phase 1 will fill in)
                logger.info(
                    "SDK runtime: nudge fired (kind=%s reason=%r) "
                    "agent=%s",
                    nudge.kind, nudge.reason_detail,
                    getattr(self.tudou_agent, "id", "?")[:8])
        except Exception as e:
            logger.debug("on_llm_end nudge eval skipped: %s", e)

    async def on_tool_end(self, ctx, agent, tool, result) -> None:
        """Called after each tool execution. Update turn-query
        cache (knowledge_lookup / memory_recall dedup) + L3 action
        buffer."""
        # TODO Phase 1
        pass

    async def on_agent_end(self, ctx, agent, output) -> None:
        """Called when the agent run completes. Flush L3 action
        buffer + extract facts (mirrors legacy chat() finalization)."""
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
            logger.debug("on_agent_end L3 flush skipped: %s", e)
