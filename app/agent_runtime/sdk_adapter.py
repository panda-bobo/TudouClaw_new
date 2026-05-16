"""SDKAgentRunner — the entry point that A.Agent.chat() routes to
when runtime_mode == "sdk".

Lifecycle of one user → agent turn:

  1. Caller invokes ``SDKAgentRunner(tudou_agent).run(user_message,
     on_event=callback)``
  2. We build SDK ``Agent(instructions=callable, tools=[...])``
     - instructions: a closure that calls TudouClaw's
       _build_static_system_prompt + _inject_dynamic_context (so
       persona / skill / project context all come from the existing
       TudouClaw machinery — no duplication)
     - tools: filtered by TudouClaw's tool permissions + opt-in,
       wrapped via @function_tool to bridge to TudouClaw's tools.py
  3. Stream events through SDK's Runner.run_streamed
  4. event_bridge translates SDK events to portal UI shape and
     forwards via on_event (matches legacy chat loop's event shape
     so frontend doesn't need to change)
  5. RunHooks call B.evaluate_nudge after each LLM turn; injected
     nudges go back into the conversation as user messages
  6. On run end, hooks flush L3 memory, mark plan steps, etc.

Lazy SDK import: the SDK is only imported when ``run()`` is called.
``import app.agent_runtime`` works without ``openai-agents`` installed
— necessary so the legacy A path stays runnable in environments
that haven't installed the SDK yet.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class SDKNotInstalledError(RuntimeError):
    """Raised when SDKAgentRunner.run() is invoked but the
    ``openai-agents`` package isn't installed."""

    INSTALL_HINT = (
        "OpenAI Agents SDK not installed. To enable the SDK runtime:\n"
        "    pip install openai-agents\n"
        "Then set agent.runtime_mode = 'sdk' (per-agent) and restart "
        "the backend. Until installed, leave runtime_mode = 'legacy' "
        "(the default) and the existing chat loop continues to work."
    )

    def __init__(self):
        super().__init__(self.INSTALL_HINT)


def is_sdk_available() -> bool:
    """True iff ``openai-agents`` is importable. Cheap — caches the
    result so repeated calls don't re-attempt import."""
    global _SDK_AVAILABLE
    if _SDK_AVAILABLE is not None:
        return _SDK_AVAILABLE
    try:
        import agents  # noqa: F401
        _SDK_AVAILABLE = True
    except ImportError:
        _SDK_AVAILABLE = False
    return _SDK_AVAILABLE


_SDK_AVAILABLE: Optional[bool] = None


