"""read_file tool — read a text file with optional line offset/limit.

Extracted from app/tools.py as the first step of the per-tool split
(see app/tools_split/__init__.py for the migration plan). Schema for
this tool still lives in tools.TOOL_DEFINITIONS for now; only the
handler moved.
"""
from __future__ import annotations

from typing import Any

from .. import sandbox as _sandbox


def _tool_read_file(path: str, offset: int = 0, limit: int | None = None,
                    **_: Any) -> str:
    pol = _sandbox.get_current_policy()
    try:
        p = pol.safe_path(path)
    except _sandbox.SandboxViolation as e:
        return f"Error: {e}"
    if not p.exists():
        return f"Error: File not found: {path}"
    if not p.is_file():
        return f"Error: Not a file: {path}"
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    except Exception as e:
        return f"Error reading file: {e}"

    total = len(lines)
    start = max(0, offset)
    end = total if limit is None else min(total, start + limit)
    selected = lines[start:end]

    # Format with line numbers (1-based for human readability)
    numbered = []
    for i, line in enumerate(selected, start=start + 1):
        numbered.append(f"{i:>6}\t{line.rstrip()}")
    header = f"[{p} — lines {start + 1}-{end} of {total}]"
    return header + "\n" + "\n".join(numbered)
