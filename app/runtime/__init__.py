"""TudouClaw runtime helpers — shared between legacy chat loop and
future OpenAI Agents SDK adapter.

Architecture (per docs/MIGRATION_OPENAI_AGENTS_SDK.md):

    A: app/agent.py:Agent.chat()   ← legacy self-rolled chat loop
    B: app/runtime/                ← THIS PACKAGE — shared helpers
    C: app/agent_runtime/          ← future SDK adapter (not yet written)

Both A and C call into B. B is pure-function / dataclass land:
no agent state, no I/O, no Hub access. That makes it trivially
testable and lets the future SDK adapter share exactly the same
intent detection + nudge logic + stream filtering as the legacy
loop, so per-agent behavior stays identical when admins toggle
runtime mode.

Modules:

  intent.py         — user-message intent detectors (retrieval /
                      wiki-write / verification request)
  nudges.py         — nudge evaluation + tool-error / completion
                      detection helpers
  stream_filters.py — XML-tool-call leak detection in streamed
                      chunks (mimo / Hermes / Functionary defense)
  narrator.py       — "let me X:" stall detector

Public re-exports below — both legacy and SDK adapter import from
``app.runtime`` (not from the submodules) so we can move things
around inside the package without breaking callers.
"""
from .intent import (
    user_explicitly_requests_retrieval,
    user_explicitly_requests_wiki_write,
    user_asked_for_verification,
)
from .nudges import (
    agent_claimed_completion,
    agent_ran_verification_this_turn,
    detect_recent_tool_error,
)
from .stream_filters import (
    contains_tool_call_xml,
    XML_TOOL_LEAK_MARKERS,
)
from .narrator import looks_like_narrator_stall

__all__ = [
    "user_explicitly_requests_retrieval",
    "user_explicitly_requests_wiki_write",
    "user_asked_for_verification",
    "agent_claimed_completion",
    "agent_ran_verification_this_turn",
    "detect_recent_tool_error",
    "contains_tool_call_xml",
    "XML_TOOL_LEAK_MARKERS",
    "looks_like_narrator_stall",
]
