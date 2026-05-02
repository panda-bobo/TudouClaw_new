"""
Sliding-window dedup for assistant message emits in the agent chat loop.

Extracted from ``agent.py:_emit`` (HANDOFF [B]) so the dedup logic is
unit-testable without spinning up a real Agent + LLM mock.

Usage:

    state = EmitDedupState()
    allow, suppressed_for_s = state.should_emit_assistant(content)
    if not allow:
        # log + skip
        ...
    else:
        # forward to on_event
        ...

The state object is one-per-chat-turn (created at the top of
``Agent.chat()`` and discarded at end of turn) so the ring doesn't
leak across unrelated conversations. 60s TTL on entries handles the
edge case where a turn lasts longer than expected.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Optional


def fingerprint(content: str) -> tuple[str, str]:
    """Normalize an assistant message to (head300, full_stripped).

    Whitespace runs collapse so "X\\n\\nY" and "X Y" match. Leading /
    trailing whitespace stripped. The head300 is used as a quick
    equality check; full is used for mutual-prefix matching.
    """
    stripped = (content or "").strip()
    normalized = " ".join(stripped.split())
    return normalized[:300], normalized


@dataclass
class EmitDedupState:
    """One-per-turn dedup state. Holds a sliding ring of recent emits."""

    ring_size: int = 5
    ttl_seconds: float = 60.0
    # Internal: list of (timestamp, head300, full)
    _ring: list[tuple[float, str, str]] = field(default_factory=list)

    def should_emit_assistant(
        self, content: str, *, now: Optional[float] = None
    ) -> tuple[bool, Optional[float]]:
        """Decide whether to emit this assistant message.

        Returns ``(allow, dup_age_seconds)``:
          * ``(True, None)`` — pass through; the message is unique
            within the current ring (or empty — empty assistant
            messages always pass as turn markers).
          * ``(False, age)`` — suppress; matches a prior emit that
            happened ``age`` seconds ago.

        Match semantics: exact head300 OR mutual prefix (one full is
        a prefix of the other — covers the "streamed chunk vs final"
        replay case). Mirrors the front-end ring buffer at
        ``portal_bundle.js:4285``.
        """
        head, full = fingerprint(content)
        if not full:
            return True, None  # empty messages always pass
        if now is None:
            now = time.time()

        # Evict expired entries
        self._ring = [e for e in self._ring if (now - e[0]) <= self.ttl_seconds]

        for (ts, ehead, efull) in self._ring:
            if ehead == head or full.startswith(efull) or efull.startswith(full):
                return False, now - ts

        # Pass — record and bound the ring size
        self._ring.append((now, head, full))
        if len(self._ring) > self.ring_size:
            self._ring.pop(0)
        return True, None
