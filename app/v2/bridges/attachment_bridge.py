"""
V2 attachment bridge.

Multimodal tasks receive images / audio files at submission time. We
persist them under the agent's ``working_directory/attachments/<task_id>/``
so a later TaskExecutor can reference them by path (which the LLM
provider can either resolve locally or convert to base64 as needed).

The on-disk layout is:

    <working_directory>/attachments/<task_id>/<nnn>-<sanitised_name>.<ext>

A serve endpoint in ``app.api.routers.v2`` returns these with the right
``Content-Type`` so the V1 attachment viewer (or our own frontend) can
render them inline.

This module is intentionally thin: the REST layer parses the upload,
this module writes the bytes and returns a dict suitable for
``Task.context.attachments``.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import re
import time
from pathlib import Path


logger = logging.getLogger("tudouclaw.v2.attachment_bridge")


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".opus"}
_VIDEO_EXTS = {".mp4", ".mov", ".webm", ".mkv"}


def _infer_kind(mime: str, filename: str) -> str:
    mime = (mime or "").lower()
    if mime.startswith("image/"):
        return "image"
    if mime.startswith("audio/"):
        return "audio"
    if mime.startswith("video/"):
        return "video"
    ext = Path(filename or "").suffix.lower()
    if ext in _IMAGE_EXTS: return "image"
    if ext in _AUDIO_EXTS: return "audio"
    if ext in _VIDEO_EXTS: return "video"
    return "file"


_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _sanitise(name: str) -> str:
    """Strip path separators and other scary chars; preserve extension."""
    stem = Path(name or "upload").name  # drop any directory traversal
    safe = _SAFE_NAME_RE.sub("_", stem)[:80] or "upload"
    return safe


def save_attachment(
    *,
    agent_working_dir: str,
    task_id: str,
    filename: str,
    content: bytes,
    mime: str = "",
) -> dict:
    """Write an uploaded attachment and return a descriptor dict.

    Descriptor shape (matches ``Task.context.attachments`` entries)::

        {"kind":   "image" | "audio" | "video" | "file",
         "handle": "<absolute path on disk>",
         "mime":   "image/png",
         "size":   12345,
         "name":   "<sanitised filename>"}

    Raises:
        ValueError: on empty content or disallowed agent_working_dir.
    """
    if not content:
        raise ValueError("attachment content is empty")
    if not agent_working_dir or not os.path.isdir(agent_working_dir):
        raise ValueError(
            f"agent working directory not found: {agent_working_dir!r}")

    safe_name = _sanitise(filename or "upload")
    ts_seq = int(time.time() * 1000)
    final_name = f"{ts_seq:013d}-{safe_name}"

    dest_dir = os.path.join(agent_working_dir, "attachments", task_id)
    os.makedirs(dest_dir, exist_ok=True)
    dest_path = os.path.join(dest_dir, final_name)

    # Refuse to overwrite — two identical timestamps are vanishingly
    # unlikely, but play it safe.
    if os.path.exists(dest_path):
        raise FileExistsError(dest_path)

    with open(dest_path, "wb") as f:
        f.write(content)

    if not mime:
        mime, _ = mimetypes.guess_type(final_name)
        mime = mime or "application/octet-stream"

    return {
        "kind": _infer_kind(mime, final_name),
        "handle": dest_path,
        "mime": mime,
        "size": len(content),
        "name": safe_name,
    }


def resolve_path_for_serve(
    *,
    agent_working_dir: str,
    handle: str,
) -> str:
    """Validate that ``handle`` lives under the agent's attachments/
    subtree and return the canonical path; raise ``ValueError`` otherwise.

    Prevents path-traversal attacks in the serve endpoint.
    """
    if not agent_working_dir:
        raise ValueError("missing agent working directory")
    base = os.path.normpath(os.path.join(agent_working_dir, "attachments"))
    target = os.path.normpath(handle or "")
    if not target.startswith(base + os.sep) and target != base:
        raise ValueError(
            f"attachment {handle!r} is outside allowed attachment root"
        )
    if not os.path.isfile(target):
        raise FileNotFoundError(target)
    return target


__all__ = ["save_attachment", "resolve_path_for_serve"]
