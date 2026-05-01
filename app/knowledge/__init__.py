"""Knowledge layer — Karpathy-pattern wiki + JSON-backed reference KB.

Two stores live side-by-side here:

  * ``wiki_store`` — markdown pages with YAML front-matter, indexed
    per scope (global / role:<name>). Used by the new wiki tools.
  * ``legacy_kb`` — flat JSON list of reference entries
    (~/.tudou_claw/shared_knowledge.json). Used by the
    ``knowledge_lookup`` tool, experience library, and core/memory
    indexing.

Both are re-exported at the package level so callers that historically
did ``from app import knowledge`` and called ``knowledge.get_entry()``
or ``knowledge.search()`` keep working — these used to live in
``app/knowledge.py`` (a sibling module), but the package directory
shadows that path. Re-exporting here is the single fix that restores
both APIs in one place.

Older callers may also import from ``app.v2.knowledge`` (kept as a
back-compat shim that re-exports from this package).
"""
from .wiki_store import WikiStore, WikiPage, get_wiki_store, slugify, VALID_KINDS

# Legacy reference-entry KB — used by knowledge_lookup, experience_library,
# core/memory indexer. Re-exported at package level so
# ``from app import knowledge; knowledge.get_entry(...)`` resolves.
from .legacy_kb import (
    list_entries,
    list_titles,
    get_entry,
    search,
    add_entry,
    update_entry,
    delete_entry,
    get_prompt_summary,
)

__all__ = [
    # Wiki API
    "WikiStore", "WikiPage", "get_wiki_store",
    "slugify", "VALID_KINDS",
    # Legacy reference-KB API
    "list_entries", "list_titles", "get_entry", "search",
    "add_entry", "update_entry", "delete_entry",
    "get_prompt_summary",
]
