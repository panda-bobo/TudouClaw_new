"""Deliverable contract verification — Day 1 AM (2026-05-05).

Each ProjectTask can carry a deliverable contract:
  - output_files:               required output paths (relative to workspace)
  - must_contain (flat list):   substrings that must appear in EVERY output_file
  - must_contain_per_file:      {path: [substrings]} — overrides flat list per file
  - min_lines / max_lines:      line-count bounds per file
  - acceptance_cmd:             optional shell verifier (exit-code based)

The agent's post-tool hook calls ``verify_task_deliverables`` after every
``write_file`` to update ``task.deliverable_status``. The system_prompt
rule then refuses ``task_complete`` until every output file has
``deliverable_status[path]['verified'] == True``.

This module is import-light (stdlib only) so it can be called from
agent.py's hot post-tool path without dragging in heavy deps.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping


# ── Acceptance command whitelist (Phase 2 P2-4, 2026-05-06) ──────────
# acceptance_cmd is shell=True, so an unrestricted command is a hole
# (a malicious / hallucinated PM agent could write `rm -rf` and have
# the framework execute it). We allow only well-known test/build/lint
# binaries as the FIRST token. Pipes, redirects, `&&` etc. are rejected.

_ACCEPTANCE_ALLOWED_BINS = frozenset({
    # Test runners
    "pytest", "python", "python3", "uv",
    "npm", "npx", "yarn", "pnpm",
    "cargo", "go", "make",
    "mvn", "gradle", "sbt",
    "rspec", "rake",
    "phpunit",
    # Static analyzers / linters (read-only)
    "ruff", "flake8", "pylint", "mypy", "pyright",
    "eslint", "tsc", "prettier",
    "clippy", "rustfmt",
    # File checks (safe)
    "test", "true", "false", "head", "tail", "wc", "grep",
    "stat", "ls", "find",
})

_ACCEPTANCE_FORBIDDEN_TOKENS = frozenset({
    "&&", "||", ";", "|", ">", "<", ">>", "<<",
    "$(", "`", "&",
})


def _acceptance_cmd_allowed(cmd: str) -> bool:
    """Return True if the acceptance_cmd is safe to execute via
    subprocess(shell=True).

    Rules:
      * First whitespace token (the binary) must be in the whitelist.
      * No shell metacharacters that could chain other commands.
      * No leading absolute path (`/bin/rm` slips past the bin check).
      * No `env VAR=val cmd` chaining (we'd have to recursively check).
    """
    if not cmd or not isinstance(cmd, str):
        return False
    s = cmd.strip()
    if not s:
        return False
    # Disallow obvious shell chaining
    for tok in _ACCEPTANCE_FORBIDDEN_TOKENS:
        if tok in s:
            return False
    head_tok = s.split(None, 1)[0]
    if head_tok.startswith("-"):
        return False
    if "/" in head_tok or "\\" in head_tok:
        return False
    if "=" in head_tok:
        return False
    bin_name = os.path.basename(head_tok)
    if bin_name == "env":
        return False  # env-prefix means a different cmd; reject for simplicity
    return bin_name in _ACCEPTANCE_ALLOWED_BINS


def _file_lines(p: Path) -> int:
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _file_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _check_substring(text: str, needle: str) -> bool:
    """Plain substring; if ``needle`` starts with ``re:`` treat as regex."""
    if needle.startswith("re:"):
        try:
            return re.search(needle[3:], text) is not None
        except re.error:
            return False
    return needle in text


def verify_one_file(abs_path: Path, must_contain: list[str],
                    min_lines: int, max_lines: int) -> tuple[bool, list[str]]:
    """Verify a single output file against the contract.

    Returns ``(verified, reasons)`` where ``reasons`` is a non-empty list
    of human-readable failure causes when verified is False.

    Each reason is augmented with an actionable "💡 fix" hint when we
    can derive one — e.g. for a missing markdown section we list the
    file's existing headers and suggest a concrete edit_file invocation
    with old_string/new_string anchors. Without this, the agent
    typically loops 5-10 times trying to figure out where to insert.
    """
    if not abs_path.exists():
        return False, [f"file does not exist: {abs_path}"]
    if not abs_path.is_file():
        return False, [f"path is not a file: {abs_path}"]
    text = _file_text(abs_path)
    reasons: list[str] = []
    n_lines = _file_lines(abs_path)
    if min_lines and n_lines < min_lines:
        reasons.append(
            f"too few lines ({n_lines} < {min_lines}). "
            f"💡 Fix: extend each section with concrete bullets / "
            f"paragraphs until you reach {min_lines}+ lines. "
            f"Don't pad with whitespace — auto-detected as gibberish."
        )
    if max_lines and n_lines > max_lines:
        reasons.append(
            f"too many lines ({n_lines} > {max_lines}). "
            f"💡 Fix: trim with edit_file — collapse verbose sections "
            f"to bullet lists, remove duplicate paragraphs."
        )
    for needle in must_contain or []:
        if not _check_substring(text, needle):
            reasons.append(_build_missing_content_reason(text, needle, abs_path))
    return (not reasons), reasons


def _build_missing_content_reason(file_text: str, needle: str,
                                    abs_path: Path) -> str:
    """For a missing required substring, render an actionable error
    that tells the agent EXACTLY how to fix it. Special-cased for
    markdown-section missing (the most common case).
    """
    preview = needle if len(needle) <= 60 else needle[:60] + "…"
    base = f"missing required content: {preview!r}"

    # Detect: does the missing needle look like a markdown heading?
    needle_stripped = needle.strip()
    is_md_header = bool(re.match(r"^#+\s", needle_stripped))

    if not is_md_header:
        # Generic literal-string missing
        return (
            f"{base}\n     💡 Fix: insert the exact text {preview!r} "
            f"somewhere in the file. If this is a marker meant to "
            f"appear at the top, prepend it; if it's a section "
            f"identifier, follow the same pattern other markers use."
        )

    # Markdown-header case — give specific edit_file instructions.
    existing_headers: list[tuple[int, str]] = []  # [(line_no_1based, header_text), ...]
    for i, line in enumerate(file_text.splitlines(), start=1):
        if re.match(r"^#+\s+\S", line):
            existing_headers.append((i, line.rstrip()))

    if not existing_headers:
        # Empty / no-header file — append the section.
        return (
            f"{base}\n"
            f"     💡 Fix: the file has no markdown headers yet. "
            f"Use write_file to overwrite with a structure that "
            f"includes the {needle_stripped!r} section, or use "
            f"edit_file with old_string=<the first line of current "
            f"content> and prepend the missing section before it."
        )

    # Show existing structure (cap at 8 to keep response concise).
    headers_preview = existing_headers[-8:] if len(existing_headers) > 8 else existing_headers
    headers_lines = "\n".join(
        f"        line {ln}: {hdr}" for ln, hdr in headers_preview
    )
    if len(existing_headers) > 8:
        headers_lines = f"        ... ({len(existing_headers) - 8} earlier headers omitted)\n" + headers_lines

    # Pick the LAST same-level (or any) header as the anchor — the new
    # section will be inserted BEFORE it (so it lands above the last
    # bookkeeping section like 总结 / 风险登记 / etc.).
    anchor_line, anchor_text = existing_headers[-1]
    new_section_template = (
        f"{needle_stripped}\n\n"
        f"<your content here — at least 3 lines of substantive bullets / paragraphs>\n\n"
    )
    edit_example = (
        f"        edit_file(\n"
        f"          path={str(abs_path)!r},\n"
        f"          old_string={anchor_text!r},\n"
        f"          new_string={(new_section_template + anchor_text)!r}\n"
        f"        )"
    )
    return (
        f"{base}\n"
        f"     💡 Fix steps:\n"
        f"     1. The file currently has these section headers:\n"
        f"{headers_lines}\n"
        f"     2. Insert the {needle_stripped!r} section BEFORE the last "
        f"existing header (line {anchor_line}: {anchor_text!r}).\n"
        f"     3. Use ONE edit_file call:\n"
        f"{edit_example}\n"
        f"     4. The deliverable check will re-run automatically — "
        f"if it still fails, the new error will tell you what's missing "
        f"in the section body."
    )


def verify_task_deliverables(
    task: Any, workspace_dir: str,
    only_path: str = "",
) -> dict[str, dict[str, Any]]:
    """Verify all (or one) of the task's output_files. Mutates
    ``task.deliverable_status`` in place AND returns the new status dict.

    ``only_path`` (relative or absolute) restricts checking to a single
    file — useful when the post-tool hook fires after one write_file.
    """
    output_files = list(getattr(task, "output_files", []) or [])
    if not output_files:
        return dict(getattr(task, "deliverable_status", {}) or {})

    flat_must = list(getattr(task, "must_contain", []) or [])
    per_file_must = dict(getattr(task, "must_contain_per_file", {}) or {})
    min_lines = int(getattr(task, "min_lines", 0) or 0)
    max_lines = int(getattr(task, "max_lines", 0) or 0)

    status = dict(getattr(task, "deliverable_status", {}) or {})
    ws = Path(workspace_dir).expanduser().resolve() if workspace_dir else None

    for rel in output_files:
        if only_path:
            # Match either basename or rel/abs path
            if not (only_path == rel
                    or os.path.basename(only_path) == os.path.basename(rel)
                    or os.path.abspath(only_path).endswith(rel)):
                continue
        abs_p = (ws / rel) if (ws and not os.path.isabs(rel)) else Path(rel)
        # Per-file must_contain overrides flat list. If neither is given,
        # we still check existence + min_lines.
        needs = per_file_must.get(rel, flat_must)
        verified, reasons = verify_one_file(abs_p, needs, min_lines, max_lines)
        status[rel] = {
            "verified": verified,
            "reasons": reasons,
            "checked_at": time.time(),
        }

    # Optional shell-level acceptance command. Only run when ALL files
    # pass their own checks AND the task declares an acceptance_cmd.
    # Phase 2 P2-4 (2026-05-06): whitelist + sanitize before running.
    acc_cmd = str(getattr(task, "acceptance_cmd", "") or "")
    if acc_cmd and all(s.get("verified") for s in status.values()):
        if not _acceptance_cmd_allowed(acc_cmd):
            for rel in output_files:
                s = status.setdefault(rel, {"verified": False, "reasons": [], "checked_at": time.time()})
                s["verified"] = False
                s["reasons"] = list(s.get("reasons", [])) + [
                    f"acceptance_cmd rejected by whitelist: {acc_cmd[:80]!r}. "
                    f"Allowed binaries: {sorted(_ACCEPTANCE_ALLOWED_BINS)}."
                ]
            try:
                task.deliverable_status = status
                task.updated_at = time.time()
            except Exception:
                pass
            return status
        expect = int(getattr(task, "acceptance_expect_exit", 0) or 0)
        try:
            cwd = workspace_dir if workspace_dir and os.path.isdir(workspace_dir) else None
            res = subprocess.run(
                acc_cmd, shell=True, cwd=cwd,
                capture_output=True, timeout=60, text=True,
            )
            if res.returncode != expect:
                # Mark every file as failed-by-acceptance so task can't
                # complete. Ugly but explicit.
                tail = (res.stdout + res.stderr)[-400:]
                for rel in output_files:
                    s = status.setdefault(rel, {"verified": False, "reasons": [], "checked_at": time.time()})
                    s["verified"] = False
                    s["reasons"] = list(s.get("reasons", [])) + [
                        f"acceptance_cmd exit={res.returncode} (expected {expect}); tail: {tail}"
                    ]
        except subprocess.TimeoutExpired:
            for rel in output_files:
                s = status.setdefault(rel, {"verified": False, "reasons": [], "checked_at": time.time()})
                s["verified"] = False
                s["reasons"] = list(s.get("reasons", [])) + ["acceptance_cmd timed out (>60s)"]
        except Exception as e:
            for rel in output_files:
                s = status.setdefault(rel, {"verified": False, "reasons": [], "checked_at": time.time()})
                s["verified"] = False
                s["reasons"] = list(s.get("reasons", [])) + [f"acceptance_cmd error: {e}"]

    try:
        task.deliverable_status = status
        task.updated_at = time.time()
    except Exception:
        pass
    return status


def all_deliverables_verified(task: Any) -> tuple[bool, list[str]]:
    """Returns (ok, missing_paths). When ok=False, list contains the
    output_files that have NOT passed verification (used by the
    task_complete gate)."""
    output_files = list(getattr(task, "output_files", []) or [])
    if not output_files:
        # No contract → free to complete (legacy tasks)
        return True, []
    status = dict(getattr(task, "deliverable_status", {}) or {})
    missing = []
    for rel in output_files:
        s = status.get(rel)
        if not s or not s.get("verified"):
            missing.append(rel)
    return (not missing), missing


def render_status_for_agent(task: Any) -> str:
    """Human-readable status block to inject into agent system messages
    after a deliverable check runs. Concise — agent should be able to
    glance and know what's left."""
    output_files = list(getattr(task, "output_files", []) or [])
    if not output_files:
        return ""
    status = dict(getattr(task, "deliverable_status", {}) or {})
    lines = ["[Deliverable Check]"]
    for rel in output_files:
        s = status.get(rel)
        if not s:
            lines.append(f"  ⏳ {rel}  (not yet written)")
        elif s.get("verified"):
            lines.append(f"  ✅ {rel}")
        else:
            reasons = "; ".join(s.get("reasons", []) or [])
            lines.append(f"  ❌ {rel}  — {reasons}")
    ok, missing = all_deliverables_verified(task)
    if ok:
        lines.append("All deliverables verified — you may call task_complete.")
    else:
        lines.append(f"task_complete blocked until these pass: {missing}")
    return "\n".join(lines)
