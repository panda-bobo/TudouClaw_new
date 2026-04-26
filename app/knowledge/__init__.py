"""Knowledge layer — Karpathy-pattern wiki + future RAG / search.

Single source of truth for agent-authored knowledge:
  - ``wiki_store`` — markdown pages with YAML front-matter, indexed
    per scope (global / role:<name>), search via simple substring +
    tag overlap.

Older callers may still import from ``app.v2.knowledge`` (kept as a
back-compat shim that re-exports from this package). New code should
import from here directly.
"""
from .wiki_store import WikiStore, WikiPage, get_wiki_store, slugify, VALID_KINDS

__all__ = [
    "WikiStore", "WikiPage", "get_wiki_store",
    "slugify", "VALID_KINDS",
]
