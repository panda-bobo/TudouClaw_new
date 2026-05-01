"""Platform-level QA gates (HANDOFF [C]).

Each gate returns ``GateResult(ok=True)`` to allow, or
``GateResult(ok=False, reason="...")`` to block. The caller is
responsible for surfacing the block as an error to the agent — silent
skips defeat the point.

Hook points wired in this round:
  * ``app/tools_split/fs.py:_tool_write_file`` — pre-write file validation
  * ``app/mcp/dispatcher.py:NodeMCPDispatcher.dispatch`` — pre-call
    validation for ``send_email``-class tools.

The third hook point sketched in HANDOFF [C] (intent detection in agent
"task done" messages) is not in this module — needs its own design pass
because it requires LLM-side intent classification.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass
class GateResult:
    ok: bool
    reason: str = ""


OK = GateResult(ok=True)


# ── Email validation (used by MCP dispatcher) ─────────────────────────

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")

# MCP tool names that are email sends. Registered here, not by sniffing
# the dispatcher's target — keeps the gate's surface explicit.
EMAIL_TOOL_NAMES = frozenset({"send_email", "sendEmail", "send-email"})


def validate_email_args(arguments: dict) -> GateResult:
    """Pre-flight validation of ``send_email``-class MCP tool arguments.

    Catches the recipient/subject/body/attachment failure modes that
    produced the 2026-04-30 wrong-recipient incident. The check is
    intentionally conservative — only blocks on certainties (empty,
    malformed, missing absolute attachment paths).
    """
    if not isinstance(arguments, dict):
        return GateResult(False, "arguments must be a dict")

    to = arguments.get("to")
    if isinstance(to, str):
        to = [to]
    if not to:
        return GateResult(False, "`to` is empty — at least one recipient required")
    for addr in to:
        a = (addr or "").strip()
        if not a or not _EMAIL_RE.match(a):
            return GateResult(False, f"invalid recipient address: {addr!r}")

    for label in ("cc", "bcc"):
        v = arguments.get(label)
        if v is None:
            continue
        if isinstance(v, str):
            v = [v]
        for addr in v:
            a = (addr or "").strip()
            if a and not _EMAIL_RE.match(a):
                return GateResult(False, f"invalid {label} address: {addr!r}")

    subject = arguments.get("subject", "")
    if not (subject or "").strip():
        return GateResult(False, "subject is empty")
    if len(subject) > 200:
        return GateResult(False, f"subject too long ({len(subject)} chars > 200)")

    body = arguments.get("body", "")
    if not (body or "").strip():
        return GateResult(False, "body is empty")

    attachments = arguments.get("attachments") or []
    if isinstance(attachments, str):
        attachments = [attachments]
    for a in attachments:
        path = a if isinstance(a, str) else (
            a.get("path") if isinstance(a, dict) else ""
        )
        if path and os.path.isabs(path) and not os.path.isfile(path):
            return GateResult(False, f"attachment file not found: {path}")

    return OK


# ── File-write validation (used by fs.py) ─────────────────────────────

# Binary formats that text-mode write_file would corrupt. Agents that
# try to write a .pptx through write_file are always making a mistake
# (the right path is python-pptx + .save()).
_BINARY_EXT = {
    ".pptx", ".docx", ".xlsx", ".pdf",
    ".png", ".jpg", ".jpeg", ".gif", ".webp",
    ".zip", ".tar", ".gz",
    ".mp3", ".mp4", ".mov", ".wav",
}

# Regex for placeholder/stub content that strongly suggests the agent
# punted. Intentionally narrow to avoid false positives on real files
# that mention "TODO" or "placeholder" in legitimate context.
_PLACEHOLDER_RE = re.compile(
    r"(Lorem ipsum|"
    r"\[insert[^\]]{0,40}\]|"
    r"placeholder text|"
    r"TBD:[ \t]*$|"
    r"XXX:[ \t]*$|"
    r"TODO:[ \t]*fill[ \t]*(in|me)?)",
    re.IGNORECASE | re.MULTILINE,
)


def validate_file_write(path: str, content: str) -> GateResult:
    """Pre-write validation. Block obviously-broken or stub writes."""
    suffix = Path(path).suffix.lower()

    if suffix in _BINARY_EXT:
        return GateResult(
            False,
            f"binary format {suffix} cannot be written via write_file "
            f"(text encoding will corrupt the file). Use the appropriate "
            f"library (python-pptx for .pptx, PIL for images, etc.) and "
            f"save with that library's writer."
        )

    if suffix == ".md":
        return _validate_md(content)
    if suffix == ".drawio":
        return _validate_drawio(content)

    return OK


def _validate_md(content: str) -> GateResult:
    if not content.strip():
        return GateResult(False, "markdown file is empty")
    m = _PLACEHOLDER_RE.search(content)
    if m:
        return GateResult(
            False,
            f"placeholder text detected ({m.group(0)!r}) — "
            f"replace with real content before saving"
        )
    return OK


def _validate_drawio(content: str) -> GateResult:
    if not content.strip():
        return GateResult(False, "drawio file is empty")
    geom_count = content.count('as="geometry"')
    if geom_count == 0:
        return GateResult(
            False,
            'no <mxGeometry as="geometry"> elements — '
            'drawio file would render as empty canvas'
        )
    return OK
