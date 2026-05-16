"""Event bridge — translates SDK stream events into TudouClaw's
portal UI event shape.

Why a translation layer:
  - Portal frontend expects AgentEvent(time, kind, data) objects
    with kinds like "text_delta" / "tool_call_start" /
    "tool_call_end" / "message" / "retract_last_assistant" / etc.
  - SDK's Runner.run_streamed yields SDK-native event objects
    (different shape, different field names)
  - Translating in one place means the frontend doesn't care which
    runtime is in use — same events arrive

Also applies B's stream-filter: if the SDK accidentally lets
``<tool_call><function=...>`` XML through as text_delta (mimo / Hermes
quirks the SDK might not catch upstream), we suppress those chunks
and emit retract_last_assistant — matching the legacy A-path
defense.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Callable, Optional

from app.runtime import contains_tool_call_xml

logger = logging.getLogger(__name__)


class EventBridge:
    """Stateful translator. One instance per Runner.run_streamed
    invocation. Holds the rolling text-tail buffer for XML leak
    detection across chunks."""

    def __init__(
        self,
        *,
        on_event: Optional[Callable] = None,
        abort_check: Optional[Callable[[], bool]] = None,
        tudou_agent=None,
    ):
        self.on_event = on_event
        self.abort_check = abort_check
        self.tudou_agent = tudou_agent
        # Rolling tail of streamed text for XML leak detection.
        # Same logic as legacy stream_filters: scan the last ~100
        # chars for `<tool_call>` / `<function=` markers; once
        # tripped, suppress all further text_delta forwarding +
        # emit retract_last_assistant.
        self._text_tail = ""
        self._xml_leak_tripped = False

    def forward(self, sdk_event: Any) -> None:
        """Translate one SDK event and forward to ``on_event``.

        SDK ``RunStreamEvent`` shapes (openai-agents 0.17+):
          - RawResponsesStreamEvent: low-level OpenAI Responses
            stream — includes text deltas as
            ResponseTextDeltaEvent (.data.delta = chunk)
          - RunItemStreamEvent: higher-level "an item completed"
            (message / tool_call / tool_call_output / etc.)
          - AgentUpdatedStreamEvent: a handoff happened

        We map:
          - text deltas → TudouClaw text_delta (chunk-level)
          - tool_call items → tool_call_start
          - tool_call_output items → tool_call_end
          - message_output items → MESSAGE event with full assembled
            text (for final / non-streamed text)
        """
        if self.on_event is None:
            return
        if self.abort_check and self.abort_check():
            return

        ev_type = getattr(sdk_event, "type", None) or ""

        try:
            # ── Raw response events (token-by-token text deltas) ──
            if ev_type == "raw_response_event":
                data = getattr(sdk_event, "data", None)
                if data is None:
                    return
                data_type = getattr(data, "type", "") or ""
                # Text delta
                if data_type == "response.output_text.delta":
                    chunk = getattr(data, "delta", "") or ""
                    if chunk:
                        self._emit_text_delta(chunk)
                    return
                # Tool call args streaming (deferred; expose on completion)
                # Other raw types: ignore (covered by run_item events)
                return

            # ── High-level run-item events ─────────────────────────
            if ev_type == "run_item_stream_event":
                item = getattr(sdk_event, "item", None)
                if item is None:
                    return
                item_type = getattr(item, "type", "") or ""

                if item_type in ("tool_call_item",
                                 "tool_call_output_item"):
                    # ── 2026-05-16 (streaming switch-back) ────────
                    # Skip — hooks (on_tool_start / on_tool_end in
                    # hooks.py) emit tool_call / tool_result with
                    # better timing (at actual dispatch moment +
                    # tool_arguments from ToolContext). Forwarding
                    # the item event too would double-render in UI +
                    # double-log to agent.events.
                    # Pre-streaming, this code path was the only
                    # source and was correct. Now hooks own it.
                    return

                if item_type == "message_output_item":
                    # Final assembled assistant message — for non-
                    # streamed text. Streamed text already went out
                    # via raw deltas above; this is harmless dup
                    # for fully-streamed responses but necessary
                    # when the SDK delivers a full message at once.
                    raw = getattr(item, "raw_item", None)
                    text = ""
                    try:
                        for part in getattr(raw, "content", []) or []:
                            if hasattr(part, "text"):
                                text += part.text or ""
                    except Exception:
                        pass
                    if text:
                        self._emit("message", {
                            "role": "assistant",
                            "content": text,
                        })
                    return

            # ── Agent handoff ──────────────────────────────────────
            if ev_type == "agent_updated_stream_event":
                new_agent = getattr(sdk_event, "new_agent", None)
                self._emit("nudge", {
                    "reason": "sdk_handoff",
                    "to": getattr(new_agent, "name", "?"),
                })
                return

            # Anything else — drop (debug log)
            logger.debug("EventBridge: unhandled SDK event type=%s", ev_type)
        except Exception as e:
            logger.warning(
                "EventBridge.forward error on event type=%s: %s",
                ev_type, e)

    def _emit_text_delta(self, chunk: str) -> None:
        """Internal: forward a text_delta chunk WITH XML leak guard."""
        if not chunk:
            return
        self._text_tail = (self._text_tail + chunk)[-200:]

        if not self._xml_leak_tripped:
            if contains_tool_call_xml(self._text_tail[-100:]):
                self._xml_leak_tripped = True
                self._emit_retract("tool_call_xml_in_stream")
                return  # don't forward this chunk

        if self._xml_leak_tripped:
            return  # subsequent chunks suppressed

        self._emit("text_delta", {"content": chunk})

    def _emit_retract(self, reason: str) -> None:
        self._emit("retract_last_assistant", {"reason": reason})

    # Events that are TOO FREQUENT to persist to agent.events on every
    # emit — they're for live streaming UI only, not restart replay.
    # text_delta fires per-token (potentially hundreds per turn); if we
    # _log every one, agent.events would grow ~30× larger than legacy
    # and the chat-UI replay would render the same bubble character-
    # by-character on every page load. The bridge still emits these
    # to on_event (live SSE) — just doesn't persist.
    _EPHEMERAL_KINDS = frozenset({"text_delta"})

    def _emit(self, kind: str, data: dict) -> None:
        """Translate to legacy AgentEvent shape, persist via _log
        (except ephemeral kinds), and forward to portal via on_event.

        ── Persist to agent.events (2026-05-16) ──
        Without this, SDK runtime events only went to the live SSE
        stream — they did NOT land in ``agent.events`` for restart
        replay. After a backend restart, /api/portal/agent/{id}/events
        returned an empty list, so the chat UI looked blank even
        though agent.messages still had the conversation. @user
        reported this as "history 记录又没了".

        Legacy chat loop does both at every event emission site
        (agent.py:12620 + 12758): ``self._log(evt.kind, evt.data)`` +
        ``_emit(evt)``. Mirror that here for semantic events
        (tool_call / tool_result / message / retract / nudge);
        skip persistence for ephemeral high-volume streaming events
        like text_delta (see ``_EPHEMERAL_KINDS``).
        """
        if kind not in self._EPHEMERAL_KINDS:
            try:
                if self.tudou_agent is not None and hasattr(self.tudou_agent, "_log"):
                    self.tudou_agent._log(kind, data)
            except Exception as e:
                logger.debug("EventBridge _log to agent.events failed: %s", e)

            # ── Mid-run persistence (2026-05-16 evening) ──────────────
            # Legacy chat loop calls _maybe_persist at every iteration
            # boundary so a SIGKILL / abort mid-run doesn't lose data.
            # SDK runtime previously only persisted at end-of-run
            # (in SDKAgentRunner.run finalization). If the process
            # died mid-stream, the user msg + any partial tool calls
            # would be in memory but not on disk.
            #
            # _maybe_persist is throttled (default 1s between saves)
            # so calling it on every semantic event doesn't pound the
            # disk. Final force-persist in run() still fires for the
            # guaranteed end-of-turn save.
            try:
                if self.tudou_agent is not None and hasattr(
                        self.tudou_agent, "_maybe_persist"):
                    self.tudou_agent._maybe_persist()
            except Exception as e:
                logger.debug(
                    "EventBridge mid-run _maybe_persist failed: %s", e)

        if self.on_event is None:
            return
        try:
            from app.agent_types import AgentEvent
            self.on_event(AgentEvent(time.time(), kind, data))
        except Exception as e:
            logger.debug("EventBridge emit failed: %s", e)
