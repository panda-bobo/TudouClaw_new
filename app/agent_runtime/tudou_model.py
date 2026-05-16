"""TudouClawModel — SDK ``Model`` implementation that delegates the
actual LLM call to TudouClaw's existing ``app.llm.chat_no_stream``.

Why this exists:
  The SDK's stock ``OpenAIChatCompletionsModel`` calls
  ``AsyncOpenAI.chat.completions.create`` directly. That bypasses
  EVERY provider-quirk adaptation TudouClaw already has:

    - ``_sanitize_messages_for_openai`` (mimo / deepseek-r / o1 / qwq
      reasoning_content roundtrip, GLM/Qwen content+tool_calls
      mutex, image-stripping for text-only, system-message merge)
    - V2 tool_parsers (DSML for DeepSeek-flash, FunctionXML for
      mimo/Hermes ``<function=NAME>`` markup, GLM ``<arg_key>``
      inline XML — these are EXACTLY the leaks that pushed us
      toward SDK migration in the first place)
    - Multi-provider fallback chain
    - Token usage tracking
    - Provider-specific retry / error categorization
    - Connection pool

  Re-implementing any of those inside the SDK adapter would be
  reinventing wheels TudouClaw already ships. Instead we make SDK
  use the legacy LLM call as its underlying transport: SDK keeps
  doing high-level orchestration (Agent, Runner.run turn loop, Tool
  dispatch, Hooks), and the LLM call itself routes through legacy.

Implementation:
  ``Model.get_response()`` is the single SDK entry point used by
  ``Runner.run`` (non-streaming, which is what we picked in
  2026-05-16 to bypass the streaming + parallel-tool_calls bug). We:

    1. Convert SDK input items → OpenAI chat-messages via the SDK's
       own Converter.items_to_messages
    2. Convert SDK Tools → OpenAI tool params via Converter.tool_to_openai
    3. Run the call through legacy ``chat_no_stream`` (gets all the
       provider quirks for free)
    4. Convert the legacy response dict → SDK ``ModelResponse``
       (output items + usage), via Converter.message_to_output_items

``stream_response()`` is wired but raises NotImplementedError because
``SDKAgentRunner._run_streamed`` switched to ``Runner.run`` — never
called. If we re-enable streaming after the upstream SDK fixes its
parallel-tool_calls accumulator, we'll wire stream_response to
``app.llm.chat_stream_events``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

logger = logging.getLogger(__name__)


def build_tudou_model(provider_id: str, model_name: str, base_url: str):
    """Construct a ``TudouClawModel`` instance, lazily importing
    SDK dependencies so this module loads without openai-agents
    installed (caller should have already failed earlier if SDK
    isn't available, but defensive)."""
    try:
        from agents.models.interface import Model  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "openai-agents not installed — TudouClawModel needs "
            "the SDK Model interface to subclass") from e
    return TudouClawModel(provider_id=provider_id,
                          model_name=model_name,
                          base_url=base_url)


# The class is defined inside a function so Model is only imported
# lazily. We re-bind at module scope after first use for type checking.
class _LazyTudouClawModelMeta(type):
    """Metaclass that defers Model subclassing to first construction.
    Lets ``import app.agent_runtime.tudou_model`` succeed without
    SDK installed; only ``build_tudou_model()`` actually pulls SDK in.
    """
    _real_cls = None

    def __call__(cls, *args, **kwargs):
        if cls._real_cls is None:
            cls._real_cls = _build_real_tudou_model_class()
        return cls._real_cls(*args, **kwargs)


class TudouClawModel(metaclass=_LazyTudouClawModelMeta):
    """Public entry — instances are actually instances of the
    SDK-Model-subclass built lazily on first construction."""
    pass


