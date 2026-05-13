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


def _rule_engine_check_file_write(resolved_path: str, content: str,
                                   kwargs: dict) -> str:
    """PEP for ``before_file_write``. Returns deny message if any rule
    refused the write, else empty string. Caller short-circuits on
    non-empty return.

    Context fields exposed to rules:
      - args.path           — resolved absolute path
      - args.basename       — just the file name
      - args.size_bytes     — len(content)
      - agent.id/name/role  — caller (from kwargs._caller_*)
      - scope.kind          — project / meeting / solo / global
      - scope.project_id?   — when in project chat
      - scope.workspace?    — agent's effective workspace root
    """
    try:
        from ..rule_engine import get_engine
    except Exception:
        return ""
    eng = get_engine()
    if eng is None:
        return ""
    caller_id = ""
    caller_name = ""
    caller_role = ""
    project_id = ""
    meeting_id = ""
    workspace = ""
    if isinstance(kwargs, dict):
        caller_id = str(kwargs.get("_caller_agent_id") or "")
        caller_name = str(kwargs.get("_caller_agent_name") or "")
        caller_role = str(kwargs.get("_caller_agent_role") or "")
        project_id = str(kwargs.get("_project_id") or "")
        meeting_id = str(kwargs.get("_meeting_id") or "")
        workspace = str(kwargs.get("_workspace") or "")
    if project_id:
        scope = {"kind": "project", "project_id": project_id, "workspace": workspace}
    elif meeting_id:
        scope = {"kind": "meeting", "meeting_id": meeting_id, "workspace": workspace}
    elif caller_id:
        scope = {"kind": "solo", "agent_id": caller_id, "workspace": workspace}
    else:
        scope = {"kind": "global"}
    ctx = {
        "args": {
            "path": resolved_path,
            "basename": os.path.basename(resolved_path),
            "size_bytes": len(content or ""),
        },
        "agent": {"id": caller_id, "name": caller_name, "role": caller_role},
        "scope": scope,
    }
    try:
        decisions = eng.evaluate("before_file_write", ctx)
    except Exception:
        return ""
    for d in decisions:
        if d.matched and d.action == "deny":
            return f"Error: write_file denied by rule '{d.rule_name}': {d.message}"
    return ""


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

# 2026-05-12 — P1 of "Claude-Code parity": same idea for glob_files.
# Per-turn cache keyed by (caller_agent_id, abs_base_path, pattern).
# Real symptom from today's log: 刘老师 called glob_files 13 times
# in one turn while assembling its "explore" mode. With this cache,
# repeats return prior results + a "[CACHED-GLOB]" marker so the LLM
# stops re-globbing.
_GLOB_FILES_CACHE_ATTR = "_glob_files_turn_cache"

# Path-level read counter — independent of (offset, limit). Tracks how
# many times the SAME PATH has been read this turn, regardless of which
# slice. Tripped by agents that loop "read 50 lines → write fail → read
# 1343 lines → write fail → ..." After ``_READ_PATH_HARD_CAP`` reads of
# the same path, returns a stop-message instead of the body.
_READ_PATH_COUNT_ATTR = "_read_file_path_counts"

# Soft warning at this count, hard refusal at the next.
_READ_PATH_SOFT_CAP = 3   # 3rd read → soft nudge appended
_READ_PATH_HARD_CAP = 5   # 5th+ read → REFUSE, return stop-message only

