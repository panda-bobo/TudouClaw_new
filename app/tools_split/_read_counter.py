"""Cross-tool read counter — Day 3 AM (2026-05-05).

Single source of truth for "how many times has this agent read this
path in the current turn". Counts are stored on the Agent instance
under ``_xtool_read_path_counts`` so:

  * read_file (fs.py) — bumps on every call
  * bash (system.py) — bumps when the command is a read-class
    primitive (cat / head / tail / less / more / bat / nl / sed -n /
    awk on a single file)

When the count for a (path) crosses ``HARD_CAP``, ``is_blocked``
returns True and the calling tool MUST short-circuit with the
``BLOCKED_MESSAGE_TEMPLATE`` instead of executing.

This closes the loophole where an agent worked around fs.py's
read-valve by switching to ``bash cat <path>``.
"""
from __future__ import annotations

import os
import re
import shlex
from typing import Any

# Threshold (hard). Soft warn handled by fs.py's existing _path_caps.
HARD_CAP_DEFAULT = 5

_ATTR = "_xtool_read_path_counts"

# Bash primitives that READ a file as their first positional path arg.
# We extract path from `cat foo`, `head -n 50 foo`, `less foo`, etc.
_READ_BIN_NAMES = frozenset({
    "cat", "head", "tail", "less", "more", "bat", "nl",
})

# `sed -n '1,50p' file` and `awk '/.../' file` — handled separately
# (sed -n needs script extracted, awk likewise).


def _hard_cap() -> int:
    """Resolution order:
    1. env TUDOU_XTOOL_READ_CAP (escape hatch — wins over UI setting)
    2. system_settings.agent_guardrails.read_valve_hard_cap
    3. HARD_CAP_DEFAULT (5)
    """
    # 1. env override
    try:
        env_v = os.getenv("TUDOU_XTOOL_READ_CAP", "").strip()
        if env_v:
            v = int(env_v)
            if v >= 1:
                return v
    except Exception:
        pass
    # 2. system_settings (admin-tunable via Settings UI)
    try:
        from .. import system_settings as _ss_mod
        store = _ss_mod.get_store()
        if store is not None:
            v = int(store.get("agent_guardrails.read_valve_hard_cap",
                              HARD_CAP_DEFAULT) or HARD_CAP_DEFAULT)
            if v >= 1:
                return v
    except Exception:
        pass
    return HARD_CAP_DEFAULT


def _normalize_path(p: str) -> str:
    """Stable identity for a path — strips ./, ~/, normalizes seps.
    Used as the counter key. Doesn't resolve symlinks (cheap)."""
    if not p:
        return ""
    s = p.strip()
    if not s:
        return ""
    s = os.path.expanduser(s)
    s = os.path.normpath(s)
    if os.path.isabs(s):
        try:
            return os.path.realpath(s)
        except OSError:
            return s
    return s


def _ensure(agent: Any) -> dict[str, int] | None:
    if agent is None:
        return None
    counts = getattr(agent, _ATTR, None)
    if counts is None:
        counts = {}
        try:
            setattr(agent, _ATTR, counts)
        except Exception:
            return None
    return counts


def bump_read(agent: Any, path: str, *, source: str = "?") -> int:
    """Increment counter for (agent, path). Returns the new count.

    ``source`` is a tag for logging: ``"read_file"``, ``"bash:cat"``,
    etc. — surfaced when the cap trips so the agent sees WHICH form
    of read is being counted.
    """
    counts = _ensure(agent)
    if counts is None:
        return 0
    key = _normalize_path(path)
    if not key:
        return 0
    n = counts.get(key, 0) + 1
    counts[key] = n
    return n


def get_count(agent: Any, path: str) -> int:
    counts = _ensure(agent)
    if counts is None:
        return 0
    return counts.get(_normalize_path(path), 0)


def is_blocked(agent: Any, path: str) -> bool:
    return get_count(agent, path) > _hard_cap()


def reset(agent: Any) -> None:
    """Reset counters (called per turn boundary)."""
    counts = _ensure(agent)
    if counts is not None:
        counts.clear()


