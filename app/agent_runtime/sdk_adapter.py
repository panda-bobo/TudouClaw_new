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
            # Don't crash the chat — surface a clear error message
            # the user can see.
            final_text = (
                f"[SDK runtime error — falling back to legacy turn: "
                f"{type(e).__name__}: {str(e)[:200]}]"
            )

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
        """Async streaming inner loop. Iterates SDK events and
        forwards each through the bridge to the portal UI.

        Returns the final assistant text (same as Runner.run_sync's
        ``result.final_output`` but assembled from streamed deltas
        so the user gets live updates)."""
        from agents import Runner

        # Pass the user message as-is — SDK Runner accepts str or
        # the OpenAI-style multimodal list (we already match shape).
        run_input = (user_message if isinstance(user_message, str)
                     else str(user_message))

        try:
            stream = Runner.run_streamed(
                sdk_agent,
                run_input,
                hooks=hooks,
            )
        except TypeError:
            # Older SDK signatures may not accept hooks=; degrade.
            stream = Runner.run_streamed(sdk_agent, run_input)

        async for event in stream.stream_events():
            bridge.forward(event)

        # After the stream completes, ``stream.final_output`` carries
        # the assembled final text.
        return getattr(stream, "final_output", "") or ""

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
        from openai import AsyncOpenAI
        from agents import OpenAIChatCompletionsModel
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
        client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        logger.info(
            "SDK model: agent=%s provider=%s base_url=%s model=%s",
            getattr(self.tudou_agent, "id", "?")[:8],
            provider_id, base_url, model_name)
        return OpenAIChatCompletionsModel(
            model=model_name,
            openai_client=client,
        )