# Override via env so ops can dial it up/down without code changes.
def _path_caps() -> tuple[int, int]:
    try:
        soft = int(os.environ.get("TUDOU_READFILE_SOFT_CAP", str(_READ_PATH_SOFT_CAP)))
        hard = int(os.environ.get("TUDOU_READFILE_HARD_CAP", str(_READ_PATH_HARD_CAP)))
        return max(1, soft), max(soft + 1, hard)
    except Exception:
        return _READ_PATH_SOFT_CAP, _READ_PATH_HARD_CAP


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

    # ── Path-level valve(忽略 offset/limit,看同 path 总次数)──
    # Catches the "read 50 → write fail → read 1343 → write fail" loop
    # that the (path,offset,limit) cache misses. Soft nudge at SOFT_CAP,
    # hard refusal at HARD_CAP+1 so the agent MUST switch tactic.
    path_str = str(p)
    soft_cap, hard_cap = _path_caps()
    # Day 3 AM (2026-05-05): also bump cross-tool counter (bash cat /
    # head / tail share this counter — see _read_counter.py).
    try:
        from . import _read_counter as _xc
        _xt_n = _xc.bump_read(agent, path_str, source="read_file") if agent else 0
        if agent and _xc.is_blocked(agent, path_str):
            return _xc.blocked_message(path_str, _xt_n, "read_file")
    except Exception:
        pass
    if agent is not None:
        pcount = getattr(agent, _READ_PATH_COUNT_ATTR, None)
        if pcount is None:
            pcount = {}
            try:
                setattr(agent, _READ_PATH_COUNT_ATTR, pcount)
            except Exception:
                pcount = None
        if pcount is not None:
            n = pcount.get(path_str, 0) + 1
            pcount[path_str] = n
            if n > hard_cap:
                # Hard refusal — return ONLY the stop message, no body.
                return (
                    f"[READ-VALVE-TRIPPED #{n}] You have read {path_str!r} "
                    f"{n} times this turn (cap={hard_cap}). The file is "
                    f"unchanged. Refusing further reads to break the loop.\n\n"
                    f"WHAT TO DO INSTEAD:\n"
                    f"  • Use the content you already have to answer.\n"
                    f"  • If write_file is failing, the issue is your tool "
                    f"call args (not the file). Inspect the LAST error.\n"
                    f"  • If you genuinely need to re-read, finish this turn "
                    f"first and re-read in a new turn (cache resets).\n"
                )

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
            # If path-level reads have crossed soft_cap, escalate the
            # message — same content but a stronger nudge.
            _path_n = (pcount or {}).get(path_str, 0)
            warn_prefix = ""
            if _path_n >= soft_cap:
                warn_prefix = (
                    f"⚠️ [READ-VALVE-WARN #{_path_n}] You've now read "
                    f"{path_str!r} {_path_n} times this turn (cap={hard_cap}). "
                    f"One more = REFUSED. If write/edit fails, **the issue is "
                    f"your tool args, not the file**. Stop reading and check "
                    f"the LAST tool error.\n\n"
                )
                # Also push to ephemeral reminder queue — anchors at
                # the LAST user message of the next LLM call so the
                # warning is visible at the prompt edge, not buried in
                # tool_result history. dedupe makes repeat reads in
                # the same turn collapse to one queued message.
                if hasattr(agent, "queue_reminder"):
                    try:
                        agent.queue_reminder(
                            f"You've read {path_str!r} {_path_n} times this turn "
                            f"(cap={hard_cap}). One more read of this exact path "
                            f"will be refused. Use what you already have, or "
                            f"finalize/fail the step instead of re-reading."
                        )
                    except Exception:
                        pass
            return (
                warn_prefix
                + f"[REPEAT-READ #{hit_count + 1}] You already read this file "
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

    # Soft nudge: if this path's read count crossed soft_cap, prepend
    # a warning to the body so the agent sees it BEFORE deciding to
    # read again. (Hard cap returns immediately above without ever
    # reading the file.)
    if agent is not None:
        pcount = getattr(agent, _READ_PATH_COUNT_ATTR, None)
        if pcount is not None:
            n = pcount.get(path_str, 0)
            if n >= soft_cap:
                body = (
                    f"⚠️ [READ-VALVE-WARN #{n}] You've read {path_str!r} "
                    f"{n} times this turn (cap={hard_cap}). One more read "
                    f"of this path will be REFUSED. If write/edit is "
                    f"failing, the issue is your tool call — not the file.\n\n"
                    + body
                )
                # Mirror to the ephemeral reminder channel so the
                # nudge surfaces at the next user-message edge rather
                # than only buried inside this tool_result.
                if hasattr(agent, "queue_reminder"):
                    try:
                        agent.queue_reminder(
                            f"You've read {path_str!r} {n} times this turn "
                            f"(cap={hard_cap}). Stop re-reading; use what you "
                            f"have or finalize the step."
                        )
                    except Exception:
                        pass

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
        # for_write=True so paths under sandbox.readonly_dirs are
        # rejected (e.g. agent can read sibling skills' manifests as
        # reference but can't overwrite them).
        p = pol.safe_path(path, for_write=True)
    except _sandbox.SandboxViolation as e:
        return f"Error: {e}"

    # ── PEP: before_file_write ──
    # Rule Engine policy hook for file writes (PM-authored path/naming
    # rules land here). Failures isolated — no engine = no policy =
    # behavior unchanged. Caller passes _caller_agent_id /
    # _project_id via kwargs (set by the dispatcher in tools_split/_common).
    deny_msg = _rule_engine_check_file_write(str(p), content, _ if isinstance(_, dict) else {})
    if deny_msg:
        return deny_msg

    # QA gate (HANDOFF [C]) — block obviously-broken writes (binary
    # extension via text mode, empty/placeholder markdown, drawio with
    # no shapes). Surfaces as a tool error so the agent retries instead
    # of silently producing garbage on disk.
    from .. import qa_gate as _qa
    gate = _qa.validate_file_write(path, content)
    if not gate.ok:
        return f"Error: QA gate blocked write to {path}: {gate.reason}"

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        # Return a Claude-Code-style result: header + numbered preview
        # of the just-written content. Without this, the LLM sees only
        # "Successfully wrote N bytes" and frequently follows up with a
        # read_file to "verify" — wasting a turn. With the snippet,
        # it sees exactly what's now on disk and can proceed.
        return _format_write_result(str(p), content, len(content))
    except Exception as e:
        return f"Error writing file: {e}"


def _format_write_result(path_str: str, content: str, byte_count: int,
                          *, max_lines: int = 40) -> str:
    """Format the write_file response with a numbered snippet of the
    written content (Claude Code style).

    The snippet caps at ``max_lines`` total — if the file is longer,
    we emit head + "...  (N more lines truncated)" tail so the LLM
    sees both the start and end. If the file fits within max_lines,
    the entire content goes back numbered.
    """
    lines = (content or "").splitlines()
    total = len(lines)
    if total == 0:
        # Empty content (rare but possible — e.g. truncating a file).
        return (f"The file {path_str} has been written ({byte_count} bytes, "
                f"empty).")
    # Build numbered preview
    if total <= max_lines:
        body_lines = list(enumerate(lines, start=1))
        truncated_note = ""
    else:
        head_n = max_lines * 2 // 3   # 2/3 head, 1/3 tail
        tail_n = max_lines - head_n
        head = list(enumerate(lines[:head_n], start=1))
        tail = list(enumerate(
            lines[-tail_n:],
            start=total - tail_n + 1,
        ))
        body_lines = head + [(0, "...")] + tail  # 0 = sentinel for skip line
        truncated_note = (
            f"\n  ({total - max_lines} more line(s) elided between "
            f"head and tail)"
        )

    preview_parts: list[str] = []
    for n, line in body_lines:
        if n == 0:
            preview_parts.append("       ...")
        else:
            preview_parts.append(f"  {n:5d}\t{line}")
    preview = "\n".join(preview_parts)

    return (
        f"The file {path_str} has been written successfully "
        f"({byte_count} bytes, {total} line(s)). "
        f"Here's the result of running `cat -n` on the file:\n"
        f"{preview}{truncated_note}"
    )


# ── edit_file ────────────────────────────────────────────────────────

def _tool_edit_file(path: str, old_string: str, new_string: str,
                    **_: Any) -> str:
    pol = _sandbox.get_current_policy()
    try:
        p = pol.safe_path(path, for_write=True)
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
        # 2026-05-13: actionable "not found" error.
        # Real symptom: agent wrote `kms_key_rotation_enabled` /
        # `rotation_days` from memory but file actually has
        # `enable_key_rotation` / `interval`. Old error just said
        # "not found" — agent retried with same wrong text 4 times
        # then gave up. Now we surface the file head + best-effort
        # nearest-match hint so the agent can self-correct on the
        # next attempt instead of looping or guessing.
        try:
            from difflib import get_close_matches
            # Search the first non-empty line of old_string against
            # all lines in the file — usually a unique signature word.
            target_line = next(
                (ln for ln in old_string.splitlines() if ln.strip()), "")
            file_lines = text.splitlines()
            hint = ""
            if target_line:
                # Match by stripped content so whitespace differences
                # don't hide near-misses
                stripped_lines = [ln.strip() for ln in file_lines]
                target_stripped = target_line.strip()
                close = get_close_matches(
                    target_stripped, stripped_lines, n=3, cutoff=0.55)
                if close:
                    hint_lines = []
                    for cl in close:
                        # Find the original (non-stripped) line + line number
                        for i, sl in enumerate(stripped_lines, start=1):
                            if sl == cl:
                                hint_lines.append(
                                    f"  line {i}: {file_lines[i-1]!r}")
                                break
                    hint = (
                        "\n\nClosest matches in the file (line numbers + "
                        "actual indentation/spelling):\n" +
                        "\n".join(hint_lines))
            preview_n = min(40, len(file_lines))
            preview = "\n".join(
                f"{i:>4}  {ln}" for i, ln in enumerate(
                    file_lines[:preview_n], start=1))
            return (
                f"Error: old_string not found in {path}.\n"
                f"Likely cause: the text you provided doesn't match the "
                f"file byte-for-byte (whitespace, indentation, field "
                f"names changed since you last read it)."
                f"{hint}\n\n"
                f"First {preview_n} lines of the file (use these for "
                f"the next edit_file call, NOT your memory):\n"
                f"{preview}\n\n"
                f"DO NOT retry with the same old_string. Either copy "
                f"from the preview above, or call read_file first to "
                f"refresh your view, then retry."
            )
        except Exception:
            return f"Error: old_string not found in {path}"
    if count > 1:
        return (f"Error: old_string found {count} times in {path}. "
                "Must be unique. Provide more context.")

    new_text = text.replace(old_string, new_string, 1)
    p.write_text(new_text, encoding="utf-8")
    # Claude-Code-style result: show the numbered SNIPPET around the
    # edited region so the LLM can verify the change without doing a
    # follow-up read_file. Without this, agents wander into a "edit →
    # read → edit → read" verification loop, wasting tokens. Showing
    # 6 lines before + 6 after the edit gives enough context to
    # confirm intent without dumping the whole file.
    return _format_edit_result(str(p), new_text, new_string, context_lines=6)


def _format_edit_result(path_str: str, full_text: str, new_string: str,
                         *, context_lines: int = 6) -> str:
    """Render an edit_file result with a numbered preview of the edited
    region. Locates the new_string in the post-edit file, finds its
    line number, and emits ``context_lines`` lines on each side
    (clamped to file boundaries) prefixed with line numbers.

    If the new_string spans multiple lines, the preview covers the
    full span plus the surrounding context.
    """
    new_lines = full_text.splitlines()
    total = len(new_lines)

    # Find which lines contain (any part of) the new_string. We search
    # by iterating — splitting on raw newlines is the simplest robust
    # approach across embedded \n in old/new_string.
    new_first_line: int | None = None
    new_last_line: int | None = None
    if new_string:
        # Linear scan for the first occurrence of new_string in
        # full_text, then map to line numbers. Cheap because edit
        # files usually < 5KB.
        idx = full_text.find(new_string)
        if idx >= 0:
            # Number of newlines before idx → start line (1-based)
            prefix = full_text[:idx]
            new_first_line = prefix.count("\n") + 1
            inner_newlines = new_string.count("\n")
            new_last_line = new_first_line + inner_newlines
    if new_first_line is None or new_last_line is None:
        # Couldn't locate (e.g. new_string was empty / pure whitespace).
        # Fall back to a brief acknowledgment without snippet.
        return (
            f"The file {path_str} has been edited successfully. "
            f"(replacement region not previewable — "
            f"new_string was empty or whitespace-only)"
        )

    # Compute snippet range
    start = max(1, new_first_line - context_lines)
    end = min(total, new_last_line + context_lines)
    preview_parts: list[str] = []
    for i in range(start, end + 1):
        line = new_lines[i - 1] if (i - 1) < total else ""
        marker = " "  # space → context line
        if new_first_line <= i <= new_last_line:
            marker = "+"  # → indicates this line is part of the edit
        preview_parts.append(f"{marker} {i:5d}\t{line}")
    preview = "\n".join(preview_parts)

    return (
        f"The file {path_str} has been edited successfully "
        f"(1 occurrence replaced). Here's the result of running `cat -n` "
        f"on a snippet of the edited file (lines {start}-{end} of {total}; "
        f"`+` marks lines inside the new content):\n"
        f"{preview}"
    )


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

def _tool_glob_files(pattern: str, path: str = ".", **ctx: Any) -> str:
    pol = _sandbox.get_current_policy()
    try:
        base = pol.safe_path(path)
    except _sandbox.SandboxViolation as e:
        return f"Error: {e}"
    if not base.exists():
        return f"Error: Path not found: {path}"

    # ── Per-turn dedup (P1, 2026-05-12) ──────────────────────────
    # Today's log: agent called glob_files 13 times in one turn while
    # exploring. Same (base_path, pattern) → return prior result with
    # a [CACHED-GLOB] marker. LLM sees results immediately + a nudge
    # to stop re-globbing.
    caller_id = (ctx.get("_caller_agent_id", "")
                 if isinstance(ctx, dict) else "")
    agent = _get_caller_agent(caller_id) if caller_id else None
    cache_key = (str(base.resolve()), str(pattern))
    if agent is not None:
        cache = getattr(agent, _GLOB_FILES_CACHE_ATTR, None)
        if cache is None:
            cache = {}
            try:
                setattr(agent, _GLOB_FILES_CACHE_ATTR, cache)
            except Exception:
                cache = None
        if cache is not None and cache_key in cache:
            cached_body, hit_count = cache[cache_key]
            cache[cache_key] = (cached_body, hit_count + 1)
            return (
                f"[CACHED-GLOB #{hit_count + 1}] You already ran this exact "
                f"glob (pattern={pattern!r}, path={path!r}) earlier this "
                f"turn. Filesystem hasn't changed in the meantime — stop "
                f"re-globbing the same pattern. Use the result you have, "
                f"or pick a more specific pattern.\n\n"
                + cached_body
            )

    found = sorted(base.glob(pattern))
    # Filter out anything under a hidden directory.
    filtered = [
        str(f) for f in found
        if not any(part.startswith(".") and part not in (".", "..")
                   for part in f.parts)
    ]
    if not filtered:
        body = "No files found."
    elif len(filtered) > _GLOB_MAX_RESULTS:
        body = ("\n".join(filtered[:_GLOB_MAX_RESULTS])
                + f"\n... ({len(filtered)} total, "
                f"showing first {_GLOB_MAX_RESULTS})")
    else:
        body = "\n".join(filtered)

    # Cache for the rest of this turn.
    if agent is not None:
        cache = getattr(agent, _GLOB_FILES_CACHE_ATTR, None)
        if isinstance(cache, dict):
            cache[cache_key] = (body, 1)

    return body
