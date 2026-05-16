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
        #
        # ── 2026-05-16: preserve MULTIMODAL content as-is ──
        # Previously stringified everything with ``str(user_message)``;
        # for {role:user, content:[{type:"text",...},{type:"image_url",...}]}
        # this turned the image into a Python repr string — useless to
        # the LLM. Now keep the original shape; downstream
        # _tudou_messages_to_sdk_items + sanitize handle conversion.
        try:
            self.tudou_agent.messages.append({
                "role": "user",
                "content": user_message,   # keep list / dict as-is
                "_source": source,
            })
            # For the _log preview (chat UI only needs text), extract
            # the text portion if it's multimodal.
            _preview_text = _extract_text_for_preview(user_message)
            self.tudou_agent._log("message", {
                "role": "user",
                "content": _preview_text[:500],
                "source": source,
            })
            # ── Persist immediately so user msg survives any mid-run
            # crash. Throttle (1s default) protects against thrashing
            # when several events fire within the same second. The
            # FIRST call always falls through because _last_persist_at
            # starts at 0; subsequent ones throttle.
            self.tudou_agent._maybe_persist()
        except Exception:
            pass

        try:
            final_text = asyncio.run(
                self._run_with_nudges(
                    sdk_agent, user_message, bridge, hooks))
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

        # ── Persistence finalization ─────────────────────────────────
        # The intermediate items (asst with tool_calls + tool results)
        # were already appended to tudou_agent.messages inside
        # _run_streamed via _persist_sdk_items_to_messages. That helper
        # also appends the FINAL assistant text — but only if it found
        # a message_output_item in result.new_items.
        #
        # Edge cases where we need a fallback append here:
        #   - SDK errored before producing a final message (final_text
        #     is the "[SDK runtime error: ...]" hint above)
        #   - max_turns hit (final_text is "已强制终止" salvage)
        #   - result.new_items was empty for some reason
        # Check the last persisted asst message; only append if it
        # doesn't match what we're about to return (avoid duplicates).
        try:
            msgs = self.tudou_agent.messages or []
            last = msgs[-1] if msgs else None
            last_is_matching = (
                isinstance(last, dict)
                and last.get("role") == "assistant"
                and (last.get("content") or "") == (final_text or "")
            )
            if not last_is_matching:
                self.tudou_agent.messages.append({
                    "role": "assistant",
                    "content": final_text or "",
                    "_source": "sdk-runtime",
                })
        except Exception:
            pass

        # Force-persist to disk so chat history survives crash / SIGKILL.
        # Legacy does this at iteration boundaries inside chat(); SDK
        # runtime has no equivalent loop, so we persist once at end-of-
        # run. force=True bypasses the throttle so this save always lands.
        try:
            self.tudou_agent._maybe_persist(force=True)
        except Exception as e:
            logger.debug("SDK runtime _maybe_persist failed: %s", e)

        return final_text or ""

    async def _run_with_nudges(
        self,
        sdk_agent: Any,
        user_message: Any,
        bridge: Any,
        hooks: Any,
    ) -> str:
        """Outer loop: runs one Runner.run_streamed, evaluates a
        nudge against the result, and re-runs if a nudge fires —
        injecting the nudge text as a user message in between.

        Why this exists:
          Legacy chat loop runs the LLM inside its own iteration
          loop (agent.py:11400ish). Between iterations it checks
          ``evaluate_nudge`` and, if one fires, appends the nudge
          as a user msg + continues looping. SDK's Runner.run_streamed
          owns its own iteration internally — once it completes, the
          run is "done" from SDK's perspective. To match legacy's
          self-correction behavior under SDK runtime, we wrap
          Runner.run_streamed with an outer loop that does the
          legacy nudge dance manually.

        Lifecycle of one user turn under this outer loop:
            1. Runner.run_streamed → returns final_text
            2. evaluate_nudge(final_text, messages, ...) → Nudge | None
            3. None → return final_text (turn done)
            4. Some → append final_text + nudge.text to messages,
                      emit ``kind=nudge`` event for UI, loop
            5. Cap at MAX_NUDGES_PER_TURN=3 (same as legacy +
               hooks._max_nudges_per_turn)

        Nudge kinds covered (per app.runtime.nudge_evaluator):
          - narrator_stall   — empty / "Let me X:" with no tool call
          - tool_error_no_continuation — last tool errored + asst
                                          didn't follow up
          - must_verify       — asst claimed "done" without verifying

        Nudge.text is injected as ``role=user`` (matches legacy at
        agent.py:12024 — system role tends to get ignored mid-
        conversation by some models; user role is the reliable hammer).
        """
        from app.runtime import evaluate_nudge
        import os

        MAX_NUDGES_PER_TURN = 3
        nudge_count = 0
        # Snapshot the user message text for evaluator (don't change
        # this across nudge loops — nudges are SYNTHETIC, the "real"
        # user text is what came in at run() entry)
        _user_text = (user_message if isinstance(user_message, str)
                      else str(user_message))

        final_text = ""
        while True:
            final_text = await self._run_streamed(
                sdk_agent, user_message, bridge, hooks)

            # Evaluate after the full Runner.run_streamed completes.
            # This catches turn-final issues (empty reply, stall, etc.)
            # — NOT mid-tool-loop issues which would need hook-level
            # injection (deferred; SDK doesn't expose mid-Runner.run
            # input mutation cleanly).
            try:
                msgs = getattr(self.tudou_agent, "messages", []) or []
                has_tools = bool(getattr(sdk_agent, "tools", None) or [])
                nudge = evaluate_nudge(
                    user_text=_user_text,
                    agent_reply=final_text or "",
                    messages=msgs,
                    has_tools=has_tools,
                    iteration=nudge_count,
                    max_iterations=MAX_NUDGES_PER_TURN,
                    nudge_count=nudge_count,
                    max_nudges_per_turn=MAX_NUDGES_PER_TURN,
                    stop_reason="",
                    enable_narrator=os.environ.get(
                        "TUDOU_NUDGE_WEAK_MODELS", "1") != "0",
                    enable_tool_error=os.environ.get(
                        "TUDOU_TOOL_ERROR_NUDGE", "1") != "0",
                    enable_must_verify=os.environ.get(
                        "TUDOU_VERIFY_NUDGE", "1") != "0",
                )
            except Exception as e:
                logger.debug("nudge evaluation skipped: %s", e)
                nudge = None

            if nudge is None:
                return final_text or ""

            if nudge_count >= MAX_NUDGES_PER_TURN:
                logger.info(
                    "SDK runtime: nudge would fire (kind=%s) but cap "
                    "%d reached — returning current reply as-is "
                    "(agent=%s)",
                    nudge.kind, MAX_NUDGES_PER_TURN,
                    getattr(self.tudou_agent, "id", "?")[:8])
                return final_text or ""

            # ── Inject the nudge + loop ────────────────────────────
            # Match legacy at agent.py:12024: nudge goes in as
            # role=user (NOT system — system gets ignored by some
            # models mid-conversation). _source marker lets future
            # transcript filtering distinguish synthetic from real.
            logger.info(
                "SDK runtime: nudge FIRED kind=%s reason=%r agent=%s "
                "(injection %d/%d)",
                nudge.kind, nudge.reason_detail,
                getattr(self.tudou_agent, "id", "?")[:8],
                nudge_count + 1, MAX_NUDGES_PER_TURN)
            try:
                self.tudou_agent.messages.append({
                    "role": "user",
                    "content": nudge.text,
                    "_source": "system_nudge",
                })
            except Exception:
                pass
            # Emit a UI event so the user sees we re-prompted
            try:
                bridge._emit("nudge", {
                    "reason": nudge.kind,
                    "detail": (nudge.reason_detail or "")[:120],
                    "phase": "injected",
                })
            except Exception:
                pass

            nudge_count += 1
            # Next iteration: Runner.run_streamed re-runs with the
            # appended nudge user msg in agent.messages (which
            # _tudou_messages_to_sdk_items will pick up at the top of
            # _run_streamed). The new "user" input to Runner.run is
            # the nudge text — though it's also in the history; the
            # SDK will see it twice but that's fine, makes the nudge
            # the most prominent recent input.
            user_message = nudge.text

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

        # ── 2026-05-16: pass conversation HISTORY to SDK ────────
        # Also: compact old history first (legacy does this at every
        # iteration via _summarize_old_history; SDK runtime had NO
        # compaction wired, so history grew unbounded — @user
        # observed hist=123k chars / 51k tokens per turn).
        _compact_history_if_needed(self.tudou_agent)
        run_input = _tudou_messages_to_sdk_items(
            getattr(self.tudou_agent, "messages", []) or [])
        if not run_input:
            run_input = (user_message if isinstance(user_message, str)
                         else str(user_message))

        # ── max_turns: Claude-style design (2026-05-16) ──────────
        # Agent.max_turns default is 0 = UNLIMITED. Real protection
        # is per-tool budget caps + abort_check, not an arbitrary
        # turn count. SDK Runner.run requires a positive int, so
        # when unlimited we pass a high sentinel (1000) and rely on
        # the inner caps to stop runaway loops.
        # Per-agent override: set max_turns to N>0 in the edit modal
        # for agents that should never run >N turns (e.g. Q&A bots).
        _agent_mt = int(getattr(self.tudou_agent, "max_turns", 0) or 0)
        max_turns = _agent_mt if _agent_mt > 0 else 1000

        # ── 2026-05-16 (afternoon): switched BACK to Runner.run_streamed
        # now that TudouClawModel.stream_response is implemented.
        #
        # Why we went non-streaming first: the SDK's ChatCmplStreamHandler
        # has a bug accumulating parallel tool_call deltas (drops one,
        # produces orphan asst.tool_calls → DeepSeek 400). We worked
        # around by using Runner.run (non-streaming) which dodged the
        # accumulator entirely. Cost: no text_delta events → voice mode
        # TTS waits ~10s for the full reply instead of starting at the
        # first sentence (~2s).
        #
        # Why we can switch back now: TudouClawModel.stream_response
        # yields tool_calls ONLY when complete (built from legacy's
        # correct accumulator). SDK never sees raw tool_call deltas, so
        # its buggy accumulator never runs. text_delta events flow
        # through cleanly for voice-mode sentence-level TTS.
        #
        # Verified via scripts/sdk_streaming_verify.py: DeepSeek + 2
        # parallel tools → 78 text_delta events + 2 tool_calls
        # dispatched + final text matches accumulated stream. No 400.
        result = None
        max_turns_hit = False
        try:
            try:
                stream = Runner.run_streamed(
                    sdk_agent,
                    run_input,
                    hooks=hooks,
                    max_turns=max_turns,
                )
            except TypeError:
                # Older SDK signatures may not accept hooks= or max_turns=
                try:
                    stream = Runner.run_streamed(
                        sdk_agent, run_input, max_turns=max_turns)
                except TypeError:
                    stream = Runner.run_streamed(sdk_agent, run_input)

            # Drain stream events, forwarding raw text deltas to the
            # bridge AS THEY ARRIVE (voice mode sentence-TTS depends
            # on this). High-level item events (tool_call_item etc.)
            # still flow to bridge.forward post-hoc via new_items
            # below — the hooks emit tool_call/tool_result in real
            # time so duplicates are filtered there.
            async for sdk_event in stream.stream_events():
                # Bridge.forward handles raw_response_event → text_delta,
                # run_item_stream_event → tool_call/tool_result/message,
                # agent_updated_stream_event → nudge. All event-kind
                # routing lives in event_bridge.py, not here.
                bridge.forward(sdk_event)

            # stream_events() drained; final_output is now valid.
            result = stream
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

        # ── 2026-05-16 (evening): NO MORE post-hoc event forwarding ──
        # When SDKAgentRunner was non-streaming (Runner.run), we had
        # to iterate result.new_items after-the-fact to feed the
        # bridge — that was the only way events reached the UI.
        # After switching back to Runner.run_streamed, stream_events()
        # already emits RunItemStreamEvent("message_output_created")
        # for the final message; bridge.forward handles it inline
        # during the async-for above. Forwarding result.new_items
        # again here was DOUBLE-emitting every message → @user found
        # back-to-back identical chat bubbles in agent.events.
        # Persistence (write to agent.messages) is still done below
        # via _persist_sdk_items_to_messages — that's a SEPARATE
        # concern (long-term history, not UI event log).

        # ── 2026-05-16: persist intermediate turn items to messages ──
        # Before this fix, only the user message + final assistant text
        # got written to tudou_agent.messages. The intermediate
        # asst-with-tool_calls and tool-result messages were dropped, so:
        #   1. Next turn's history conversion lost the tool-call history
        #      — agent didn't "remember" what it called
        #   2. Transcript replay was incomplete (showed final reply but
        #      not how the agent got there)
        #   3. L3 fact extraction had less material to work with
        # Walk result.new_items, convert each to legacy ChatCompletion
        # shape, append to agent.messages. The final assistant text is
        # appended separately by run() — we skip the LAST message_output_
        # item here to avoid duplicating it.
        if result is not None:
            try:
                _persist_sdk_items_to_messages(
                    self.tudou_agent,
                    getattr(result, "new_items", []) or [],
                )
            except Exception as e:
                logger.warning(
                    "SDK item persistence failed: %s — chat history may "
                    "be incomplete on next turn", e)

        if max_turns_hit:
            # Salvage: stitch together a coherent reply that tells
            # the user we hit the cap. Mirrors legacy's "已强制终止"
            # behavior so the chat doesn't dead-end on a raw exception.
            if _agent_mt > 0:
                msg = (
                    f"[已强制终止 — 已连续调用工具 {max_turns} 轮仍未收敛。"
                    f"该 agent 的 max_turns 显式设为 {max_turns}；"
                    f"请简化任务或在 edit modal 改大（设 0 = 不限）。]"
                )
            else:
                # Hit the unlimited sentinel — should be VERY rare.
                # If we get here, per-tool budget caps + abort weren't
                # enough to stop the loop. Probably a model genuinely
                # stuck; user should investigate the agent's behavior.
                msg = (
                    f"[已强制终止 — 已连续调用工具 {max_turns} 轮仍未收敛。"
                    f"该 agent max_turns=0 (不限)，但 SDK 内部 sentinel "
                    f"({max_turns}) 触发了。这通常说明 agent 卡在某个工具"
                    f"调用循环里，请检查 agent 行为或在 edit modal 设"
                    f"显式上限。]"
                )
            return msg

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