def blocked_message(path: str, count: int, source: str) -> str:
    cap = _hard_cap()
    return (
        f"[READ-VALVE-TRIPPED #{count}] You have read '{path}' {count} "
        f"times this turn (cap={cap}, includes ALL forms: read_file + "
        f"bash cat/head/tail/less/more). Refusing further reads. "
        f"Most recent attempt was via: {source}.\n"
        f"WHAT TO DO: stop reading. Use the content you already have. "
        f"If you need fresh data, finish this turn first (counters "
        f"reset on the next user message)."
    )


# ── bash command parsing ─────────────────────────────────────────────

# Operators that split a compound command — we only want to inspect
# the leftmost simple-command token. Conservative: don't try to
# emulate bash; just look at the first command of the line.
_SPLIT_OPS = re.compile(r"[;&|]+|\|\||&&|>+|<+")


def extract_read_path_from_bash(command: str) -> tuple[str, str] | None:
    """If ``command`` starts with a read-class primitive followed by a
    file path, return ``(abs_or_rel_path, source_label)``. Otherwise
    return None.

    Examples that match:
      ``cat /tmp/foo.md``                 → ("/tmp/foo.md", "bash:cat")
      ``head -n 50 ../README.md``         → ("../README.md", "bash:head")
      ``tail -f log.txt``                 → ("log.txt", "bash:tail")
      ``less file with spaces.md``        → ...
      ``sed -n '1,80p' notes.md``         → ("notes.md", "bash:sed")
      ``awk '/^TODO/' tasks.md``          → ("tasks.md", "bash:awk")

    Examples that do NOT match (correctly):
      ``cat a b c``        — multiple files; not a single-target read
      ``echo foo``         — not a read primitive
      ``cat foo | grep x`` — pipe-chained; only the leftmost is examined,
                              still extracts foo (still counts as a read)
    """
    if not command or not isinstance(command, str):
        return None
    # Take only the leftmost segment before first split operator.
    head_seg = _SPLIT_OPS.split(command, maxsplit=1)[0].strip()
    if not head_seg:
        return None
    try:
        toks = shlex.split(head_seg, posix=True)
    except ValueError:
        # Unbalanced quotes — treat as opaque, don't count.
        return None
    if not toks:
        return None
    bin_name = os.path.basename(toks[0])
    # Skip env prefix: `env FOO=1 cat file` → unwrap
    while bin_name == "env" and len(toks) > 1:
        toks = toks[1:]
        # Skip VAR=VAL pairs
        while toks and "=" in toks[0] and not toks[0].startswith("-"):
            toks = toks[1:]
        if not toks:
            return None
        bin_name = os.path.basename(toks[0])

    if bin_name in _READ_BIN_NAMES:
        # Find the first non-flag token after the binary
        rest = toks[1:]
        # Special: head/tail/nl take `-n N` or `-N`. Skip those.
        skip_next = False
        for tok in rest:
            if skip_next:
                skip_next = False
                continue
            if tok.startswith("-"):
                # Two-arg flags: -n, -c, -A, -B (head/tail/grep style)
                if tok in ("-n", "-c", "-A", "-B", "-C"):
                    skip_next = True
                continue
            return tok, f"bash:{bin_name}"
        return None
    if bin_name == "sed":
        # Look for the file path AFTER the script. sed -n '1,80p' file
        rest = toks[1:]
        # Skip flags + script
        i = 0
        while i < len(rest):
            t = rest[i]
            if t.startswith("-"):
                # -n, -e, -f take no second arg here for our purposes
                i += 1
                continue
            # First non-flag = script. Next non-flag = file.
            i += 1
            break
        # Now look for file
        while i < len(rest):
            t = rest[i]
            if not t.startswith("-"):
                return t, "bash:sed"
            i += 1
        return None
    if bin_name == "awk":
        # awk '<script>' file ...
        rest = toks[1:]
        i = 0
        while i < len(rest) and rest[i].startswith("-"):
            # -F, -v take next token
            if rest[i] in ("-F", "-v", "-f"):
                i += 2
            else:
                i += 1
        # Skip script
        if i < len(rest):
            i += 1
        if i < len(rest):
            return rest[i], "bash:awk"
        return None
    return None
