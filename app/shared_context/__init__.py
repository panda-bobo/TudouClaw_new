"""Project-scoped shared context — the database approach to multi-agent
collaboration.

Replaces "push messages between agents" with "agents query a shared
database for what they need". Token cost of cross-agent state transfer
drops from O(content size) to O(reference + on-demand pull).

Five tables (project-scoped, all in the unified ``tudou_claw.db``):
  * ``sc_artifacts``     — file references with summary + token count
  * ``sc_decisions``     — structured decision log
  * ``sc_milestones``    — project goals/phases
  * ``sc_handoffs``      — agent → agent context transfer (pull, not push)
  * ``sc_pending_qs``    — Q&A queue between agents

Plus ``sc_agent_view`` — per-agent / per-table cursor for incremental sync.

See ``store.SharedContextStore`` for the public API.
"""
from .store import SharedContextStore, get_shared_context_store
from .budget_allocator import (
    get_agent_context, ContextBundle, SectionResult,
    DEFAULT_WEIGHTS, COMPLEX_WEIGHTS,
)

__all__ = [
    "SharedContextStore", "get_shared_context_store",
    "get_agent_context", "ContextBundle", "SectionResult",
    "DEFAULT_WEIGHTS", "COMPLEX_WEIGHTS",
]
