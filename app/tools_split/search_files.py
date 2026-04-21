"""search_files tool — grep-style regex search across files.

Extracted from app/tools.py as part of the per-tool split. Schema is
still in tools.TOOL_DEFINITIONS; only the handler moved here.
"""
from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any

from .. import sandbox as _sandbox


# Cap on number of match lines returned. Bigger than this the result is
# truncated with a trailing note — kept conservative because agents that
# get thousands of matches usually need a narrower pattern anyway.
_MAX_MATCHES = 200

# Directories never worth walking for source-code searches. Skipped
# both by path check and by default when enumerating with os.walk.
_SKIP_DIRS = frozenset({"node_modules", "__pycache__", ".git"})


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
                        if len(matches) >= _MAX_MATCHES:
                            return
        except (PermissionError, IsADirectoryError, OSError):
            # Silently skip unreadable files — search must be resilient
            # to one bad file in a large tree.
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
                if len(matches) >= _MAX_MATCHES:
                    break
            if len(matches) >= _MAX_MATCHES:
                break

    if not matches:
        return "No matches found."
    result = "\n".join(matches)
    if len(matches) >= _MAX_MATCHES:
        result += f"\n... (truncated at {_MAX_MATCHES} matches)"
    return result