class SDKAgentRunner:
    """Wraps an OpenAI Agents SDK Runner around a TudouClaw Agent.

    Construct with the live TudouClaw Agent instance. Call ``.run(
    user_message, on_event=...)`` to execute one chat turn through
    the SDK instead of the legacy chat loop.

    Reads from the TudouClaw Agent (no copy):
      - profile, persona, soul_md, instructions
      - granted_skills, allowed_tools, denied_tools
      - working_dir, project_id, meeting_id, ...
      - L3 memory store reference (read by hooks)
    Writes back:
      - self.messages (assistant replies + tool results)
      - self.events (UI event log via _log)
      - L3 memory facts (via on_agent_end hook)

    No duplicate state: this adapter is a façade; the source of
    truth stays on the TudouClaw Agent.
    """

    def __init__(self, tudou_agent):
        """Args:
            tudou_agent: an instance of app.agent.Agent
        """
        self.tudou_agent = tudou_agent

    def run(
        self,
        user_message: Any,
        on_event: Optional[Callable] = None,
        abort_check: Optional[Callable[[], bool]] = None,
        source: str = "admin",
        context_id: str = "solo",
    ) -> str:
        """Execute one chat turn via the SDK runtime.

        Mirror of app.agent.Agent.chat() signature so the caller
        (A.Agent.chat() dispatch) can swap legacy ↔ SDK trivially.

        Args:
            user_message: str or multimodal list (same shape as
                          legacy chat())
            on_event: callback receiving AgentEvent objects; same
                      shape the portal frontend already understands
            abort_check: optional callable returning True if the
                         user clicked stop
            source: "admin" / "agent:{name}" / "system"
            context_id: "solo" / "project:{id}" / "meeting:{id}"

        Returns:
            The agent's final assistant text (same as legacy chat()).

        Raises:
            SDKNotInstalledError: if openai-agents is not installed.
        """
        if not is_sdk_available():
            raise SDKNotInstalledError()

        # Lazy SDK import — only happens when SDK is actually needed
        from agents import Agent as SDKAgent, Runner

        # Build the SDK Agent each turn (cheap — just a dataclass).
        # instructions is a callable so persona/dynamic context get
        # rebuilt fresh each turn from TudouClaw state.
        from .instructions_builder import build_instructions_callable
        from .tool_registry import build_sdk_tools
        from .event_bridge import EventBridge
        from .hooks import TudouClawRunHooks

        instructions = build_instructions_callable(
            self.tudou_agent,
            user_message=user_message,
            context_id=context_id,
        )
        sdk_tools = build_sdk_tools(self.tudou_agent, user_message)

        sdk_agent = SDKAgent(
            name=self.tudou_agent.name or self.tudou_agent.id[:8],
            instructions=instructions,
            tools=sdk_tools,
            model=self._build_sdk_model(),
        )

        bridge = EventBridge(on_event=on_event,
                             abort_check=abort_check,
                             tudou_agent=self.tudou_agent)
        hooks = TudouClawRunHooks(self.tudou_agent, bridge)

        # ── Run through SDK ───────────────────────────────────────
        # Connectivity validated in 0c (scripts/sdk_mimo_connectivity_
        # poc.py). Use ``Runner.run_streamed`` so the bridge can
        # forward token-by-token events into the portal — same UX
        # as the legacy chat loop's text_delta stream.
        #
        # The SDK Runner is async. We're invoked from the legacy
        # ``Agent.chat()`` which is sync, so wrap with asyncio.run.
        # In Phase 2+, when ``Agent.chat_async()`` is the primary
        # caller, we'll skip the wrap and yield natively.
        import asyncio

        # Persist the user message into self.messages so the
        # rest of TudouClaw (history compaction, transcript replay,
        # L3 fact extraction) sees it — mirrors legacy ``chat()``.
        try:
            _user_text = (user_message if isinstance(user_message, str)
                          else str(user_message))
            self.tudou_agent.messages.append({
                "role": "user",
                "content": _user_text,
                "_source": source,
            })
            self.tudou_agent._log("message", {
                "role": "user",
                "content": _user_text[:500],
                "source": source,
            })
        except Exception:
            pass

        try:
            final_text = asyncio.run(
                self._run_streamed(sdk_agent, user_message, bridge, hooks))
        except SDKNotInstalledError:
            raise
        except Exception as e:
            logger.exception(
                "SDKAgentRunner.run failed (agent=%s): %s",
                getattr(self.tudou_agent, "id", "?")[:8], e)

            # Helpful per-error-class messages so the user sees what
            # to do, not just a stack-trace excerpt.
            err_str = str(e)
            err_type = type(e).__name__

            if ("tool_calls" in err_str
                    and "tool messages" in err_str):
                # 2026-05-16: this should no longer be reachable
                # since we switched _run_streamed to Runner.run
                # (non-streaming) — the SDK streaming + parallel
                # tool_calls bug that produced these orphans is
                # bypassed. If it shows up again, the root cause
                # is somewhere new (e.g. tool dispatch raising
                # without returning a string). Check backend logs
                # for "SDK tool wrapper error" — every tool MUST
                # return a string, never raise.
                hint = (
                    "[SDK runtime error — orphan asst.tool_calls. "
                    "Should not happen post-2026-05-16 fix. Check "
                    "backend logs for 'SDK tool wrapper error' to "
                    "find which tool didn't return a string.]"
                )
            elif "401" in err_str or "Incorrect API key" in err_str:
                hint = (
                    "[SDK runtime error — provider auth failed (401). "
                    "Check that the agent's provider has a valid "
                    "api_key in Settings → Providers.]"
                )
            elif "404" in err_str or "model" in err_str.lower():
                hint = (
                    f"[SDK runtime error — provider rejected the "
                    f"model name. {err_type}: {err_str[:200]}]"
                )
            else:
                hint = (
                    f"[SDK runtime error: {err_type}: "
                    f"{err_str[:200]}]"
                )

            final_text = hint

        # Persist agent reply to self.messages (mirrors legacy
        # chat() finalization, so transcript / L3 / dynamic context
        # see it on the next turn).
        try:
            self.tudou_agent.messages.append({
                "role": "assistant",
                "content": final_text or "",
                "_source": "sdk-runtime",
            })
        except Exception:
            pass

        return final_text or ""

    async def _run_streamed(
        self,
        sdk_agent: Any,
        user_message: Any,
        bridge: Any,
        hooks: Any,
    ) -> str:
        """Run the agent through the SDK and forward events to the
        portal UI.

        ── 2026-05-16: switched from Runner.run_streamed to Runner.run

        Reproduced (scripts/sdk_streaming_repro): SDK's
        Runner.run_streamed has a real bug with DeepSeek-style
        OpenAI-compat backends + parallel tool_calls. The streaming
        delta accumulator drops one of the parallel tool_call entries,
        so the SECOND LLM call sends a conversation with N tool_calls
        in the asst message but only N-1 tool results — DeepSeek
        rejects with HTTP 400 "insufficient tool messages following
        tool_calls". The same setup with Runner.run (non-streaming)
        works perfectly.

        Trade-off: lose token-by-token typing animation in chat. We
        still emit a single MESSAGE event with the full assembled
        reply at end-of-run + tool_call_start / tool_call_end events
        for each tool dispatch. This matches the legacy A behavior
        for non-streaming providers; UX downgrade is "spinner +
        result" instead of "typing animation". Acceptable.

        When the upstream SDK fixes the streaming-tool_call
        accumulator, switch this back to Runner.run_streamed +
        async stream_events loop. Until then, stable > pretty.
        """
        from agents import Runner
        try:
            from agents.exceptions import MaxTurnsExceeded
        except ImportError:
            MaxTurnsExceeded = None  # older SDK shape

        # Pass the user message as-is — SDK Runner accepts str or
        # the OpenAI-style multimodal list (we already match shape).
        run_input = (user_message if isinstance(user_message, str)
                     else str(user_message))

        # ── max_turns: match legacy agent.py:11180 ──────────────
        # SDK default is 10; legacy uses 20 (+ per-tool soft/hard
        # budget caps inside the loop). Read from the agent's
        # persisted ``max_turns`` field so per-agent overrides via
        # the edit modal apply to SDK runtime too.
        max_turns = int(getattr(self.tudou_agent, "max_turns", 20) or 20)

        # Non-streaming: gather all events at end-of-run via
        # result.new_items and forward to the portal.
        result = None
        max_turns_hit = False
        try:
            try:
                result = await Runner.run(
                    sdk_agent,
                    run_input,
                    hooks=hooks,
                    max_turns=max_turns,
                )
            except TypeError:
                # Older SDK signatures may not accept hooks= or
                # max_turns=. Try progressively simpler signatures.
                try:
                    result = await Runner.run(
                        sdk_agent, run_input, max_turns=max_turns)
                except TypeError:
                    result = await Runner.run(sdk_agent, run_input)
        except Exception as e:
            # ── Graceful MaxTurnsExceeded handling ──────────────
            # Legacy at hard-cap shows "已强制终止 — 工具调用太多"
            # instead of a raw error so the user gets a coherent reply.
            # Mirror that here: log, then return a salvage message
            # rather than re-raising (which would bubble up to the
            # generic "[SDK runtime error...]" hint in run()).
            if MaxTurnsExceeded is not None and isinstance(e, MaxTurnsExceeded):
                max_turns_hit = True
                logger.warning(
                    "SDK runtime: agent %s hit max_turns=%d — "
                    "returning salvage message",
                    getattr(self.tudou_agent, "id", "?")[:8], max_turns)
            else:
                # Other exceptions: re-raise so SDKAgentRunner.run's
                # outer try/except categorizes them with a useful hint.
                raise

        # Forward post-hoc events to the portal so the chat UI sees
        # what tool calls happened + the final assistant text. The
        # bridge translates each item into the legacy AgentEvent
        # shape exactly the same way it would for streamed events.
        if result is not None:
            try:
                for item in (getattr(result, "new_items", []) or []):
                    # Wrap each item as a fake "run_item_stream_event"
                    # so the bridge's existing dispatcher handles it.
                    fake_event = _FakeRunItemEvent(item)
                    bridge.forward(fake_event)
            except Exception as e:
                logger.warning(
                    "post-run event forwarding failed: %s — final reply "
                    "still returned to chat", e)

        if max_turns_hit:
            # Salvage: stitch together a coherent reply that tells
            # the user we hit the cap. Mirrors legacy's "已强制终止"
            # behavior so the chat doesn't dead-end on a raw exception.
            return (
                f"[已强制终止 — 已连续调用工具 {max_turns} 轮仍未收敛。"
                f"请简化任务或在 agent 编辑界面提高 max_turns 上限"
                f"（当前 {max_turns}）后重试。]"
            )

        return getattr(result, "final_output", "") or ""

    def _build_sdk_model(self):
        """Construct the SDK Model object pointing at TudouClaw's
        currently-resolved provider/model.

        Resolution order (matches what legacy chat() does, so the
        SDK runtime hits the SAME endpoint a legacy turn would):

          1. Agent.\\_resolve_effective_provider_model() returns
             (provider_id, model_name) — already accounts for
             multimodal routing, learning model overrides, etc.
          2. llm.get_registry().get(provider_id) returns the
             ProviderEntry with base_url + api_key
          3. Build AsyncOpenAI(base_url=..., api_key=...) +
             OpenAIChatCompletionsModel(model=model_name, ...)

        If anything in the chain fails, raises a clear error rather
        than falling back to OpenAI's public endpoint with a "dummy"
        key (the previous behavior that produced 401 errors). The
        caller's try/except in run() turns this into a chat-visible
        "[SDK runtime error...]" reply so the user sees what's wrong
        instead of a silent crash.
        """
        # AsyncOpenAI / OpenAIChatCompletionsModel no longer needed —
        # we now use TudouClawModel below (delegates to legacy).
        from app import llm as _llm

        # 1. Resolve (provider_id, model_name) the same way legacy
        #    chat() does at agent.py:11008-ish.
        try:
            provider_id, model_name = (
                self.tudou_agent._resolve_effective_provider_model())
        except Exception as e:
            raise RuntimeError(
                "SDK runtime: could not resolve provider/model from "
                f"agent (id={getattr(self.tudou_agent, 'id', '?')[:8]}): {e}"
            ) from e

        if not provider_id or not model_name:
            raise RuntimeError(
                "SDK runtime: agent has no provider/model configured "
                f"(provider={provider_id!r}, model={model_name!r}). "
                "Set them in the agent edit modal first."
            )

        # 2. Pull base_url + api_key from the provider registry.
        try:
            registry = _llm.get_registry()
            entry = registry.get(provider_id)
        except Exception as e:
            raise RuntimeError(
                f"SDK runtime: provider registry lookup failed: {e}"
            ) from e

        if entry is None:
            raise RuntimeError(
                f"SDK runtime: provider {provider_id!r} not in registry. "
                "Configure it in Settings → Providers."
            )

        base_url = (entry.base_url or "").strip()
        api_key = (entry.api_key or "").strip()
        if not base_url:
            raise RuntimeError(
                f"SDK runtime: provider {provider_id!r} has no base_url. "
                "Edit the provider in Settings → Providers."
            )
        if not api_key:
            raise RuntimeError(
                f"SDK runtime: provider {provider_id!r} has no api_key. "
                "Edit the provider in Settings → Providers."
            )

        # 3. Build the SDK Model.
        # ── 2026-05-16: switched from OpenAIChatCompletionsModel to
        # TudouClawModel, which delegates the actual LLM call to
        # ``app.llm.chat_no_stream``. This makes the SDK runtime
        # reuse EVERY provider-quirk adapter TudouClaw already ships:
        #   - _sanitize_messages_for_openai (mimo reasoning roundtrip,
        #     GLM/Qwen content+tool_calls mutex, image downgrade,
        #     system merge, field whitelisting)
        #   - V2 tool_parsers (DSML for DeepSeek-flash, FunctionXML
        #     for mimo/Hermes ``<function=NAME>``, GLM ``<arg_key>``
        #     inline XML — the EXACT leaks that pushed us to SDK)
        #   - Multi-provider fallback chain
        #   - Token usage tracking
        #   - Connection pool + retries
        # No more reinventing wheels at the message-prep layer; SDK
        # is purely the high-level orchestrator (Agent / Tool / Runner /
        # Hooks).
        from .tudou_model import build_tudou_model
        logger.info(
            "SDK model: agent=%s provider=%s base_url=%s model=%s "
            "(via TudouClawModel → legacy chat_no_stream)",
            getattr(self.tudou_agent, "id", "?")[:8],
            provider_id, base_url, model_name)
        return build_tudou_model(
            provider_id=provider_id,
            model_name=model_name,
            base_url=base_url,
        )


class _FakeRunItemEvent:
    """Minimal shim that lets the bridge's stream-event handler
    process post-run items the same way it would streamed events.
    The bridge looks at .type and .item — that's all we need.

    Used by ``SDKAgentRunner._run_streamed`` to wrap each item from
    ``RunResult.new_items`` so we can reuse the bridge's existing
    streamed-event dispatcher (which keys on ``event.type ==
    'run_item_stream_event'``) for post-hoc items too. This keeps
    one event-routing path in event_bridge.py — no need for a
    separate "non-streaming items" handler.
    """
    type = "run_item_stream_event"

    def __init__(self, item):
        self.item = item