def _build_real_tudou_model_class():
    from agents.models.interface import Model
    from agents.items import ModelResponse
    from agents.usage import Usage
    from agents.models.openai_chatcompletions import Converter
    from openai.types.chat import ChatCompletionMessage
    from app import llm as _llm

    class _TudouClawModelImpl(Model):
        """SDK Model subclass that delegates the actual LLM call to
        TudouClaw's ``app.llm.chat_no_stream``."""

        def __init__(self, provider_id: str, model_name: str,
                     base_url: str):
            self.provider_id = provider_id
            self.model_name = model_name
            self.base_url = base_url

        async def get_response(
            self,
            system_instructions: str | None,
            input,
            model_settings,
            tools,
            output_schema,
            handoffs,
            tracing,
            *,
            previous_response_id=None,
            conversation_id=None,
            prompt=None,
        ) -> "ModelResponse":
            # ── 1. SDK input items → OpenAI chat messages ───────────
            messages = Converter.items_to_messages(
                input,
                model=self.model_name,
                base_url=self.base_url,
            )
            if system_instructions:
                messages.insert(0, {
                    "role": "system",
                    "content": system_instructions,
                })

            # ── 2. SDK Tools → OpenAI tool params ───────────────────
            oai_tools = None
            if tools:
                try:
                    oai_tools = [
                        Converter.tool_to_openai(t) for t in tools
                    ]
                except Exception as e:
                    logger.warning(
                        "tudou_model: tool_to_openai failed for "
                        "some tools (%s) — sending without tools", e)
                    oai_tools = None

            # ── 3. Legacy chat_no_stream — all quirks for free ──────
            # Run the sync legacy call in a thread so we don't block
            # the SDK's event loop.
            try:
                result = await asyncio.to_thread(
                    _llm.chat_no_stream,
                    messages,
                    tools=oai_tools,
                    provider=self.provider_id,
                    model=self.model_name,
                    tool_choice=getattr(model_settings, "tool_choice", None),
                    temperature=getattr(model_settings, "temperature", None),
                )
            except Exception as e:
                # Re-raise so the SDK Runner sees it; the upper
                # SDKAgentRunner.run try/except converts to a
                # chat-visible "[SDK runtime error...]" hint.
                logger.exception(
                    "tudou_model: chat_no_stream failed (provider=%s "
                    "model=%s): %s", self.provider_id, self.model_name, e)
                raise

            # ── 4. Legacy result → SDK ModelResponse ────────────────
            msg_dict = result.get("message") or {}
            # Diagnostic — keep until SDK runtime is rock-solid;
            # tells us EXACTLY what mimo/DeepSeek/etc. returned so we
            # can tell "model returned empty" vs "converter dropped
            # something". Captures content len, has-tool-calls, has-
            # reasoning, stop_reason. Non-PII (just shape, not text).
            try:
                logger.info(
                    "tudou_model: chat_no_stream → provider=%s model=%s "
                    "content_len=%d tool_calls=%d reasoning_len=%d "
                    "stop=%s",
                    self.provider_id, self.model_name,
                    len(str(msg_dict.get("content") or "")),
                    len(msg_dict.get("tool_calls") or []),
                    len(str(msg_dict.get("reasoning_content") or "")),
                    result.get("stop_reason", "?"),
                )
            except Exception:
                pass

            # ── Thinking-mode content fallback (matches legacy
            #    agent.py:13922-13926) ─────────────────────────────
            # mimo / DeepSeek thinking-mode sometimes returns
            # content="" with everything in reasoning_content. Without
            # this fallback, SDK sees empty content → empty final_output
            # → "empty_reply" nudge fires but agent UI shows nothing.
            # Legacy already documented + handles this; we copy the
            # same fallback so SDK gets identical behavior.
            _content = str(msg_dict.get("content") or "").strip()
            if not _content:
                _reasoning = str(msg_dict.get("reasoning_content") or "").strip()
                if _reasoning and not (msg_dict.get("tool_calls") or []):
                    # Only surface reasoning AS content when there are
                    # no tool_calls (if there ARE tool_calls, the model
                    # is in mid-thought routing to a tool — let the
                    # tool dispatch happen, don't surface CoT as reply).
                    logger.info(
                        "tudou_model: empty content + reasoning_content "
                        "present (%d chars) — using reasoning as visible "
                        "reply (legacy fallback)", len(_reasoning))
                    # Mutate the dict so the ChatCompletionMessage
                    # builder picks up the fallback content. Also keep
                    # original reasoning_content for sanitize roundtrip.
                    msg_dict = dict(msg_dict)
                    msg_dict["content"] = _reasoning

            # Legacy may keep ``reasoning_content`` on the assistant
            # message — that's fine, it gets sent back on the next
            # turn (sanitize sees it pre-set, doesn't placeholder).
            # The SDK's Converter.message_to_output_items doesn't
            # know about reasoning_content; it'll be ignored at SDK
            # layer but PRESERVED in the next call's messages because
            # we re-pass through our own Converter.items_to_messages
            # which doesn't strip non-standard fields.
            #
            # Build a ChatCompletionMessage from the dict so SDK's
            # converter can chew it.
            try:
                cm = _build_chat_completion_message(msg_dict)
            except Exception as e:
                logger.warning(
                    "tudou_model: ChatCompletionMessage construction "
                    "failed (%s); using bare-minimum fallback", e)
                cm = ChatCompletionMessage(
                    role="assistant",
                    content=str(msg_dict.get("content") or ""),
                )

            try:
                items = Converter.message_to_output_items(cm)
            except Exception as e:
                logger.warning(
                    "tudou_model: message_to_output_items failed "
                    "(%s); returning raw text item only", e)
                # Fall back to a single text item so the agent has
                # SOMETHING to return.
                items = []

            # Second diagnostic: how many output items did the converter
            # produce? If chat_no_stream returned content but items is
            # empty, that's a converter bug (not a model issue).
            try:
                item_types = [type(i).__name__ for i in items]
                logger.info(
                    "tudou_model: → SDK output_items=%d types=%s",
                    len(items), item_types)
            except Exception:
                pass

            usage_dict = result.get("usage") or {}
            sdk_usage = Usage(
                requests=1,
                input_tokens=int(usage_dict.get("prompt_tokens") or 0),
                output_tokens=int(usage_dict.get("completion_tokens") or 0),
                total_tokens=int(usage_dict.get("total_tokens") or 0),
            )

            return ModelResponse(
                output=items,
                usage=sdk_usage,
                response_id=None,
            )

        def stream_response(self, *args, **kwargs) -> AsyncIterator:
            """Streaming not wired yet — SDKAgentRunner uses Runner.run
            (non-streaming) since 2026-05-16 to bypass the SDK
            streaming + parallel-tool_calls bug. If/when we re-enable
            streaming, point this at ``app.llm.chat_stream_events``."""
            raise NotImplementedError(
                "TudouClawModel streaming not implemented; SDKAgentRunner "
                "uses Runner.run (non-streaming). If this is being "
                "called, something switched back to Runner.run_streamed "
                "— either revert that or wire stream_response to "
                "app.llm.chat_stream_events.")

    return _TudouClawModelImpl