def _compact_history_if_needed(tudou_agent):
    """Run TudouClaw's existing ``_summarize_old_history`` on the
    agent's messages, replacing in place if it compressed anything.

    SDK runtime never had history compaction wired (@user observed
    hist=123k chars / 51k tokens per turn before this fix). Legacy
    chat loop calls _summarize_old_history at every iteration (see
    agent.py:11416, 11587, 11774, etc.) so old turns get rolled up
    into a single summary system message; we mirror that here for
    SDK runtime by hooking the same function at the one entry point
    we have (start of _run_streamed, before history is converted to
    SDK items + sent to LLM).

    Defaults (from app.agent module-level constants):
      - TUDOU_HISTORY_SUMMARY_CHARS=25000 (soft trigger)
      - hard cap = 2× = 50000 chars (force-compress)
      - TUDOU_HISTORY_SUMMARY_KEEP_LAST=6 (recent msgs kept verbatim)
      - TUDOU_HISTORY_SUMMARY_OFF=1 → disable entirely

    Fail-safe: if summarize fails (LLM call timeout, parse error,
    etc.), legacy returns the same list unchanged and logs. We do
    the same — never block the chat turn on summarization.

    Mutates ``tudou_agent.messages`` in place (same as legacy at
    agent.py:11774) so the COMPRESSED version is what gets persisted
    to disk on the next _maybe_persist call — disk doesn't bloat.
    """
    try:
        from app.agent import _summarize_old_history
    except Exception as e:
        logger.debug("history compaction skipped (import failed): %s", e)
        return

    # ── SDK-specific tunings ──
    # 1. keep_last bumped 6 → 12. Tool-heavy turns under SDK can be
    #    1 user → asst.tool_calls → tool_result → asst.tool_calls →
    #    tool_result → asst.text = 6 messages for ONE conversational
    #    round. keep_last=6 then preserves just one round verbatim;
    #    12 covers ~2 rounds, which matches "the LLM should still
    #    see what just happened" intuition.
    # 2. Other params left at legacy defaults — they govern char-
    #    based thresholds, not message count, so SDK doesn't need
    #    to override them.
    SDK_KEEP_LAST = 12

    try:
        msgs_before = list(tudou_agent.messages or [])
        before_chars = sum(len(str(m.get("content") or ""))
                           for m in msgs_before)
        compacted = _summarize_old_history(
            msgs_before, tudou_agent,
            keep_last=SDK_KEEP_LAST,
        )
        if compacted is msgs_before:
            return  # nothing compressed; no need to reassign

        after_chars = sum(len(str(m.get("content") or ""))
                          for m in compacted)

        # ── Bloat guard (2026-05-16): if the summary made things
        # WORSE (e.g. LLM expanded narrative beyond what was being
        # compressed), abort and keep the original. Threshold: 80%
        # of original — anything above is below the "worth the
        # compression cost" line.
        if after_chars > int(before_chars * 0.8):
            logger.warning(
                "SDK runtime: history compaction REVERTED — summary "
                "(%d chars) is ≥80%% of original (%d chars). Most "
                "likely the 9-section template's narrative inflated "
                "rather than compressed. Keeping original. agent=%s",
                after_chars, before_chars,
                getattr(tudou_agent, "id", "?")[:8])
            return

        # Mutate in place — Agent.__setattr__ has a hook that re-routes
        # writes to ``self.messages`` into _messages_by_context, so the
        # context binding stays consistent.
        tudou_agent.messages = compacted
        logger.info(
            "SDK runtime: compacted history "
            "%d msgs / %d chars → %d msgs / %d chars "
            "(saved %.0f%%) agent=%s",
            len(msgs_before), before_chars,
            len(compacted), after_chars,
            (1 - after_chars / max(1, before_chars)) * 100,
            getattr(tudou_agent, "id", "?")[:8])
    except Exception as e:
        # Same fail-safe as legacy — never block the chat turn.
        logger.warning(
            "SDK runtime: history compaction failed (%s) — "
            "sending uncompressed history", e)


