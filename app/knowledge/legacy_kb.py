"""
Shared Knowledge Base — global reference entries accessible by all agents.

Each entry has:
  - id:      unique identifier (auto-generated)
  - title:   short name, e.g. "UI精美网站TOP10"
  - content: full reference text (multi-line)
  - tags:    optional keyword tags for search
  - created_at / updated_at: timestamps

Storage: ~/.tudou_claw/shared_knowledge.json

Agents can:
  1. See the list of available titles in their system prompt
  2. Read full content of specific entries via the `knowledge_lookup` tool
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger("tudou.knowledge")

# Data dir is resolved lazily so pytest fixtures can set
# TUDOU_CLAW_DATA_DIR (or TUDOU_CLAW_HOME) BEFORE the first read/write
# and have it take effect. The old hard-coded Path.home() ignored env
# and caused every test run to pollute the user's real shared_knowledge.json
# with fixture titles like "Python style guide", "X", "Updated", "A/B/C".
def _resolve_data_dir() -> Path:
    env = (os.environ.get("TUDOU_CLAW_DATA_DIR", "").strip()
           or os.environ.get("TUDOU_CLAW_HOME", "").strip())
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".tudou_claw"


def _data_dir() -> Path:
    return _resolve_data_dir()


def _knowledge_file() -> Path:
    return _data_dir() / "shared_knowledge.json"


# Legacy module-level aliases — keep for any imports reading them directly.
# IMPORTANT: these are snapshots at import time. New code paths should call
# _data_dir() / _knowledge_file() to honor env overrides set later.
_DATA_DIR = _resolve_data_dir()
_KNOWLEDGE_FILE = _DATA_DIR / "shared_knowledge.json"

# In-memory cache
_entries: list[dict] | None = None


def _ensure_dir():
    # Resolve at call time so pytest env overrides take effect even if
    # the module was imported before the fixture ran.
    _data_dir().mkdir(parents=True, exist_ok=True)


def _load() -> list[dict]:
    global _entries
    if _entries is not None:
        return _entries
    _ensure_dir()
    path = _knowledge_file()
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                _entries = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.error("Failed to load knowledge base: %s", e)
            _entries = []
    else:
        _entries = []
    return _entries


def _save():
    _ensure_dir()
    try:
        with open(_knowledge_file(), "w", encoding="utf-8") as f:
            json.dump(_entries or [], f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.error("Failed to save knowledge base: %s", e)


# ── Public API ──────────────────────────────────────────────

def list_entries() -> list[dict]:
    """Return all knowledge entries (full objects)."""
    return list(_load())


def list_titles() -> list[dict]:
    """Return lightweight list: [{id, title, tags}, ...]"""
    return [
        {"id": e["id"], "title": e["title"], "tags": e.get("tags", [])}
        for e in _load()
    ]


def get_entry(entry_id: str) -> Optional[dict]:
    """Get a single entry by ID."""
    for e in _load():
        if e["id"] == entry_id:
            return e
    return None


def search(query: str) -> list[dict]:
    """Search entries by title or tags (case-insensitive substring match)."""
    q = query.lower()
    results = []
    for e in _load():
        title = (e.get("title") or "").lower()
        tags = " ".join(e.get("tags") or []).lower()
        content = (e.get("content") or "").lower()
        if q in title or q in tags or q in content:
            results.append(e)
    return results


def _sync_to_vector(entry_id: str, title: str, content: str,
                     tags: list[str] | None = None, delete: bool = False):
    """Sync knowledge entry to ChromaDB vector store (if available)."""
    try:
        from ..core.memory import get_memory_manager
        mm = get_memory_manager()
        if delete:
            mm.vector_delete_knowledge(entry_id)
        else:
            mm.vector_store_knowledge(entry_id, title, content, tags)
    except Exception as e:
        # Vector sync is best-effort; don't break Knowledge Wiki operations
        logger.debug("Knowledge vector sync skipped: %s", e)


def add_entry(title: str, content: str, tags: list[str] | None = None) -> dict:
    """Add a new knowledge entry. Returns the created entry."""
    entries = _load()
    entry = {
        "id": uuid.uuid4().hex[:10],
        "title": title.strip(),
        "content": content.strip(),
        "tags": tags or [],
        "created_at": time.time(),
        "updated_at": time.time(),
    }
    entries.append(entry)
    _save()
    # Sync to vector store for semantic search
    _sync_to_vector(entry["id"], entry["title"], entry["content"], entry["tags"])
    logger.info("Knowledge entry added: %s (%s)", title, entry["id"])
    return entry


def update_entry(entry_id: str, title: str = None, content: str = None,
                 tags: list[str] = None) -> Optional[dict]:
    """Update an existing entry. Returns updated entry or None if not found."""
    for e in _load():
        if e["id"] == entry_id:
            if title is not None:
                e["title"] = title.strip()
            if content is not None:
                e["content"] = content.strip()
            if tags is not None:
                e["tags"] = tags
            e["updated_at"] = time.time()
            _save()
            # Sync updated content to vector store
            _sync_to_vector(e["id"], e["title"], e["content"], e.get("tags"))
            return e
    return None


def delete_entry(entry_id: str) -> bool:
    """Delete an entry by ID. Returns True if deleted."""
    global _entries
    entries = _load()
    before = len(entries)
    _entries = [e for e in entries if e["id"] != entry_id]
    if len(_entries) < before:
        _save()
        # Remove from vector store
        _sync_to_vector(entry_id, "", "", delete=True)
        return True
    return False


# ── For Agent System Prompt ─────────────────────────────────

def get_prompt_summary() -> str:
    """Build a short summary for injection into agent system prompts.

    Lists available knowledge titles so agents know what's available.
    Returns empty string if no entries exist.
    """
    entries = _load()
    if not entries:
        return ""
    lines = ["[Shared Knowledge Base — use `knowledge_lookup` tool to read details]"]
    for e in entries:
        tags_str = f" [{', '.join(e['tags'])}]" if e.get("tags") else ""
        lines.append(f"  • {e['title']}{tags_str}")
    return "\n".join(lines)