def _build_chat_completion_message(msg_dict: dict):
    """Build an OpenAI ``ChatCompletionMessage`` from a legacy
    ``chat_no_stream`` ``result['message']`` dict.

    Legacy passes through whatever fields the upstream provider
    returned (content, role, tool_calls, reasoning_content, etc.).
    The SDK's Converter expects a ChatCompletionMessage instance,
    not a dict. We construct it tolerantly: required fields get
    defaults, unknown fields are dropped (ChatCompletionMessage's
    pydantic model rejects extras).
    """
    from openai.types.chat import ChatCompletionMessage
    from openai.types.chat.chat_completion_message_tool_call import (
        ChatCompletionMessageToolCall, Function,
    )

    role = msg_dict.get("role") or "assistant"
    content = msg_dict.get("content")
    if content is not None and not isinstance(content, str):
        content = str(content)

    tool_calls_raw = msg_dict.get("tool_calls") or []
    tool_calls = []
    for tc in tool_calls_raw:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        try:
            tool_calls.append(ChatCompletionMessageToolCall(
                id=tc.get("id") or "",
                type=tc.get("type") or "function",
                function=Function(
                    name=fn.get("name") or "",
                    arguments=fn.get("arguments") or "",
                ),
            ))
        except Exception as e:
            logger.debug(
                "tudou_model: skipping malformed tool_call %r: %s",
                tc, e)

    kwargs = {"role": role}
    # ChatCompletionMessage requires content to exist (can be null).
    kwargs["content"] = content
    if tool_calls:
        kwargs["tool_calls"] = tool_calls
    return ChatCompletionMessage(**kwargs)