def _persist_sdk_items_to_messages(tudou_agent, new_items):
    """Convert SDK ``RunResult.new_items`` into legacy ChatCompletion-
    shape dicts and append to ``tudou_agent.messages``.

    Inverse of ``_tudou_messages_to_sdk_items`` (which goes the other
    direction for history input). Used at end-of-run to make sure
    intermediate turn state (asst with tool_calls, tool results) gets
    persisted, not just the final assistant text.

    Mapping:
      - ``tool_call_item.raw_item`` (Responses API function_call shape)
        → buffered until we know whether the NEXT message is a text
        asst reply, since legacy stores a single asst msg with both
        ``content`` AND ``tool_calls`` together
      - ``tool_call_output_item`` → tool-role message with
        ``tool_call_id`` and ``content``
      - ``message_output_item`` (assistant text)
        → either flushed alone (no preceding tool_calls) or merged
        into the buffered asst that holds the tool_calls

    SDK items arrive in this typical order per turn:
        [reasoning?] [text?] [tool_call+] [tool_output+] [reasoning?] [text]
    We flush whenever we hit a tool_call_output_item (end of a
    function_call group) or at the end of new_items.
    """
    if not new_items:
        return

    pending_asst_text = ""    # text from message_output_item that
                              # precedes / accompanies tool_calls in the
                              # SAME asst turn
    pending_tool_calls = []   # accumulated tool_call dicts for current
                              # asst turn
    appended_count = 0

    def _flush_asst():
        """Write the current pending asst message (text + tool_calls)
        if either field is non-empty."""
        nonlocal pending_asst_text, pending_tool_calls
        if not pending_asst_text and not pending_tool_calls:
            return
        msg = {
            "role": "assistant",
            "content": pending_asst_text or "",
            "_source": "sdk-runtime",
        }
        if pending_tool_calls:
            msg["tool_calls"] = pending_tool_calls
        try:
            tudou_agent.messages.append(msg)
        except Exception:
            pass
        pending_asst_text = ""
        pending_tool_calls = []

    for item in new_items:
        item_type = getattr(item, "type", "") or ""

        if item_type == "tool_call_item":
            # function_call from the asst. Pull the OpenAI ChatCompletion
            # shape we need: {id, type:"function", function:{name, args}}.
            raw = getattr(item, "raw_item", None)
            if raw is None:
                continue
            name = getattr(raw, "name", "") or ""
            args = getattr(raw, "arguments", "") or ""
            call_id = (
                getattr(raw, "call_id", "")
                or getattr(raw, "id", "")
                or "")
            if not call_id:
                continue
            pending_tool_calls.append({
                "id": call_id,
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": args if isinstance(args, str)
                                 else __import__("json").dumps(args),
                },
            })

        elif item_type == "tool_call_output_item":
            # First flush any pending asst (must precede the tool result
            # in chat-completion order).
            _flush_asst()
            # Output → tool-role message.
            raw = getattr(item, "raw_item", None)
            call_id = ""
            output = ""
            if isinstance(raw, dict):
                call_id = raw.get("call_id") or raw.get("id") or ""
                output = raw.get("output") or ""
            if not call_id:
                call_id = getattr(item, "call_id", "") or ""
            if not output:
                output = getattr(item, "output", "") or ""
            if not isinstance(output, str):
                try:
                    output = str(output)
                except Exception:
                    output = ""
            try:
                tudou_agent.messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": output,
                    "_source": "sdk-runtime",
                })
                appended_count += 1
            except Exception:
                pass

        elif item_type == "message_output_item":
            # Assistant text message. Buffer it (may be paired with
            # following tool_call_items in the same turn).
            raw = getattr(item, "raw_item", None)
            text = ""
            try:
                for part in (getattr(raw, "content", []) or []):
                    if hasattr(part, "text"):
                        text += part.text or ""
            except Exception:
                pass
            if text:
                pending_asst_text += text

        # reasoning_item → skipped (legacy doesn't persist CoT either)

    # Final flush at end of run.
    _flush_asst()
    logger.debug(
        "SDK persistence: appended %d intermediate item(s) to "
        "tudou_agent.messages", appended_count)


