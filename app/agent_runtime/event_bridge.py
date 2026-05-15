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

        TODO Phase 1: actual SDK event → portal event mapping.
        SDK event types include (per openai-agents 0.14+):
          - text_delta event → TudouClaw text_delta
          - tool_call_started → TudouClaw tool_call_start
          - tool_call_completed → TudouClaw tool_call_end
          - message_completed → TudouClaw message
          - run_completed → TudouClaw done
          - error events → bubble up to caller

        For now this is a SCAFFOLD that just logs receipt.
        """
        if self.on_event is None:
            return
        if self.abort_check and self.abort_check():
            return

        # PoC level: log + drop
        try:
            ev_type = getattr(sdk_event, "type", None) or "unknown"
            logger.debug("EventBridge received SDK event type=%s", ev_type)
        except Exception:
            pass

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

    def _emit(self, kind: str, data: dict) -> None:
        """Translate to legacy AgentEvent shape and forward."""
        if self.on_event is None:
            return
        try:
            from app.agent_types import AgentEvent
            self.on_event(AgentEvent(time.time(), kind, data))
        except Exception as e:
            logger.debug("EventBridge emit failed: %s", e)
