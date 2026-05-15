"""Streaming-chunk filters — detect XML tool_call leaks mid-stream.

Mimo / Hermes / Functionary models sometimes emit
``<tool_call><function=NAME><parameter=KEY>VALUE</parameter></function></tool_call>``
as plain TEXT content instead of through the structured tool_calls
field. The post-stream parser (FunctionXMLParser at app/v2/bridges/
tool_parsers/builtin.py) extracts the structured call correctly,
but text_delta chunks have ALREADY drawn the raw XML into the chat
bubble character-by-character via the streaming UI.

This module's job is the LIVE detection: as chunks accumulate, scan
the rolling tail for XML markers. When detected, the caller stops
forwarding chunks to the UI and emits a retract_last_assistant
event so the bubble is wiped.

Both legacy chat loop (app/agent.py + app/agent_execution.py — 3
streaming paths) and the future SDK adapter call ``contains_tool_call_xml``
on each chunk. Single source of truth = no more "fix in 1 path,
forget the other 2" bugs.

History: extracted from inline streaming filter at agent.py:11695,
agent.py:13328, agent_execution.py:127 on 2026-05-15.
"""
from __future__ import annotations

# Markers we look for. Both must trigger detection because either
# alone is enough to confirm the LLM is leaking tool-call XML:
#   <tool_call>          — Hermes/Functionary outer wrapper
#   <function=NAME>      — function-name carrier (some variants skip
#                          the outer wrapper)
XML_TOOL_LEAK_MARKERS = (
    "<tool_call>",
    "<function=",
)


def contains_tool_call_xml(running_text_tail: str) -> bool:
    """True iff the rolling tail of streamed text contains a known
    XML-tool-call leak marker.

    Caller responsibility: pass the LAST ~100 chars of the
    accumulated text. Anything smaller risks missing markers split
    across chunks (e.g. 1-2-char tokens — '<tool_call>' is 11 chars
    so spans 5-10 chunks at worst).

    100 chars > any marker length, so detection is guaranteed once
    the marker is fully in the tail.

    Idempotent / cheap — pure substring checks. Safe to call on
    every chunk; once a caller's flag trips True, they should stop
    calling this for the remainder of the stream.
    """
    if not running_text_tail:
        return False
    return any(m in running_text_tail for m in XML_TOOL_LEAK_MARKERS)
