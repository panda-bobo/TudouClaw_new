"""Filesystem tools — read / write / edit / search / glob.

All five handlers share the sandbox policy (``_sandbox.get_current_policy``)
for path resolution and violation handling, so they live together here.
Schemas still live in ``tools.TOOL_DEFINITIONS``; only handlers moved.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any

from .. import sandbox as _sandbox


# Cap on number of match lines returned from ``search_files``. Larger
# than this the result is truncated with a trailing note — agents that
# hit the cap usually need a narrower pattern.
_SEARCH_MAX_MATCHES = 200

# Cap on number of paths returned from ``glob_files``.
_GLOB_MAX_RESULTS = 500

# Directories never worth walking for source-code searches. Skipped
# both by path check and when enumerating with ``os.walk``.
_SKIP_DIRS = frozenset({"node_modules", "__pycache__", ".git"})


# ── read_file ────────────────────────────────────────────────────────
#
# Per-turn dedup: agents (especially under heavy history compression)
# routinely lose track of files they already read this turn and re-read
# the same file 5-30 times — observed in 小专 audit logs, 30+ identical
# read_file calls on the same outline file across one task. Each redundant
# read costs one round-trip + LLM tokens for nothing. We cache results
# keyed by (caller_agent_id, abs_path, offset, limit) and return the
# cached body with a short marker on the second hit. Cache lives in a
# thread-local on the calling agent and is cleared at turn boundary
# (see Agent._reset_per_turn_caches).

_READ_FILE_CACHE_ATTR = "_read_file_turn_cache"


def _get_caller_agent(caller_agent_id: str):
    if not caller_agent_id:
        return None
    try:
        # Lazy import to avoid circular ref
        import sys as _sys
        _llm_mod = _sys.modules.get("app.llm")
        hub = getattr(_llm_mod, "_active_hub", None) if _llm_mod else None
        if hub is None:
            return None
        return hub.agents.get(caller_agent_id)
    except Exception:
        return None


def _tool_read_file(path: str, offset: int = 0, limit: int | None = None,
                    **ctx: Any) -> str:
    pol = _sandbox.get_current_policy()
    try:
        p = pol.safe_path(path)
    except _sandbox.SandboxViolation as e:
        return f"Error: {e}"
    if not p.exists():
        return f"Error: File not found: {path}"
    if not p.is_file():
        return f"Error: Not a file: {path}"

    # ── Per-turn dedup ──────────────────────────────────────────
    # Cache prior result for this (path, offset, limit) within the
    # turn. Second hit returns the same body with a short note so the
    # model sees "you already read this — stop reading it again".
    caller_id = ctx.get("_caller_agent_id", "") if isinstance(ctx, dict) else ""
    agent = _get_caller_agent(caller_id) if caller_id else None
    cache_key = (str(p), int(offset), int(limit) if limit else 0)
    if agent is not None:
        cache = getattr(agent, _READ_FILE_CACHE_ATTR, None)
        if cache is None:
            cache = {}
            try:
                setattr(agent, _READ_FILE_CACHE_ATTR, cache)
            except Exception:
                cache = None
        if cache is not None and cache_key in cache:
            cached_body, hit_count = cache[cache_key]
            cache[cache_key] = (cached_body, hit_count + 1)
            return (
                f"[REPEAT-READ #{hit_count + 1}] You already read this file "
                f"this turn. The body is unchanged — stop calling read_file "
                f"on it again. Use the content you already have, or fail "
                f"the step if it isn't enough.\n\n"
                + cached_body
            )

    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except Exception as e:
        return f"Error reading file: {e}"

    total = len(lines)
    start = max(0, offset)
    end = total if limit is None else min(total, start + limit)
    selected = lines[start:end]

    # 1-based line numbers for human readability.
    numbered = [f"{i:>6}\t{line.rstrip()}"
                for i, line in enumerate(selected, start=start + 1)]
    header = f"[{p} — lines {start + 1}-{end} of {total}]"
    body = header + "\n" + "\n".join(numbered)

    # Stash for next call's dedup hit.
    if agent is not None:
        cache = getattr(agent, _READ_FILE_CACHE_ATTR, None)
        if cache is not None:
            cache[cache_key] = (body, 1)

    return body


# ── write_file ───────────────────────────────────────────────────────

def _tool_write_file(path: str, content: str, **_: Any) -> str:
    pol = _sandbox.get_current_policy()
    try:
        p = pol.safe_path(path)
    except _sandbox.SandboxViolation as e:
        return f"Error: {e}"
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        # Return the resolved absolute path so the artifact system can
        # locate the file reliably (relative paths break file card downloads).
        return f"Successfully wrote {len(content)} bytes to {p}"
    except Exception as e:
        return f"Error writing file: {e}"


# ── edit_file ────────────────────────────────────────────────────────

def _tool_edit_file(path: str, old_string: str, new_string: str,
                    **_: Any) -> str:
    pol = _sandbox.get_current_policy()
    try:
        p = pol.safe_path(path)
    except _sandbox.SandboxViolation as e:
        return f"Error: {e}"
    if not p.exists():
        return f"Error: File not found: {path}"
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as e:
        return f"Error reading file: {e}"

    count = text.count(old_string)
    if count == 0:
        return f"Error: old_string not found in {path}"
    if count > 1:
        return (f"Error: old_string found {count} times in {path}. "
                "Must be unique. Provide more context.")

    new_text = text.replace(old_string, new_string, 1)
    p.write_text(new_text, encoding="utf-8")
    return f"Successfully edited {path} (replaced 1 occurrence)"


# ── search_files ─────────────────────────────────────────────────────

def _tool_search_files(pattern: str, path: str = ".", include: str = "",
                       **_: Any) -> str:
    pol = _sandbox.get_current_policy()
    try:
        base = pol.safe_path(path)
    except _sandbox.SandboxViolation as e:
        return f"Error: {e}"
    if not base.exists():
        return f"Error: Path not found: {path}"

    try:
        regex = re.compile(pattern)
    except re.error as e:
        return f"Error: Invalid regex pattern: {e}"

    matches: list[str] = []

    def _search_file(fpath: Path) -> None:
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                for lineno, line in enumerate(f, 1):
                    if regex.search(line):
                        matches.append(f"{fpath}:{lineno}: {line.rstrip()}")
                        if len(matches) >= _SEARCH_MAX_MATCHES:
                            return
        except (PermissionError, IsADirectoryError, OSError):
            # One bad file shouldn't abort the whole walk.
            pass

    if base.is_file():
        _search_file(base)
    else:
        for root, _dirs, files in os.walk(base):
            root_path = Path(root)
            parts = root_path.parts
            # Skip hidden dirs (those starting with '.') and known noise.
            if any(p.startswith(".") and p not in (".", "..") for p in parts):
                continue
            if any(p in _SKIP_DIRS for p in parts):
                continue

            for fname in files:
                if include and not fnmatch.fnmatch(fname, include):
                    continue
                _search_file(root_path / fname)
                if len(matches) >= _SEARCH_MAX_MATCHES:
                    break
            if len(matches) >= _SEARCH_MAX_MATCHES:
                break

    if not matches:
        return "No matches found."
    result = "\n".join(matches)
    if len(matches) >= _SEARCH_MAX_MATCHES:
        result += f"\n... (truncated at {_SEARCH_MAX_MATCHES} matches)"
    return result


# ── glob_files ───────────────────────────────────────────────────────

def _tool_glob_files(pattern: str, path: str = ".", **_: Any) -> str:
    pol = _sandbox.get_current_policy()
    try:
        base = pol.safe_path(path)
    except _sandbox.SandboxViolation as e:
        return f"Error: {e}"
    if not base.exists():
        return f"Error: Path not found: {path}"

    found = sorted(base.glob(pattern))
    # Filter out anything under a hidden directory.
    filtered = [
        str(f) for f in found
        if not any(part.startswith(".") and part not in (".", "..")
                   for part in f.parts)
    ]
    if not filtered:
        return "No files found."
    if len(filtered) > _GLOB_MAX_RESULTS:
        return ("\n".join(filtered[:_GLOB_MAX_RESULTS])
                + f"\n... ({len(filtered)} total, "
                f"showing first {_GLOB_MAX_RESULTS})")
    return "\n".join(filtered)