def _extract_text_for_preview(user_message):
    """Pull a string-only preview out of a (possibly multimodal)
    user message. Used for chat-UI log + nudge evaluator (both want
    plain text). Mirrors agent.py's _ensure_str_content but locally
    scoped so sdk_adapter doesn't depend on it."""
    if user_message is None:
        return ""
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, list):
        parts = []
        for block in user_message:
            if isinstance(block, dict):
                t = block.get("type")
                if t == "text" or t == "input_text":
                    parts.append(str(block.get("text") or ""))
                elif t in ("image_url", "image", "input_image"):
                    parts.append("[image]")
                else:
                    parts.append("[" + str(t or "block") + "]")
            elif isinstance(block, str):
                parts.append(block)
        return " ".join(p for p in parts if p)
    if isinstance(user_message, dict):
        # Single block dict
        return _extract_text_for_preview([user_message])
    return str(user_message)


def _convert_multimodal_blocks(blocks):
    """Convert OpenAI ChatCompletion-style multimodal content blocks
    (``[{type:"text",text:...}, {type:"image_url", image_url:{url:...}}]``)
    into SDK Responses-API input content parts
    (``[{type:"input_text", text:...}, {type:"input_image", image_url:str, detail:"auto"}]``).

    OpenAI ChatCompletion + SDK Responses-API use DIFFERENT type
    strings for the same content:
      text:        ``text`` ↔ ``input_text``
      image:       ``image_url`` ↔ ``input_image``
                   field:  ``image_url: {url: ...}`` ↔ ``image_url: str``
    Without this mapping, the SDK rejects the message with
    ``Unhandled item type or structure`` (same family of errors as
    when we tried to pass tool_call dicts directly back in commit
    e84f571).

    Audio / video / other block types not yet wired — strip with a
    text placeholder so the rest of the message still flows.
    """
    if not isinstance(blocks, list):
        return []
    out = []
    for block in blocks:
        if not isinstance(block, dict):
            if isinstance(block, str) and block.strip():
                out.append({"type": "input_text", "text": block})
            continue
        block_type = block.get("type", "")
        if block_type == "text":
            text = str(block.get("text") or "")
            if text:
                out.append({"type": "input_text", "text": text})
        elif block_type == "input_text":
            # Already in SDK format — pass through
            out.append(block)
        elif block_type in ("image_url", "image"):
            # OpenAI ChatCompletion format. Extract url.
            iu = block.get("image_url")
            url = ""
            if isinstance(iu, dict):
                url = iu.get("url", "")
            elif isinstance(iu, str):
                url = iu
            if url:
                out.append({
                    "type": "input_image",
                    "image_url": url,
                    "detail": "auto",
                })
        elif block_type == "input_image":
            # Already in SDK format
            out.append(block)
        else:
            # Unknown block type — drop with a text marker so the
            # LLM at least knows something was here.
            out.append({
                "type": "input_text",
                "text": f"[unsupported content type: {block_type}]",
            })
    return out


