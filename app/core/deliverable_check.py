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
    """
    if not abs_path.exists():
        return False, [f"file does not exist: {abs_path}"]
    if not abs_path.is_file():
        return False, [f"path is not a file: {abs_path}"]
    text = _file_text(abs_path)
    reasons: list[str] = []
    n_lines = _file_lines(abs_path)
    if min_lines and n_lines < min_lines:
        reasons.append(f"too few lines ({n_lines} < {min_lines})")
    if max_lines and n_lines > max_lines:
        reasons.append(f"too many lines ({n_lines} > {max_lines})")
    for needle in must_contain or []:
        if not _check_substring(text, needle):
            preview = needle if len(needle) <= 60 else needle[:60] + "…"
            reasons.append(f"missing required content: {preview!r}")
    return (not reasons), reasons


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
