"""glob_files tool — list files matching a glob pattern.

Extracted from app/tools.py as part of the per-tool split. Schema is
still in tools.TOOL_DEFINITIONS; only the handler moved here.
"""
from __future__ import annotations

from typing import Any

from .. import sandbox as _sandbox


# Cap on number of paths returned to keep tool output bounded. Callers
# that need more should narrow the pattern.
_MAX_RESULTS = 500


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
    if len(filtered) > _MAX_RESULTS:
        return ("\n".join(filtered[:_MAX_RESULTS])
                + f"\n... ({len(filtered)} total, showing first {_MAX_RESULTS})")
    return "\n".join(filtered)