def _tudou_messages_to_sdk_items(messages):
    """Convert TudouClaw's ChatCompletion-style ``messages`` list
    into SDK Responses-API input items.

    TudouClaw shape (legacy):
        {"role": "user"|"assistant"|"system"|"tool",
         "content": str,
         "tool_calls": [...] (asst only),
         "tool_call_id": str (tool only),
         ...other metadata fields starting with "_" we ignore}

    SDK Responses-API input shape (what Runner.run accepts):
        - ``EasyInputMessageParam``: {"role", "content", "type": "message"}
          for user / assistant text messages
        - ``ResponseFunctionToolCallParam``: {"type": "function_call",
          "id", "call_id", "name", "arguments"} for each tool_call
          in an assistant message
        - ``FunctionCallOutput``: {"type": "function_call_output",
          "call_id", "output"} for tool-role messages

    System messages are EXCLUDED — the SDK Agent's ``instructions=``
    callable rebuilds the system prompt fresh each turn from live
    persona / skill / context state. Including persisted system
    messages would either double-count or staleout.

    Empty messages are filtered out (some providers / nudge paths
    leave empty assistant entries that would confuse the SDK).
    """
    items = []
    if not isinstance(messages, list):
        return items

    for m in messages:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        content = m.get("content")
        # Normalize content to string (multimodal lists become a
        # string fallback for history — SDK still handles current-turn
        # multimodal via tudou_model. Phase 3 work: preserve full
        # multimodal parts in history items too.)
        if content is not None and not isinstance(content, str):
            try:
                content = str(content)
            except Exception:
                content = ""

        if role in ("user", "developer"):
            # ── Multimodal pass-through (2026-05-16) ──
            # If the original content was a list (OpenAI ChatCompletion
            # multimodal format with text + image_url parts), convert
            # to SDK Responses-API parts. Otherwise keep the plain
            # string path (already handled by content normalization
            # above, which str()'d the list — but we re-fetch raw to
            # detect images).
            raw_content = m.get("content")
            if isinstance(raw_content, list) and raw_content:
                sdk_parts = _convert_multimodal_blocks(raw_content)
                if sdk_parts:
                    items.append({
                        "role": "user",
                        "content": sdk_parts,
                        "type": "message",
                    })
                    continue
            # Plain text path (or fall-through if multimodal stripped
            # to nothing useful).
            if content and content.strip():
                items.append({
                    "role": "user",
                    "content": content,
                    "type": "message",
                })

        elif role == "assistant":
            tool_calls = m.get("tool_calls") or []
            # Asst text part (if any). Some asst messages have ONLY
            # tool_calls and content="" — skip the text item in that
            # case, only emit the function_call items.
            # SDK's converter is STRICT about assistant content shape:
            # it must be a list of typed parts (NOT a plain string,
            # unlike user messages which accept strings). This is
            # because Responses-API distinguishes input vs output
            # message types — assistant output uses ``output_text``
            # parts. Pass a plain string here and chatcmpl_converter
            # blows up with "string indices must be integers".
            if content and content.strip():
                items.append({
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": content},
                    ],
                    "type": "message",
                })
            # Each tool_call → function_call item.
            for tc in tool_calls:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") or {}
                _id = str(tc.get("id") or "")
                if not _id:
                    # Skip malformed tool_call without id — SDK pairs
                    # function_call to function_call_output by call_id,
                    # an empty id would orphan both.
                    continue
                items.append({
                    "type": "function_call",
                    "id": _id,
                    "call_id": _id,
                    "name": str(fn.get("name") or ""),
                    "arguments": str(fn.get("arguments") or ""),
                })

        elif role == "tool":
            call_id = str(m.get("tool_call_id") or "")
            if not call_id:
                # Orphan tool result — skip to avoid the SDK / provider
                # rejecting the request with "no matching tool_call_id".
                continue
            items.append({
                "type": "function_call_output",
                "call_id": call_id,
                "output": str(content or ""),
            })

        # role == "system" → skipped (instructions= rebuilds it fresh)
        # Other roles → skipped (unknown to SDK / Responses API)

    return items


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
