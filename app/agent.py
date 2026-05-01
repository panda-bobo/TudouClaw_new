"""
Agent — encapsulates an AI agent with its own conversation, tools, config & lifecycle.

Each Agent has a rich profile: personality, communication style, expertise,
skills, language, and configurable tool permissions. Supports policy-based
approval for dangerous tool executions.

Task system tracks per-agent work items (todo/in_progress/done/blocked).
MCP integration allows each agent to connect to external Model Context Protocol servers.

NOTE: This module has been partially refactored. Standalone types live in
``agent_types.py``, chat task management in ``chat_task.py``, and Agent
method groups in mixin modules (``agent_llm.py``, ``agent_execution.py``,
``agent_growth.py``).  All public names are re-exported from here for full
backward compatibility — existing ``from app.agent import X`` statements
continue to work unchanged.
"""
from __future__ import annotations
import concurrent.futures
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar, Optional

# ── Re-export extracted types for backward compatibility ──
from .agent_types import (                                        # noqa: F401
    _ensure_str_content,
    AgentStatus, AgentPhase,
    TaskStatus, TaskSource, AgentTask,
    StepStatus, ExecutionStep, ExecutionPlan,
    MCPServerConfig, AgentProfile, AgentEvent,
)
from .chat_task import (                                          # noqa: F401
    ChatTaskStatus, ChatTask, ChatTaskManager,
    get_chat_task_manager,
)
# ── Mixin imports (DEAD as of 2026-04-30, see tombstones) ──
# Agent class is a bare dataclass — does NOT inherit any mixin. All
# methods that the mixin modules WOULD provide are implemented directly
# on Agent. Three modules now wrap their dead-code body in a string
# literal as archaeology:
#   * app/agent_llm.py            (AgentLLMMixin)
#   * app/agent_growth.py         (enhancement helpers)
#   * app/agent_execution.py      (AgentExecutionMixin — module-level
#                                  helpers _stream_chat_to_response,
#                                  _text_similarity, _is_meta_promise
#                                  ARE still live and imported below)
# Old import lines kept here as comments for searchability:
#   from .agent_llm import AgentLLMMixin            # ← retired 2026-04-30
#   from .agent_execution import AgentExecutionMixin # ← retired 2026-04-30
from .agent_execution import _stream_chat_to_response             # noqa: F401
# from .agent_growth import AgentGrowthMixin                        # noqa: F401

def _ensure_str_content(content) -> str:
    """Normalize message content to string.

    OpenAI-compatible APIs may return content as a string, a list of content
    blocks (multimodal format), a dict, or None.  This helper guarantees a
    plain string so downstream code never hits 'list + str' TypeError.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # list of content blocks – extract text parts
        text_parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif "text" in block:
                    text_parts.append(block["text"])
                else:
                    text_parts.append(json.dumps(block, ensure_ascii=False))
            elif isinstance(block, str):
                text_parts.append(block)
            else:
                text_parts.append(str(block))
        return "\n".join(text_parts)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    return str(content)


_IMAGE_TYPES = frozenset({"image_url", "image", "input_image"})


def _strip_old_images(messages: list[dict]) -> list[dict]:
    """Replace base64 image data in all but the last user message.

    Keeps only the current turn's images; older images become a short
    text placeholder like ``[image from earlier turn]``. This prevents
    runaway token usage from accumulated base64 data URIs and avoids
    confusing the model with stale images.

    Returns a **new** list — never mutates the input.
    """
    # Find the index of the last user message that has multimodal content
    last_mm_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            if any(isinstance(p, dict) and p.get("type") in _IMAGE_TYPES
                   for p in m["content"]):
                last_mm_idx = i
                break

    if last_mm_idx < 0:
        return messages  # no multimodal messages at all

    result = []
    for i, m in enumerate(messages):
        if (i != last_mm_idx
                and m.get("role") == "user"
                and isinstance(m.get("content"), list)):
            # Check if this message has image parts
            has_img = any(isinstance(p, dict) and p.get("type") in _IMAGE_TYPES
                         for p in m["content"])
            if has_img:
                # Keep text parts, replace images with placeholder
                new_parts = []
                img_count = 0
                for p in m["content"]:
                    if isinstance(p, dict) and p.get("type") in _IMAGE_TYPES:
                        img_count += 1
                    else:
                        new_parts.append(p)
                new_parts.append({
                    "type": "text",
                    "text": f"[{img_count} image(s) from earlier turn — omitted]",
                })
                result.append({**m, "content": new_parts})
                continue
        result.append(m)
    return result


# Knobs for _compress_old_tool_results. Surface-level constants so
# anyone tuning prompt-size behavior doesn't have to read the function.
_KEEP_LAST_TOOL_RESULTS = 3        # most recent N tool results preserved in full
_OLD_TOOL_RESULT_HEAD_CHARS = 300  # older tool results trimmed to this many head chars

# NB: image-base64 elision lives in ``_strip_old_images`` (line ~84
# above). It WAS only wired into the iteration>0 compression chain
# (line ~7033); turn 0 leaked all historical image bytes through. The
# first-call path below (around line 6916) now calls it too.


def _compress_old_tool_results(messages: list[dict],
                                keep_last: int = _KEEP_LAST_TOOL_RESULTS,
                                max_body_chars: int = _OLD_TOOL_RESULT_HEAD_CHARS
                                ) -> list[dict]:
    """Truncate the body of tool-result messages older than ``keep_last``.

    Without this, every ``web_fetch`` result (up to 5k chars) stays in
    the message history and is re-sent on every tool-call round. A
    research session with 5-6 fetches + 10 iterations easily burns
    40k+ input tokens on STALE tool output.

    Recent results (``keep_last`` newest) are preserved in full so the
    model can still reason on current data. Older ones are replaced by
    their first ``max_body_chars`` characters + a clear truncation
    marker — enough for the model to remember what it already saw
    without re-paying for it.

    Returns a NEW list; never mutates the input.
    """
    # Pass 1: index all tool-result messages (role == "tool").
    tool_idx = [i for i, m in enumerate(messages)
                if m.get("role") == "tool"]
    if len(tool_idx) <= keep_last:
        return messages
    to_compress = set(tool_idx[:-keep_last])

    result: list[dict] = []
    for i, m in enumerate(messages):
        if i not in to_compress:
            result.append(m)
            continue
        content = m.get("content", "")
        if not isinstance(content, str):
            result.append(m)
            continue
        if len(content) <= max_body_chars:
            result.append(m)
            continue
        trimmed = (
            content[:max_body_chars]
            + f"\n\n... [truncated from {len(content)} chars — "
            "older tool result, see recent turns for full data]"
        )
        result.append({**m, "content": trimmed})
    return result


# ── Compress old write_file / edit_file tool_call args ──
# When the LLM calls write_file({path, content="<5000 chars of code>"})
# the FULL content stays on the assistant message forever. On turn 2+
# we re-send it. Once the write succeeded and we have the file on disk,
# the payload is pure waste — the LLM can always re-read it via read_file
# if needed. This trimmer replaces old content args with a one-line
# summary pointing at the path.
_KEEP_LAST_WRITE_CALLS = 2
_OLD_WRITE_CALL_HEAD_CHARS = 200
_WRITE_LIKE_TOOLS = frozenset({"write_file", "edit_file", "create_file"})


def _compress_old_write_tool_calls(messages: list[dict],
                                    keep_last: int = _KEEP_LAST_WRITE_CALLS,
                                    max_body_chars: int = _OLD_WRITE_CALL_HEAD_CHARS
                                    ) -> list[dict]:
    """Trim large `content` / `new_string` args on OLD write_file-like
    tool_calls sitting on assistant messages.

    Only touches messages OLDER than ``keep_last`` write-like calls back —
    the 2 most recent stay verbatim so the current iteration of the loop
    can still see what it just tried to write.

    Returns a NEW list; never mutates input. Large-arg JSON strings are
    replaced with a single-line summary that preserves path + a head
    preview so the model still remembers it happened.
    """
    import json as _json

    # Pass 1: index assistant messages that carry at least one write call.
    write_msg_idx: list[int] = []
    for i, m in enumerate(messages):
        if m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            fn = (tc.get("function") or {}).get("name") or ""
            if fn in _WRITE_LIKE_TOOLS:
                write_msg_idx.append(i)
                break
    if len(write_msg_idx) <= keep_last:
        return messages
    to_compress = set(write_msg_idx[:-keep_last])

    def _shrink_arg(val, field_name: str) -> str:
        """Return a short placeholder for a large string arg value."""
        s = val if isinstance(val, str) else _json.dumps(val, ensure_ascii=False, default=str)
        if len(s) <= max_body_chars:
            return s
        head = s[:max_body_chars].replace("\n", "⏎")
        return (f"[elided: {len(s)} chars of {field_name} — written to disk; "
                f"head preview: {head}…]")

    result: list[dict] = []
    for i, m in enumerate(messages):
        if i not in to_compress:
            result.append(m)
            continue
        calls = m.get("tool_calls") or []
        new_calls = []
        changed = False
        for tc in calls:
            fn = (tc.get("function") or {}).get("name") or ""
            if fn not in _WRITE_LIKE_TOOLS:
                new_calls.append(tc)
                continue
            fn_obj = dict(tc.get("function") or {})
            raw_args = fn_obj.get("arguments")
            if isinstance(raw_args, str):
                try:
                    parsed = _json.loads(raw_args)
                except Exception:
                    new_calls.append(tc); continue
            elif isinstance(raw_args, dict):
                parsed = dict(raw_args)
            else:
                new_calls.append(tc); continue
            modified = False
            for field in ("content", "new_string"):
                v = parsed.get(field)
                if isinstance(v, str) and len(v) > max_body_chars:
                    parsed[field] = _shrink_arg(v, field)
                    modified = True
            if modified:
                fn_obj["arguments"] = _json.dumps(parsed, ensure_ascii=False)
                new_calls.append({**tc, "function": fn_obj})
                changed = True
            else:
                new_calls.append(tc)
        if changed:
            result.append({**m, "tool_calls": new_calls})
        else:
            result.append(m)
    return result


# ── History summarization (Nov 2026) ─────────────────────────────────────
# 真正的"摘要+滑窗"裁剪。老的 compression 只是截头 600 字符,丢失上下文,
# 结果 LLM 再读一次 → 又生成新 tool_result → 死循环。 这里用 agent 自己的
# LLM 把老段落压成 300-500 字结构化摘要 (一次性成本) 换掉 5-15 KB 历史,
# 之后每轮省 ~10k input tokens。
#
# 触发条件(任一):
#   - messages 字符总数超过 threshold_chars (默认 30k,约 7.5k token)
#   - tool message 数量超过 8 个
#
# 切分规则:
#   [0..N-keep_last-1]  → 摘要替换 (但保留 role==system 原样)
#   [N-keep_last..N-1]  → 最近 keep_last 条原样保留
#
# tool_call/tool_result 配对保护:
#   切点如果落在一个 assistant(tool_calls) + tool(result) 配对的中间,
#   把整个配对都留到 "recent" 这一侧,避免 OpenAI-compat API 的 orphan tool 400。
#
# 摘要失败时直接返回原 messages (fail-safe, 不能阻塞 chat 循环)。
# 可通过 TUDOU_HISTORY_SUMMARY_OFF=1 关闭。

# Tuned Nov 2026 after observing 6k→9k→15k→17k/call linear growth —
# summary thresholds were too lax; trigger earlier to flatten the curve.
_HISTORY_SUMMARY_CHARS = int(
    os.environ.get("TUDOU_HISTORY_SUMMARY_CHARS", "25000"))
_HISTORY_SUMMARY_KEEP_LAST = int(
    os.environ.get("TUDOU_HISTORY_SUMMARY_KEEP_LAST", "6"))
_HISTORY_SUMMARY_MAX_TOOLS = int(
    os.environ.get("TUDOU_HISTORY_SUMMARY_MAX_TOOLS", "6"))
# Old slice 必须至少这么大才值得摘要 (LLM call 本身 ~2k tokens,
# old_slice < 这个值时摘要本身比省下的还贵)
_HISTORY_SUMMARY_MIN_OLD_CHARS = int(
    os.environ.get("TUDOU_HISTORY_SUMMARY_MIN_OLD_CHARS", "5000"))
# 上次摘要后,old_slice 至少多这么多消息或字符,才值得重新摘要。
# 不达标就复用缓存,新增消息自然挤进 recent 窗口处理。
_HISTORY_SUMMARY_RESUM_DELTA_MSGS = int(
    os.environ.get("TUDOU_HISTORY_SUMMARY_RESUM_DELTA_MSGS", "10"))
_HISTORY_SUMMARY_RESUM_DELTA_CHARS = int(
    os.environ.get("TUDOU_HISTORY_SUMMARY_RESUM_DELTA_CHARS", "8000"))
_HISTORY_SUMMARY_OFF = os.environ.get("TUDOU_HISTORY_SUMMARY_OFF", "0") == "1"


def _estimate_messages_chars(messages: list[dict]) -> int:
    """粗略估算 messages 的字符总数 (content + tool_calls)."""
    import json as _json
    total = 0
    for m in messages:
        c = m.get("content") or ""
        if isinstance(c, list):
            try:
                c = _json.dumps(c, ensure_ascii=False)
            except Exception:
                c = str(c)
        total += len(str(c))
        tcs = m.get("tool_calls") or []
        if tcs:
            try:
                total += len(_json.dumps(tcs, ensure_ascii=False))
            except Exception:
                pass
    return total


def _count_tool_messages(messages: list[dict]) -> int:
    return sum(1 for m in messages if m.get("role") == "tool")


def _build_recent_file_refs(agent, turn_started_at: float) -> list[dict]:
    """Fallback FileCard envelope for files produced by side-channel tools.

    `_shadow.build_envelope_refs()` only sees files attached to the last
    assistant turn (i.e. files produced via the `write_file` tool). But
    bash scripts (`python build_pptx.py`) generate output files as a side
    effect — those land in the workspace but aren't on a turn.

    This helper scans the agent's workspace for files whose mtime falls
    within the current turn (mtime >= turn_started_at - 5s grace),
    filters out infrastructure files, and returns FileCard refs the
    same shape as `build_envelope_refs`.

    Returns [] silently on any error — the chat must not break.
    """
    import os as _os
    from pathlib import Path as _P

    if turn_started_at <= 0:
        return []
    grace = 5.0   # mtime can be slightly before turn_started_at on slow disks
    cutoff = turn_started_at - grace

    # Workspace dirs to scan: agent's working dir + shared workspace
    dirs: list[str] = []
    wd = getattr(agent, "working_dir", "") or ""
    if wd and _os.path.isdir(wd):
        dirs.append(wd)
    sw = getattr(agent, "shared_workspace", "") or ""
    if sw and _os.path.isdir(sw) and sw != wd:
        dirs.append(sw)
    if not dirs:
        return []

    # File-extension → ref kind mapping
    _EXT_KIND = {
        ".pptx": "document", ".pdf": "document", ".docx": "document",
        ".xlsx": "document", ".xls": "document", ".csv": "document",
        ".md": "document", ".txt": "document", ".html": "document",
        ".png": "image", ".jpg": "image", ".jpeg": "image",
        ".gif": "image", ".svg": "image", ".webp": "image",
        ".mp4": "video", ".mov": "video", ".webm": "video",
        ".mp3": "audio", ".wav": "audio",
        ".zip": "archive", ".tar": "archive", ".gz": "archive",
    }
    _EXT_MIME = {
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pdf":  "application/pdf",
        ".md":   "text/markdown",
        ".txt":  "text/plain",
        ".png":  "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    }
    _SKIP_NAMES = frozenset({
        "MCP.md", "Project.md", "Scheduled.md", "Skills.md", "Tasks.md",
        "AGENT.md", "agent.json",
    })
    _SKIP_DIRS = frozenset({"skills", "tool_outputs", "session", "logs",
                            "memory", ".git", "__pycache__"})

    out: list[dict] = []
    seen_paths: set = set()
    for d in dirs:
        try:
            for entry in _os.scandir(d):
                if not entry.is_file():
                    continue
                if entry.name in _SKIP_NAMES or entry.name.startswith("."):
                    continue
                p = _P(entry.path)
                if p.parent.name in _SKIP_DIRS:
                    continue
                ext = p.suffix.lower()
                kind = _EXT_KIND.get(ext)
                if kind is None:
                    continue   # only surface known doc/image/etc kinds
                try:
                    st = entry.stat()
                except OSError:
                    continue
                if st.st_mtime < cutoff:
                    continue   # not produced this turn
                abs_path = str(p)
                if abs_path in seen_paths:
                    continue
                seen_paths.add(abs_path)
                # Build FileCard ref shape matching _artifact_to_ref output
                from urllib.parse import quote as _q
                url = "/api/portal/attachment?path=" + _q(abs_path)
                out.append({
                    "id": f"recent_{abs(hash(abs_path)) & 0xffffffff:x}",
                    "url": url,
                    "path": abs_path,
                    "filename": p.name,
                    "label": p.name,
                    "kind": kind,
                    "mime": _EXT_MIME.get(ext, "application/octet-stream"),
                    "render_hint": "card",
                    "category": "auto_recent",
                    "size": st.st_size,
                    "produced_at": st.st_mtime,
                })
        except OSError:
            continue
    # Sort newest first, cap at 8 cards
    out.sort(key=lambda r: -float(r.get("produced_at", 0)))
    return out[:8]


def _drop_orphan_tool_messages(messages: list[dict]) -> list[dict]:
    """Drop any `role:tool` whose tool_call_id doesn't point at an
    IMMEDIATELY-PRECEDING assistant(tool_calls) with that id.

    DeepSeek rejects with `'tool' must be a response to a preceding message
    with 'tool_calls'` if there's ANY non-tool message between an assistant
    and its tool response. Our own sanitizer in llm.py uses 'active_ids'
    which handles most cases, but my recent summarize/hoist changes can
    occasionally drop the anchoring assistant(tc) while leaving the tool —
    producing an orphan that sneaks past. This is a belt-and-suspenders pass
    applied INSIDE the message pipeline to guarantee no orphans leave here.
    """
    if not messages:
        return messages
    active_ids: set = set()
    out: list[dict] = []
    dropped = 0
    for m in messages:
        r = m.get("role")
        if r == "assistant":
            tcs = m.get("tool_calls") or []
            if tcs:
                active_ids = {tc.get("id") or "" for tc in tcs}
                active_ids.discard("")
            else:
                active_ids = set()
            out.append(m)
        elif r == "tool":
            tcid = m.get("tool_call_id") or ""
            if tcid and tcid in active_ids:
                out.append(m)
            else:
                dropped += 1
                # don't clear active_ids — other tools in same batch OK
        else:
            active_ids = set()
            out.append(m)
    if dropped:
        logger.warning(
            "pipeline: dropped %d orphan tool message(s) before LLM send",
            dropped)
    return out


def cleanup_message_history(messages: list[dict],
                              *, log_label: str = "history") -> list[dict]:
    """Comprehensive message-history cleaner for LLM payloads.

    Catches the four classes of corruption that DeepSeek/OpenAI strict
    APIs reject — stricter than ``_drop_orphan_tool_messages`` because it
    also handles cumulative orphans, empty content, role-typing errors.

    Drops:
      1. **Orphan tool messages** — ``role:tool`` whose ``tool_call_id``
         has no matching ``assistant.tool_calls.id`` ANYWHERE earlier
         (cumulative, not just immediate-preceding).
      2. **Empty content** — assistant/user/system with no content AND no
         tool_calls (these provide no signal and bloat history).
      3. **Malformed role** — anything not in
         {system,user,assistant,tool} is dropped (rare, defensive).
      4. **Empty/missing tool_call_id on tool messages** — drop, since
         strict APIs require it.

    Does NOT reorder messages — preserves chronological order.

    Args:
        messages: list of OpenAI-shape message dicts
        log_label: included in WARNING log for traceability

    Returns: filtered list (never None; empty if all bad).
    """
    if not messages:
        return messages
    valid_roles = {"system", "user", "assistant", "tool"}
    seen_tc_ids: set[str] = set()
    out: list[dict] = []
    drops = {"orphan_tool": 0, "empty": 0, "bad_role": 0,
             "no_tcid": 0, "empty_tcs_stripped": 0}
    for m in messages:
        if not isinstance(m, dict):
            drops["bad_role"] += 1
            continue
        r = m.get("role", "")
        if r not in valid_roles:
            drops["bad_role"] += 1
            continue
        if r == "assistant":
            for tc in (m.get("tool_calls") or []):
                if isinstance(tc, dict):
                    tcid = tc.get("id") or ""
                    if tcid:
                        seen_tc_ids.add(tcid)
            # Empty assistant with neither content nor tool_calls = noise.
            content = m.get("content")
            has_content = bool((content or "")) if isinstance(content, str) else (content is not None)
            if not has_content and not (m.get("tool_calls") or []):
                drops["empty"] += 1
                continue
            # Strip empty tool_calls field — strict APIs (DeepSeek)
            # reject `tool_calls: []`. Drop the field entirely.
            if "tool_calls" in m and not m.get("tool_calls"):
                m = dict(m)
                m.pop("tool_calls", None)
                drops["empty_tcs_stripped"] += 1
            out.append(m)
        elif r == "tool":
            tcid = m.get("tool_call_id") or ""
            if not tcid:
                drops["no_tcid"] += 1
                continue
            if tcid not in seen_tc_ids:
                drops["orphan_tool"] += 1
                continue
            out.append(m)
        else:
            # system / user — drop only if completely empty
            content = m.get("content")
            if isinstance(content, str) and not content.strip():
                drops["empty"] += 1
                continue
            if content is None:
                drops["empty"] += 1
                continue
            out.append(m)
    actually_dropped = (drops["orphan_tool"] + drops["empty"]
                        + drops["bad_role"] + drops["no_tcid"])
    if actually_dropped or drops["empty_tcs_stripped"]:
        logger.warning(
            "[%s] cleanup_message_history: dropped=%d (orphan_tool=%d "
            "empty=%d bad_role=%d no_tcid=%d) modified=%d (empty_tcs_stripped) "
            "— was %d, now %d",
            log_label, actually_dropped,
            drops["orphan_tool"], drops["empty"],
            drops["bad_role"], drops["no_tcid"],
            drops["empty_tcs_stripped"],
            len(messages), len(out),
        )
    return out


def _find_safe_cut_idx(messages: list[dict], want_idx: int) -> int:
    """
    Find the largest cut idx <= want_idx that does NOT sever a
    tool_call/tool_result pair. Returns the idx to use as start of 'recent'.

    Rule: a 'tool' message requires its preceding assistant(tool_calls) to
    also be in the kept range. So walk backwards from want_idx: if we land
    on a 'tool', move cut back to just before the owning assistant.
    """
    if want_idx <= 0 or want_idx >= len(messages):
        return want_idx
    cut = want_idx
    # If cut points at a tool message, walk back past its assistant.
    while cut < len(messages) and messages[cut].get("role") == "tool":
        cut -= 1
        if cut < 0:
            return 0
    # Walk back past any assistant that has tool_calls (to grab whole batch)
    # only if cut is currently assistant(tool_calls) — we want the pair whole.
    while cut > 0:
        m = messages[cut]
        prev = messages[cut - 1]
        # If prev is assistant(tool_calls) and m is tool, we'd be severing.
        # Protect by moving cut back to include prev.
        if (m.get("role") == "tool"
                and prev.get("role") == "assistant"
                and prev.get("tool_calls")):
            cut -= 1
            continue
        break
    return max(0, cut)


def _summarize_old_history(messages: list[dict],
                            agent,
                            threshold_chars: int = _HISTORY_SUMMARY_CHARS,
                            keep_last: int = _HISTORY_SUMMARY_KEEP_LAST,
                            max_tool_msgs: int = _HISTORY_SUMMARY_MAX_TOOLS
                            ) -> list[dict]:
    """If messages are too big, summarize old range into one system message.

    The summary is cached on the agent under ``_history_summary_cache`` keyed
    by ``(old_range_hash, old_len)`` so iterations of the SAME chat turn don't
    re-pay the summarize call. When more messages later shift from 'recent'
    into 'old', the hash changes and we re-summarize.

    Never mutates the input. Returns the same list (identity) if no change.
    """
    if _HISTORY_SUMMARY_OFF:
        return messages
    if not messages:
        return messages
    total_chars = _estimate_messages_chars(messages)
    tool_count = _count_tool_messages(messages)
    # AND 逻辑: 总字符超阈值 AND tool 数也超(两者都说明历史真的膨胀了才动手)。
    # 旧的 OR 逻辑太激进,只要 tool_count>8 就触发,而 tool_count 在正常跑
    # 任务时很容易 8 (随便几次 glob+read 就到) → 频繁误触发。
    if total_chars < threshold_chars or tool_count <= max_tool_msgs:
        return messages

    # Split point: keep last `keep_last` messages + the leading system prefix.
    # Find where system prefix ends.
    sys_prefix_end = 0
    for i, m in enumerate(messages):
        if m.get("role") == "system":
            sys_prefix_end = i + 1
        else:
            break
    # Target start of "recent" block
    want_recent_start = max(sys_prefix_end, len(messages) - keep_last)
    # Protect tool pairs
    recent_start = _find_safe_cut_idx(messages, want_recent_start)
    if recent_start <= sys_prefix_end:
        # Nothing to summarize between system prefix and recent range.
        return messages
    old_slice = messages[sys_prefix_end:recent_start]
    if not old_slice:
        return messages

    # ── Min-benefit gate ── 老 slice 太小时摘要调用本身 (~2k token) 比省下的贵。
    old_chars = sum(len(str(m.get("content") or "")) for m in old_slice)
    if old_chars < _HISTORY_SUMMARY_MIN_OLD_CHARS:
        return messages

    # Cache check with INCREMENTAL REUSE.
    # 上次摘要覆盖了 cached_n 条 old 消息,本次 old_slice 可能只多了
    # 几条 (最常见:每轮 iter 多 1-2 条新 tool_result 挤进 old)。
    # 不达 RESUM_DELTA 就直接复用缓存的摘要文本,把新增的 delta 挤进 recent
    # 保持原样 (调整 recent_start 往前移),避免反复摘要。
    cache = getattr(agent, "_history_summary_cache", None)
    summary_text = ""
    if isinstance(cache, dict):
        cached_n = int(cache.get("covers_n") or 0)
        cached_chars = int(cache.get("covers_chars") or 0)
        delta_msgs = len(old_slice) - cached_n
        delta_chars = old_chars - cached_chars
        if (cached_n > 0
                and 0 <= delta_msgs < _HISTORY_SUMMARY_RESUM_DELTA_MSGS
                and delta_chars < _HISTORY_SUMMARY_RESUM_DELTA_CHARS):
            # Reuse cached summary; shrink old_slice to the originally-
            # covered prefix so the delta stays as verbatim messages
            # between the summary and 'recent'.
            summary_text = str(cache.get("text") or "")
            if summary_text:
                # Shift recent_start left so the delta messages are
                # preserved as-is. Protect tool pairs at the new boundary.
                new_recent_start = _find_safe_cut_idx(
                    messages, sys_prefix_end + cached_n)
                if new_recent_start > sys_prefix_end:
                    old_slice = messages[sys_prefix_end:new_recent_start]
                    recent_start = new_recent_start
                    logger.debug(
                        "HISTORY_SUMMARY reused cache (covers=%d, "
                        "delta_msgs=%d, delta_chars=%d) agent=%s",
                        cached_n, delta_msgs, delta_chars,
                        agent.id[:8] if agent else "?")

    if not summary_text:
        import json as _json
        # Compose a compact transcript. Truncate each message aggressively
        # — we're sending this to the LLM to BE summarized, so we can afford
        # bigger cuts than in the real prompt.
        lines: list[str] = []
        for m in old_slice:
            role = m.get("role") or "?"
            content = m.get("content") or ""
            if isinstance(content, list):
                try:
                    content = _json.dumps(content, ensure_ascii=False)
                except Exception:
                    content = str(content)
            content = str(content)
            # Inline tool_calls as a short marker
            tcs = m.get("tool_calls") or []
            if tcs:
                names = [((tc.get("function") or {}).get("name") or "?")
                         for tc in tcs]
                content = (content + " " if content else "") \
                    + f"[tool_calls: {', '.join(names)}]"
            if len(content) > 1500:
                content = content[:1500] + f"…({len(content)}c)"
            lines.append(f"[{role}] {content}")
        transcript = "\n".join(lines)

        prompt = (
            "以下是一段 agent 对话历史。请压缩成一份事实性摘要,下一轮用于给"
            "该 agent 做上下文提示。\n\n"
            "要求:\n"
            "1. 用中文,300-500 字。\n"
            "2. 只记事实,不要评论或润色。\n"
            "3. 分 3 节:\n"
            "   ## 用户意图: (1-2 句概括整条对话用户想达成什么)\n"
            "   ## 已完成: (bullet 列表: 调用了什么工具,得到什么关键结果,"
            "写入了哪些文件/路径)\n"
            "   ## 关键数据: (保留必须记住的字段/路径/数字/ID,不要省略)\n"
            "4. 禁止编造。历史里没写的不要加。\n\n"
            "--- 历史开始 ---\n"
            f"{transcript}\n"
            "--- 历史结束 ---"
        )
        summary_text = ""
        try:
            from . import llm as _llm
            # Use the agent's resolved provider/model (non-stream)
            try:
                _prov, _mdl = agent._resolve_effective_provider_model()
            except Exception:
                _prov, _mdl = "", ""
            resp = _llm.chat_no_stream(
                messages=[
                    {"role": "system",
                     "content": ("你是一个事实性对话摘要器。忠实压缩,"
                                 "不编造,不润色。")},
                    {"role": "user", "content": prompt},
                ],
                model=_mdl, provider=_prov,
            )
            if isinstance(resp, dict):
                _m = resp.get("message") or {}
                summary_text = str(_m.get("content") or "").strip()
                if not summary_text:
                    summary_text = str(_m.get("reasoning_content") or "").strip()
            logger.info(
                "HISTORY_SUMMARY: compressed %d old messages (~%d chars) "
                "into %d-char summary (agent=%s)",
                len(old_slice),
                sum(len(str(m.get("content") or "")) for m in old_slice),
                len(summary_text), agent.id[:8] if agent else "?")
        except Exception as e:
            logger.warning("history summarize failed: %s — keeping full history", e)
            return messages
        if not summary_text:
            return messages
        try:
            # 记下本次摘要覆盖的消息数 + 字符数,下次用于"增量复用"判定:
            # 只要新 old_slice 没比这个多 RESUM_DELTA_MSGS 条或
            # RESUM_DELTA_CHARS 字符,就直接复用 summary_text,不再 re-summarize。
            agent._history_summary_cache = {
                "text": summary_text,
                "covers_n": len(old_slice),
                "covers_chars": sum(
                    len(str(m.get("content") or "")) for m in old_slice),
            }
        except Exception:
            pass

    # Assemble: [system prefix ..., summary system msg, recent messages ...]
    summary_msg = {
        "role": "system",
        "content": (
            f"[HISTORY_SUMMARY — 覆盖 {len(old_slice)} 条旧消息]\n"
            f"{summary_text}"
        ),
    }
    result = list(messages[:sys_prefix_end])
    result.append(summary_msg)
    result.extend(messages[recent_start:])
    return result


# ── C: Skill-guide hoisting (Nov 2026) ───────────────────────────────────
# LLM 通过 `get_skill_guide(name)` 读取 SKILL.md 后,结果 (几 KB 的
# markdown) 会以 tool_result 形式留在 messages 里,每一轮迭代都随
# messages 全量重发给 LLM。这是 19-iter 场景下巨大的 token 浪费。
#
# 本函数做两件事:
#   1. 把 messages 里所有 get_skill_guide 的 tool_result 内容,统一抽
#      出来放到一块,塞进消息头部作为一个 system 块。
#   2. 原 tool_result 的 content 替换成短占位符 `[skill X 指南已加载 →
#      见消息头 LOADED_SKILLS]`,从这一步开始不再占几 KB。
#
# 为什么不放到 static system prompt?  因为 skill guide 是"按需拉取"
# 的 —— agent 没调 get_skill_guide 就不该付这个钱。只有实际拉过的才
# 挂到本 turn 的消息头。
#
# KV-cache 友好:  consolidated system 块放在系统前缀之后 (位置稳定),
# 后续 iteration 这块内容 hash 相同 → LM Studio / Anthropic prompt
# caching 可以命中。
#
# 不 mutate 输入 messages —— 返回新列表。

_SKILL_GUIDE_TOOL_NAMES = frozenset({
    "get_skill_guide", "skill_guide", "load_skill", "read_skill",
})


def _hoist_skill_guides(messages: list[dict]) -> list[dict]:
    """Pull get_skill_guide tool_result bodies into ONE consolidated
    system block at the front; replace the originals with placeholders.

    Returns a NEW list; never mutates input.
    """
    if not messages:
        return messages
    import json as _json

    # Pass 1: find tool_call_id -> skill_name for every get_skill_guide call.
    id_to_name: dict[str, str] = {}
    for m in messages:
        if m.get("role") != "assistant":
            continue
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            if fn.get("name") not in _SKILL_GUIDE_TOOL_NAMES:
                continue
            call_id = tc.get("id") or ""
            if not call_id:
                continue
            name = ""
            args = fn.get("arguments")
            if isinstance(args, str):
                try:
                    args = _json.loads(args)
                except Exception:
                    args = {}
            if isinstance(args, dict):
                # Canonical arg is `name`; be generous — accept a few variants.
                name = str(args.get("name")
                            or args.get("skill")
                            or args.get("skill_name")
                            or "").strip()
            id_to_name[call_id] = name or call_id[:8]

    if not id_to_name:
        return messages  # no guide calls → nothing to do

    # Pass 2: collect bodies + build placeholder messages.
    # Idempotence: if this function already ran on these messages, the tool
    # contents are already placeholders starting with `[skill `. Detect and
    # skip so running twice doesn't produce a 2nd LOADED_SKILLS block.
    loaded: dict[str, str] = {}   # skill_name → content (latest wins)
    new_msgs: list[dict] = []
    any_replaced = False
    already_hoisted = False
    for m in messages:
        if m.get("role") == "tool":
            tcid = m.get("tool_call_id") or ""
            if tcid in id_to_name:
                body = m.get("content")
                if isinstance(body, str) and body:
                    # Already a placeholder? skip reprocessing.
                    if body.startswith("[skill ") and "指南已加载" in body:
                        already_hoisted = True
                        new_msgs.append(m)
                        continue
                    skill = id_to_name[tcid]
                    loaded[skill] = body
                    any_replaced = True
                    placeholder = (
                        f"[skill `{skill}` 指南已加载 ({len(body)} chars) "
                        f"→ 内容见消息开头的 <LOADED_SKILLS> 块,不要再重复调用 "
                        f"get_skill_guide({skill})]"
                    )
                    new_msgs.append({**m, "content": placeholder})
                    continue
        new_msgs.append(m)

    # If we found existing placeholders AND there's already a LOADED_SKILLS
    # system block, treat as already-hoisted and return unchanged.
    if already_hoisted and not loaded:
        has_block = any(
            m.get("role") == "system"
            and isinstance(m.get("content"), str)
            and m["content"].startswith("<LOADED_SKILLS>")
            for m in messages)
        if has_block:
            return messages

    if not any_replaced or not loaded:
        return messages

    # Pass 3: find end of initial system prefix; insert consolidated block.
    sys_prefix_end = 0
    for i, m in enumerate(new_msgs):
        if m.get("role") == "system":
            sys_prefix_end = i + 1
        else:
            break

    parts = [
        "<LOADED_SKILLS>",
        "本轮已通过 get_skill_guide 拉取过的 skill 指南汇总。后续消息里看到 "
        "'[skill X 指南已加载 → 见消息头 LOADED_SKILLS]' 占位符就到这里查。",
        "",
    ]
    for name in sorted(loaded.keys()):
        body = loaded[name]
        # Cap each guide at 6KB so a pathological >20KB SKILL.md doesn't
        # blow up the prompt. 6KB ≈ 1.5k tokens per skill.
        if len(body) > 6000:
            body = body[:6000].rstrip() + "\n…(truncated, 原始 {0} chars)".format(
                len(loaded[name]))
        parts.append(f"### {name}")
        parts.append(body)
        parts.append("")
    parts.append("</LOADED_SKILLS>")

    consolidated = {
        "role": "system",
        "content": "\n".join(parts),
    }

    out = list(new_msgs[:sys_prefix_end])
    out.append(consolidated)
    out.extend(new_msgs[sys_prefix_end:])
    return out


# ── Narrator-stall detection (weak-model nudge) ────────────────────────────
# Weak / quantized / open-source models frequently reply with phrases like
# "Let me fix the errors:" or "让我检查一下：" and then end the turn *without*
# calling a tool.  The chat loop sees an empty tool_calls list and breaks,
# leaving the user staring at a promise that was never kept.
#
# This helper spots that pattern so the outer loop can inject a one-shot
# nudge ("you promised — now call the tool") and re-prompt once, instead of
# silently stalling.  Guarded by env var TUDOU_NUDGE_WEAK_MODELS (default on;
# set to "0" to disable globally).
_NARRATOR_STALL_PATTERNS = (
    # English
    "let me ", "let's ", "i'll ", "i will ", "i am going to",
    "i'm going to", "now let me", "first, let me", "first let me",
    "next, i'll", "next i'll", "i am about to", "i'm about to",
    # Chinese
    "让我", "我来", "我将", "我会", "我要", "接下来", "马上", "现在我",
    "下面我", "我准备",
)


def _looks_like_narrator_stall(text: str) -> bool:
    """True if `text` looks like a promise-without-action ("Let me X:" style).

    Heuristic:
      1. Non-empty text that ends with ``:`` or ``：`` (the "commitment colon")
      2. The trailing line contains an intent phrase ("let me", "让我" …)

    Both conditions must hold — this keeps false positives low (e.g. a
    genuine answer that happens to end with a colon before a code block
    won't match unless it also announces future work).
    """
    if not text:
        return False
    t = text.strip()
    if not t:
        return False
    if not (t.endswith(":") or t.endswith("：")):
        return False
    last_line = t.rsplit("\n", 1)[-1].lower()
    return any(p in last_line for p in _NARRATOR_STALL_PATTERNS)


# ── B: Granted-skills roster (Nov 2026) ──────────────────────────────────
# 一个短小的 "你有哪些技能" 清单 (name + 1 行描述), 注入到 static
# system prompt。让 LLM 不用再通过 get_skill_guide 探索就知道自己有
# 哪些能力。每个 skill ~40 tokens, 10-15 个约 500 tokens, 且因为是
# 静态 prompt 的一部分, 能被 KV cache / Anthropic prompt cache 吃掉。

def _build_granted_skills_roster(agent) -> str:
    """Short roster of all skills granted to this agent."""
    try:
        from .skills.engine import get_registry as _get_skill_registry
        reg = _get_skill_registry()
        if reg is None:
            return ""
        installs = reg.list_for_agent(agent.id)
    except Exception:
        return ""
    if not installs:
        return ""

    lines = [
        "## 你已装配的技能 (Installed Skills)",
        "以下是你可用的技能包。**需要执行时才调 `get_skill_guide(name)`** "
        "拿详细用法。不要在没必要时调用,也不要重复调用同一个。",
        "",
    ]
    # Stable order for KV cache: sort by name
    sorted_installs = sorted(
        installs,
        key=lambda i: ((i.manifest.name or i.id or "").lower(), i.id))
    for inst in sorted_installs:
        m = inst.manifest
        name = m.name or inst.id or "?"
        # Prefer zh-CN description; else fall back to generic description.
        desc = ""
        try:
            if hasattr(m, "get_description"):
                desc = m.get_description("zh-CN") or ""
        except Exception:
            pass
        if not desc:
            desc = getattr(m, "description", "") or ""
        # Flatten multi-line desc to first sentence + cap length.
        desc = str(desc).replace("\n", " ").strip()
        if len(desc) > 80:
            desc = desc[:80].rstrip() + "…"
        if desc:
            lines.append(f"- `{name}`: {desc}")
        else:
            lines.append(f"- `{name}`")

    # ── 通用 QA Gate ─────────────────────────────────────────────────
    # 适用于所有 skill / 所有产出物 (设计 / 报告 / 数据 / 代码 / 通信)。
    # Cause-of-write timeline:
    #   2026-04-30 小刚用 drawio-skill 交付 125×382 破损缩略图(没自检)
    #   2026-04-30 小刚用 pptx-author 交付 31 处 overlap/overflow 的 PPT(没跑 QA gate)
    #   2026-05-01 小刚用 send_email MCP 一边发邮件一边假装在等用户确认(没核对参数)
    # 三起事故的共同模式: 工具调用成功 != 产出可交付。
    # 解法: 把"生成 != 交付"作为铁律,每类产出 KV-cache 里直接给可执行检查 snippet,
    # agent 不用现想; QA gate 退出码非 0 时禁止说"已完成"。
    lines.extend([
        "",
        "### ⚠️ 强制 QA Gate — 任何产出在交付前必须通过可执行检查",
        "",
        "**铁律: 生成 ≠ 交付。** 工具调用成功只代表 \"动作完成\","
        "不代表 \"产出可用\"。任何文件 / 邮件 / MCP 结果 / 代码改动"
        "**交付给用户前必须跑 QA gate, 退出码非 0 时禁止说 \"已完成\"**。",
        "",
        "**优先级 1 — Skill 自带 QA gate**: 如果调用的 skill 在 SKILL.md "
        "末尾有 QA / 质量门段(drawio 的 Step 3.5/4.5/5、pptx-author 的 unified QA), "
        "**直接跑那段脚本**。skill 作者已经把该 skill 特有的检查项写好了。",
        "",
        "**优先级 2 — 通用 snippet (skill 没有 QA gate 时用):**",
        "",
        "```bash",
        "# 【代码 .py】",
        "python3 -m py_compile <file>.py        # 语法检查",
        "[ -d tests ] && python3 -m pytest -q   # 有测试就跑",
        "",
        "# 【代码 .sh / .bash】",
        "bash -n <file>.sh                       # 语法检查",
        "shellcheck <file>.sh 2>/dev/null || true",
        "",
        "# 【markdown / 报告 .md / .txt】",
        "python3 -c \"",
        "import re; t=open('<file>.md').read()",
        "bad=[]",
        "for p in [r'\\bxxx+\\b',r'\\bTODO\\b',r'\\[insert\\b',r'Lorem ipsum',r'<请填写>',r'\\bFIXME\\b']:",
        "    if re.search(p,t,re.I): bad.append(f'占位符遗留: {p}')",
        "if len(t.strip())<200: bad.append(f'太短: {len(t)} chars')",
        "lv=[len(m.group(1)) for m in re.finditer(r'^(#+) ',t,re.M)]",
        "for i in range(1,len(lv)):",
        "    if lv[i]-lv[i-1]>1: bad.append(f'标题跳级 h{lv[i-1]}->h{lv[i]}')",
        "print('❌ '+str(bad) if bad else '✓ OK')\"",
        "",
        "# 【数据 .csv】",
        "python3 -c \"",
        "import csv; rows=list(csv.reader(open('<file>.csv')))",
        "print(f'rows={len(rows)} cols={len(rows[0]) if rows else 0}')",
        "null=sum(1 for r in rows[1:] for c in r if not c.strip())",
        "print(f'空值: {null}; 首/末行: {rows[1] if len(rows)>1 else None} / {rows[-1] if len(rows)>1 else None}')\"",
        "",
        "# 【数据 .json】",
        "python3 -c \"",
        "import json; d=json.load(open('<file>.json'))",
        "print(f'type={type(d).__name__} keys={list(d.keys())[:5] if isinstance(d,dict) else \\\"list len=\\\"+str(len(d))}')\"",
        "",
        "# 【设计 .png / .jpg】",
        "python3 -c \"",
        "from PIL import Image; im=Image.open('<file>.png'); print(im.size, im.mode); ",
        "assert im.size[0]>=600 and im.size[1]>=400, '太小 可能渲染失败'\"",
        "",
        "# 【邮件 send_email / AgentMail MCP】发送前校验,不要发完了再后悔",
        "python3 -c \"",
        "to='<recipient>'; subject='<subj>'; body='<body>'; attachments=[]",
        "import re,os",
        "assert '@' in to and '.' in to.split('@')[1], f'收件人格式错: {to}'",
        "assert subject and len(subject)<=78, f'subject 空或超长: {len(subject)}'",
        "assert body and len(body)>=20, f'body 太短或空: {len(body)}'",
        "for a in attachments: assert os.path.isfile(a), f'附件不存在: {a}'",
        "print('✓ ready to send')\"",
        "",
        "# 【MCP 调用结果 通用】调完后立刻看",
        "# - 结果不为 None / 空字符串 / \\\"Error\\\" 开头",
        "# - 如果是 dict 检查没有 'error'/'errno'/'exception' 字段",
        "# - 数字结果在合理范围 (e.g. 余额非负、时间戳近期)",
        "```",
        "",
        "**校验通过后在回复中明示** —— 例: \"✓ 已跑 QA gate (4 项检查全过), 交付文件: /path/to/...\"",
        "**校验失败禁止上交** —— 回头修源,重跑 gate,exit 0 才说 \"已完成\"。如果改 5 轮仍过不了,向用户报告具体卸在哪一项。",
    ])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Plan D: handoff-trigger detection
# ---------------------------------------------------------------------------
# Weak models (esp. 4-bit quantized Qwen/MLX) routinely hallucinate a peer
# agent's response when told to hand off work ("交接给X"/"让Y审核") — they
# skip calling handoff_request entirely and fabricate the result in plain
# text.  When we detect a handoff trigger in the user message, we pass
# tool_choice=handoff_request to the LLM so it *cannot* return free text on
# that first iteration — it must call the tool or the server rejects it.
#
# Guarded by env var TUDOU_FORCE_HANDOFF (default on; "0" disables).
# Applied only on iteration 0 — subsequent iterations process tool results
# normally so the agent can reply to the user after the handoff completes.
import re as _re_handoff

_HANDOFF_TRIGGER_RE = _re_handoff.compile(
    # Chinese verb list — covers the full review/verify/test/check family
    # so "让他验收" / "让她测试" / "让大卫检查" all trigger, not just "审核".
    r"(交接给|移交给|转给|派给|"
    r"让\S{1,20}("
    r"审核|复核|评审|review|"
    r"验收|验证|verify|"
    r"检查|核对|check|"
    r"测试|test|"
    r"把关|过目|"
    r"做|完成|处理"
    r")|"
    # English: "hand off to X" / "ask X to (review|do|handle|check|verify|test)" / "pass it to X"
    r"hand\s*off\s+to\s|"
    r"ask\s+\S+\s+to\s+(review|do|handle|check|verify|test|accept)|"
    r"pass\s+(it|this|the\s+task)\s+to\s)",
    _re_handoff.IGNORECASE,
)


def _user_msg_triggers_handoff(text: str) -> bool:
    """True if the user message looks like it's asking for a work handoff."""
    if not text:
        return False
    return bool(_HANDOFF_TRIGGER_RE.search(text))


# Three-layer memory
try:
    from .memory import get_memory_manager, MemoryManager, MemoryConfig
except ImportError:
    try:
        from app.core.memory import get_memory_manager, MemoryManager, MemoryConfig
    except ImportError:
        get_memory_manager = None  # type: ignore
        MemoryManager = None  # type: ignore
        MemoryConfig = None  # type: ignore

logger = logging.getLogger("tudou.agent")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-5s %(name)s | %(message)s",
        datefmt="%H:%M:%S"))
    logger.addHandler(_h)
    logger.setLevel(logging.DEBUG)

from . import llm, tools, security
from .tools import PARALLEL_SAFE_TOOLS, MAX_PARALLEL_WORKERS
from .enhancement import (AgentEnhancer, build_enhancer, build_multi_enhancer,
                           list_enhancement_presets)
from .template_library import get_template_library
from .core.execution_analyzer import ExecutionAnalyzer, analyze_and_grow
from .core.prompt_enhancer import get_prompt_pack_registry
from .core.role_growth_path import RoleGrowthPath, ROLE_GROWTH_PATHS

# --- src package integration ---
import sys as _sys
_src_root = str(Path(__file__).resolve().parent.parent / "src")
if _src_root not in _sys.path:
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.session_store import StoredSession, save_session, load_session
from src.cost_tracker import CostTracker
from src.costHook import apply_cost_hook
from src.history import HistoryLog, HistoryEvent
from src.context import PortContext, build_port_context, render_context
from src.tools import (build_tool_backlog, find_tools as src_find_tools,
                       render_tool_index, execute_tool as src_execute_tool,
                       get_tools as src_get_tools, ToolExecution)
from src.commands import (execute_command as src_execute_command,
                          find_commands as src_find_commands, CommandExecution)
from src.transcript import TranscriptStore
from src.query_engine import QueryEnginePort, QueryEngineConfig, TurnResult
from src.execution_registry import (ExecutionRegistry, MirroredCommand,
                                     MirroredTool, build_execution_registry)
from src.tool_pool import ToolPool, assemble_tool_pool
from src.permissions import ToolPermissionContext
from src.runtime import PortRuntime, RuntimeSession, RoutedMatch
from src.models import PermissionDenial, UsageSummary, PortingModule


# ---------------------------------------------------------------------------
# Agent status
# ---------------------------------------------------------------------------

class AgentStatus(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    WAITING_APPROVAL = "waiting_approval"
    ERROR = "error"
    OFFLINE = "offline"


class AgentPhase(str, Enum):
    """State machine phases for task continuity.

    Controls how the agent routes incoming messages:
      IDLE      → no active task, new messages go to LLM normally
      PLANNING  → agent has decomposed a task into milestones/steps;
                   queries about plan/progress → local memory, no LLM
      EXECUTING → actively working through steps; interrupted tasks
                   resume from checkpoint via L3 memory injection
      REVIEWING → post-execution review/QA phase
      BLOCKED   → waiting for external input (user / another agent)
    """
    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    REVIEWING = "reviewing"
    BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# Task system
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class TaskSource(str, Enum):
    ADMIN = "admin"
    AGENT = "agent"
    SYSTEM = "system"
    USER = "user"


@dataclass
class AgentTask:
    """A trackable work item for an agent."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    title: str = ""
    description: str = ""
    status: TaskStatus = TaskStatus.TODO
    # priority: -1 = background growth (lowest, only run when no other task);
    #            0 = normal; 1 = high; 2 = urgent
    priority: int = 0
    parent_id: str = ""        # for sub-tasks
    assigned_by: str = ""      # who/what created it (hub, user, another agent)
    source: str = "admin"      # admin | agent | system | user | meeting
    source_agent_id: str = ""  # if source=agent, which agent created it
    source_meeting_id: str = ""   # if spawned from a meeting assignment
    source_assignment_id: str = ""  # the meeting assignment ID
    result: str = ""           # summary when done
    deadline: float = 0.0      # unix timestamp, 0 = no deadline
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)
    notified: bool = False     # whether agent has been notified of this task
    # ── Per-task LLM routing (override agent's default provider/model) ──
    # When set, the agent will use these for any LLM call made WHILE this task
    # is the currently executing task (see Agent._task_model_context).
    # Empty string = inherit agent's default provider/model.
    provider: str = ""
    model: str = ""
    # ── 方案乙: extra_llms 路由 label ──
    # 当 task 指定 llm_label 时，_resolve_effective_provider_model 会在
    # agent.extra_llms 里查找 label 或 purpose 相同的 slot，命中就用那
    # 个 provider/model。label 不命中会回退到默认 provider/model。
    llm_label: str = ""
    # Subkey of self_improvement._learning_queue if this is a growth task.
    learning_goal: str = ""
    knowledge_gap: str = ""
    # Recurrence: "once" | "daily" | "weekly" | "monthly" | "cron"
    recurrence: str = "once"
    # For daily: "HH:MM" (e.g. "09:00"). For weekly: "MON HH:MM".
    # For monthly: "D HH:MM" (e.g. "15 09:00"). For cron: raw cron string.
    recurrence_spec: str = ""
    # Unix timestamp of next scheduled run (0 = not scheduled)
    next_run_at: float = 0.0
    # Number of times this recurring task has fired
    run_count: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "priority": self.priority,
            "parent_id": self.parent_id,
            "assigned_by": self.assigned_by,
            "source": self.source,
            "source_agent_id": self.source_agent_id,
            "source_meeting_id": self.source_meeting_id,
            "source_assignment_id": self.source_assignment_id,
            "result": self.result,
            "deadline": self.deadline,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tags": self.tags,
            "notified": self.notified,
            "provider": self.provider,
            "model": self.model,
            "llm_label": self.llm_label,
            "learning_goal": self.learning_goal,
            "knowledge_gap": self.knowledge_gap,
            "recurrence": self.recurrence,
            "recurrence_spec": self.recurrence_spec,
            "next_run_at": self.next_run_at,
            "run_count": self.run_count,
        }

    @staticmethod
    def from_dict(d: dict) -> AgentTask:
        return AgentTask(
            id=d.get("id", ""),
            title=d.get("title", ""),
            description=d.get("description", ""),
            status=TaskStatus(d.get("status", "todo")),
            priority=d.get("priority", 0),
            parent_id=d.get("parent_id", ""),
            assigned_by=d.get("assigned_by", ""),
            source=d.get("source", "admin"),
            source_agent_id=d.get("source_agent_id", ""),
            source_meeting_id=d.get("source_meeting_id", ""),
            source_assignment_id=d.get("source_assignment_id", ""),
            result=d.get("result", ""),
            deadline=d.get("deadline", 0.0),
            created_at=d.get("created_at", 0),
            updated_at=d.get("updated_at", 0),
            tags=d.get("tags", []),
            notified=d.get("notified", False),
            provider=d.get("provider", "") or "",
            model=d.get("model", "") or "",
            llm_label=d.get("llm_label", "") or "",
            learning_goal=d.get("learning_goal", "") or "",
            knowledge_gap=d.get("knowledge_gap", "") or "",
            recurrence=d.get("recurrence", "once"),
            recurrence_spec=d.get("recurrence_spec", ""),
            next_run_at=float(d.get("next_run_at", 0.0) or 0.0),
            run_count=int(d.get("run_count", 0) or 0),
        )

    @property
    def deadline_str(self) -> str:
        if not self.deadline:
            return ""
        from datetime import datetime
        return datetime.fromtimestamp(self.deadline).strftime("%Y-%m-%d %H:%M")

    @property
    def is_overdue(self) -> bool:
        return self.deadline > 0 and time.time() > self.deadline and self.status not in (TaskStatus.DONE, TaskStatus.CANCELLED)


# ---------------------------------------------------------------------------
# Execution Steps — 执行步骤分解 (类似 Claude 的 TodoList)
# ---------------------------------------------------------------------------

class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ExecutionStep:
    """A single step in an agent's execution plan.

    Used to track real-time task decomposition — the agent breaks down
    a user request into steps, and marks them as it progresses.
    Similar to Claude's Todo widget.

    `acceptance` (P1/L2): a short natural-language criterion that MUST
    be satisfied before the step can be marked completed. Example:
    "生成 report.pptx ≥ 5 slides，落在 $AGENT_WORKSPACE"。Injected
    into system prompt so the LLM self-checks; backward compatible
    (missing acceptance = legacy step, no guard).
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    title: str = ""               # short description: "Read existing code"
    detail: str = ""              # longer description when needed
    acceptance: str = ""          # L2: what "done" looks like — plain text
    # Block 2 Review loop — when the LLM calls complete_step, if this
    # is set the step is handed to the verifier module. A failing
    # verifier rolls the step status back to FAILED with the verifier's
    # reason as result_summary. Shape: {"kind": "...", "config": {...},
    # "required": true, "timeout_s": 300}. None / empty = no auto check
    # (LLM's own result_summary + acceptance heuristic is the only gate).
    verify: dict = field(default_factory=dict)
    status: StepStatus = StepStatus.PENDING
    order: int = 0                # display order
    parent_step_id: str = ""      # for nested sub-steps
    depends_on: list[str] = field(default_factory=list)  # step IDs this step depends on
    started_at: float = 0.0
    completed_at: float = 0.0
    result_summary: str = ""      # brief result after completion
    # LLM routing hint — which category best fits this step. Filled by the
    # primary LLM when calling plan_update(create_plan) with scores table
    # injected in its system prompt. Read by the per-iteration LLM resolver
    # as a category override (beats keyword detection). "" = fall back.
    # Valid: tool-heavy | multimodal | reasoning | analysis | coding | default
    llm_purpose: str = ""
    llm_rationale: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title,
            "detail": self.detail,
            "acceptance": self.acceptance,
            "verify": dict(self.verify) if self.verify else {},
            "status": self.status.value,
            "order": self.order,
            "parent_step_id": self.parent_step_id,
            "depends_on": self.depends_on,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result_summary": self.result_summary,
            "llm_purpose": self.llm_purpose,
            "llm_rationale": self.llm_rationale,
        }

    @staticmethod
    def from_dict(d: dict) -> "ExecutionStep":
        return ExecutionStep(
            id=d.get("id", uuid.uuid4().hex[:8]),
            title=d.get("title", ""),
            detail=d.get("detail", ""),
            acceptance=d.get("acceptance", ""),
            verify=dict(d.get("verify") or {}),
            status=StepStatus(d.get("status", "pending")),
            order=d.get("order", 0),
            parent_step_id=d.get("parent_step_id", ""),
            depends_on=d.get("depends_on", []),
            started_at=d.get("started_at", 0),
            completed_at=d.get("completed_at", 0),
            result_summary=d.get("result_summary", ""),
            llm_purpose=str(d.get("llm_purpose") or ""),
            llm_rationale=str(d.get("llm_rationale") or ""),
        )


@dataclass
class ExecutionPlan:
    """A plan containing multiple execution steps for a task.

    Each chat message that triggers tool usage creates a new plan.
    The agent decomposes the task into steps and updates them in real-time.
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    task_summary: str = ""        # what the user asked for
    steps: list[ExecutionStep] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    completed_at: float = 0.0
    status: str = "active"        # active | completed | failed
    # Index into agent.messages at the moment this plan was created.
    # Used by ``Agent._fold_completed_plan_into_recap`` to identify which
    # messages belong to this plan's lifecycle. -1 = no anchor recorded
    # (older plans pre-fold-feature, or plan loaded from disk).
    msg_anchor_idx: int = -1

    def add_step(self, title: str, detail: str = "",
                 parent_step_id: str = "",
                 acceptance: str = "",
                 verify: Optional[dict] = None) -> ExecutionStep:
        step = ExecutionStep(
            title=title, detail=detail,
            acceptance=acceptance,
            verify=dict(verify) if verify else {},
            order=len(self.steps),
            parent_step_id=parent_step_id,
        )
        self.steps.append(step)
        return step

    def start_step(self, step_id: str):
        for s in self.steps:
            if s.id == step_id:
                s.status = StepStatus.IN_PROGRESS
                s.started_at = time.time()
                return s
        return None

    def complete_step(self, step_id: str, result_summary: str = ""):
        step_found = None
        for s in self.steps:
            if s.id == step_id:
                s.status = StepStatus.COMPLETED
                s.completed_at = time.time()
                s.result_summary = result_summary
                step_found = s
                break
        # After completing a step, check if all steps are done
        if all(s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED)
               for s in self.steps):
            self.status = "completed"
            self.completed_at = time.time()
        return step_found

    def fail_step(self, step_id: str, error: str = ""):
        for s in self.steps:
            if s.id == step_id:
                s.status = StepStatus.FAILED
                s.completed_at = time.time()
                s.result_summary = error
                return s
        return None

    def get_progress(self) -> dict:
        total = len(self.steps)
        done = sum(1 for s in self.steps
                   if s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED))
        in_progress = sum(1 for s in self.steps
                          if s.status == StepStatus.IN_PROGRESS)
        return {
            "total": total, "done": done,
            "in_progress": in_progress,
            "pending": total - done - in_progress,
            "percent": int(done / total * 100) if total else 0,
        }

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_summary": self.task_summary,
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "progress": self.get_progress(),
        }

    @staticmethod
    def from_dict(d: dict) -> "ExecutionPlan":
        plan = ExecutionPlan(
            id=d.get("id", uuid.uuid4().hex[:10]),
            task_summary=d.get("task_summary", ""),
            created_at=d.get("created_at", time.time()),
            completed_at=d.get("completed_at", 0),
            status=d.get("status", "active"),
        )
        for sd in d.get("steps", []):
            plan.steps.append(ExecutionStep.from_dict(sd))
        return plan


# ---------------------------------------------------------------------------
# MCP server config (per agent)
# ---------------------------------------------------------------------------

@dataclass
class MCPServerConfig:
    """Configuration for an external MCP server connection."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = ""
    transport: str = "stdio"    # "stdio" | "sse" | "streamable-http"
    command: str = ""           # for stdio: e.g. "npx @modelcontextprotocol/server-filesystem /tmp"
    url: str = ""               # for sse/http: e.g. "http://localhost:3000/mcp"
    env: dict = field(default_factory=dict)
    enabled: bool = True
    # ── 作用域 ──
    scope: str = "node"               # "global" (API类) | "node" (需本地安装)
    # ── 安装状态 ──
    install_status: str = "unknown"   # "unknown"|"not_installed"|"installing"|"installed"|"failed"
    install_error: str = ""           # 安装失败时的错误信息
    install_command: str = ""         # 记录对应的安装命令
    installed_at: float = 0           # 安装成功的时间戳

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "url": self.url,
            "env": self.env,
            "enabled": self.enabled,
            "scope": self.scope,
            "install_status": self.install_status,
            "install_error": self.install_error,
            "install_command": self.install_command,
            "installed_at": self.installed_at,
        }

    @staticmethod
    def from_dict(d: dict) -> MCPServerConfig:
        return MCPServerConfig(
            id=d.get("id", ""),
            name=d.get("name", ""),
            transport=d.get("transport", "stdio"),
            command=d.get("command", ""),
            url=d.get("url", ""),
            env=d.get("env", {}),
            enabled=d.get("enabled", True),
            scope=d.get("scope", "node"),
            install_status=d.get("install_status", "unknown"),
            install_error=d.get("install_error", ""),
            install_command=d.get("install_command", ""),
            installed_at=d.get("installed_at", 0),
        )


# ---------------------------------------------------------------------------
# Agent profile — personality, style, expertise
# ---------------------------------------------------------------------------

@dataclass
class AgentProfile:
    """Rich agent configuration beyond just role."""
    agent_class: str = "enterprise"
    # Agent classification: "advisor" (专业领域顾问), "enterprise" (企业办公),
    # "personal" (个人应用).  Determines default capabilities, memory, and UI grouping.
    memory_mode: str = "full"
    # Memory persistence mode:
    #   "full"  — all 5 memory layers active (intent/reasoning/outcome/rule/reflection)
    #   "light" — L1 working memory + L2 recent N entries only
    #   "off"   — no persistent memory (stateless per session)
    rag_mode: str = "shared"
    # RAG knowledge retrieval mode:
    #   "shared"  — query the global shared knowledge base (enterprise default)
    #   "private" — query agent's own private knowledge collection (advisor default)
    #   "both"    — query private first, fall back to shared
    #   "none"    — no RAG retrieval (personal default)
    rag_provider_id: str = ""
    # RAG provider to use. Empty = local ChromaDB.
    # Can reference a registered RAG provider (e.g. a remote node endpoint,
    # third-party vector DB API, etc.) via the RAG provider registry.
    rag_collection_ids: list[str] = field(default_factory=list)
    # Additional knowledge collection IDs to query (for fine-grained control).
    # Advisor agents auto-get a private collection named "advisor_{agent_id}".
    # Users can also manually bind extra collections here.
    personality: str = "helpful"
    # e.g. "friendly", "formal", "concise", "patient", "strict"
    communication_style: str = "technical"
    # e.g. "technical", "casual", "detailed", "brief", "educational"
    expertise: list[str] = field(default_factory=list)
    # e.g. ["python", "rust", "kubernetes", "database", "security"]
    skills: list[str] = field(default_factory=list)
    # e.g. ["code_review", "testing", "refactoring", "documentation", "debugging"]
    language: str = "auto"
    # e.g. "zh-CN", "en", "ja", "auto" (follow user's language)
    max_context_messages: int = 50
    # Max messages to keep in context window
    allowed_tools: list[str] = field(default_factory=list)
    # Empty = all tools; non-empty = only these tools
    denied_tools: list[str] = field(default_factory=list)
    # Tools this agent is not allowed to use
    auto_approve_tools: list[str] = field(default_factory=list)
    # Tools that skip approval for this agent (e.g. coder can auto-approve write_file)
    temperature: float = 0.7
    # LLM temperature for this agent
    # P1-C: per-agent output token budget. 0 = no explicit budget (use
    # provider default). When > 0, the chat loop injects a compact
    # system hint "⚠️ 本轮输出 ≤ N tokens" on the LAST message before
    # the LLM call, nudging the model toward concise output. This is
    # advisory — LLMs usually respect explicit budget hints but some
    # providers also expose a hard max_tokens parameter we can wire
    # through later.
    max_output_tokens: int = 0
    # P1-C: agent tier. Drives default budget shape:
    #   "thinker"    — high input, medium output (plan / analyze)
    #   "actor"      — low input, short output (execute tools)
    #   "summarizer" — medium input, very short output (compress)
    #   ""           — no tier-based defaults
    agent_tier: str = ""
    custom_instructions: str = ""
    # Extra instructions appended to system prompt
    exec_policy: str = "ask"
    # 'full' = auto-approve all, 'deny' = block all, 'ask' = prompt user
    exec_blacklist: list[str] = field(default_factory=list)
    # Commands that are always blocked for this agent
    exec_whitelist: list[str] = field(default_factory=list)
    # Commands that are always allowed for this agent
    mcp_servers: list = field(default_factory=list)
    # List of MCPServerConfig dicts for this agent
    sandbox_mode: str = ""
    # "" (use global default), "off", "restricted", or "strict"
    sandbox_allow_commands: list[str] = field(default_factory=list)
    # Command allowlist (first-token basenames) for strict sandbox mode
    skill_capabilities: list[str] = field(default_factory=list)
    # Permanently granted skill capabilities, e.g. ["pdf:rw", "docx:rw"]
    # Populated automatically when a skill is granted to the agent.

    # ══════════════════════════════════════════════════════════════════════
    # RolePresetV2 — 7-dimensional role enhancement (all fields optional, V1 agents keep defaults)
    # ══════════════════════════════════════════════════════════════════════
    role_preset_id: str = ""
    # References a RolePresetV2 loaded from data/roles/*.yaml.
    # Empty = legacy behavior (V1 compatibility).
    role_preset_version: int = 1
    # 1 = legacy V1 preset (prompt-only), 2 = V2 (7-dim enhanced).
    llm_tier: str = ""
    # LLM capability tier: "reasoning_strong" | "coding_strong" | "writing_strong"
    # | "fast_cheap" | "multimodal" | "domain_specific" | "" (use agent.provider/model)
    # Resolved by LLMTierRouter at runtime to a concrete provider/model.
    llm_tier_overrides: dict = field(default_factory=dict)
    # Per-context tier overrides, e.g. {"multimodal": "multimodal", "coding": "coding_strong"}
    sop_template_id: str = ""
    # References a WorkflowTemplate used as this role's SOP (state machine).
    # Empty = no SOP (free-form execution).
    use_langgraph: bool = False
    # Legacy field, kept for back-compat. The default path is now
    # LangGraph; see ``force_v1`` below for the inverse opt-out.
    force_v1: bool = False
    # When True (or env TUDOU_USE_LEGACY_V1=1 globally), this agent's
    # chat_async runs through the V1 chat loop instead of the new
    # LangGraph state machine. Default False — LangGraph is the
    # production path. Use this only as a kill switch when a graph
    # bug surfaces and you need to keep an agent running on V1 while
    # the bug is fixed.
    quality_rules: list = field(default_factory=list)
    # List of QualityCheckRule dicts (see app/quality_gate.py).
    # Empty = no quality gate.
    output_contract: dict = field(default_factory=dict)
    # What this role produces: {"produces": [...], "schema": {...}}
    input_contract: dict = field(default_factory=dict)
    # What this role accepts: {"accepts": [...], "requires_fields": [...]}
    kpi_definitions: list = field(default_factory=list)
    # List of KPIDefinition dicts for this role.

    def __post_init__(self):
        # Defensive: any list-typed field passed as ``None`` (legacy
        # saved JSON, preset round-trip via to_dict→AgentProfile(**d),
        # explicit ``None`` from form input) → coerce to ``[]``. Without
        # this guard, ``list(profile.allowed_tools)`` crashes downstream
        # when re-saving the profile from another agent's update.
        for _f in (
            "rag_collection_ids", "expertise", "skills",
            "allowed_tools", "denied_tools", "auto_approve_tools",
            "exec_blacklist", "exec_whitelist", "mcp_servers",
            "sandbox_allow_commands", "skill_capabilities",
            "quality_rules", "kpi_definitions",
        ):
            if getattr(self, _f, None) is None:
                setattr(self, _f, [])
        for _d in ("llm_tier_overrides", "output_contract", "input_contract"):
            if getattr(self, _d, None) is None:
                setattr(self, _d, {})

    def to_dict(self) -> dict:
        return {
            "agent_class": self.agent_class,
            "memory_mode": self.memory_mode,
            "rag_mode": self.rag_mode,
            "rag_provider_id": self.rag_provider_id,
            "rag_collection_ids": self.rag_collection_ids,
            "personality": self.personality,
            "communication_style": self.communication_style,
            "expertise": self.expertise,
            "skills": self.skills,
            "language": self.language,
            "max_context_messages": self.max_context_messages,
            "allowed_tools": self.allowed_tools,
            "denied_tools": self.denied_tools,
            "auto_approve_tools": self.auto_approve_tools,
            "temperature": self.temperature,
            "max_output_tokens": self.max_output_tokens,
            "agent_tier": self.agent_tier,
            "custom_instructions": self.custom_instructions,
            "exec_policy": self.exec_policy,
            "exec_blacklist": self.exec_blacklist,
            "exec_whitelist": self.exec_whitelist,
            "mcp_servers": [s.to_dict() if hasattr(s, 'to_dict') else s
                           for s in self.mcp_servers],
            "sandbox_mode": self.sandbox_mode,
            "sandbox_allow_commands": self.sandbox_allow_commands,
            "skill_capabilities": self.skill_capabilities,
            # RolePresetV2 fields (all optional)
            "role_preset_id": self.role_preset_id,
            "role_preset_version": self.role_preset_version,
            "llm_tier": self.llm_tier,
            "llm_tier_overrides": self.llm_tier_overrides,
            "sop_template_id": self.sop_template_id,
            "quality_rules": self.quality_rules,
            "output_contract": self.output_contract,
            "input_contract": self.input_contract,
            "kpi_definitions": self.kpi_definitions,
        }

    @staticmethod
    def from_dict(d: dict) -> AgentProfile:
        mcp_servers = []
        for s in d.get("mcp_servers", []):
            if isinstance(s, dict):
                mcp_servers.append(MCPServerConfig.from_dict(s))
            elif isinstance(s, MCPServerConfig):
                mcp_servers.append(s)
        return AgentProfile(
            agent_class=d.get("agent_class", "enterprise"),
            memory_mode=d.get("memory_mode", "full"),
            rag_mode=d.get("rag_mode", "shared"),
            rag_provider_id=d.get("rag_provider_id", ""),
            # NB: ``or []`` (not just ``, []``) — saved JSON may contain
            # ``"allowed_tools": null`` etc. for older agents, and dict.get's
            # default only fires when the KEY is absent, not when the
            # VALUE is None. Without ``or []`` we'd hand back None and
            # downstream ``list(profile.allowed_tools)`` crashes.
            rag_collection_ids=d.get("rag_collection_ids") or [],
            personality=d.get("personality", "helpful"),
            communication_style=d.get("communication_style", "technical"),
            expertise=d.get("expertise") or [],
            skills=d.get("skills") or [],
            language=d.get("language", "auto"),
            max_context_messages=d.get("max_context_messages", 50),
            allowed_tools=d.get("allowed_tools") or [],
            denied_tools=d.get("denied_tools") or [],
            auto_approve_tools=d.get("auto_approve_tools") or [],
            temperature=d.get("temperature", 0.7),
            max_output_tokens=int(d.get("max_output_tokens", 0) or 0),
            agent_tier=d.get("agent_tier", "") or "",
            custom_instructions=d.get("custom_instructions", ""),
            exec_policy=d.get("exec_policy", "ask"),
            exec_blacklist=d.get("exec_blacklist") or [],
            exec_whitelist=d.get("exec_whitelist") or [],
            mcp_servers=mcp_servers,
            sandbox_mode=d.get("sandbox_mode", ""),
            sandbox_allow_commands=d.get("sandbox_allow_commands") or [],
            skill_capabilities=d.get("skill_capabilities") or [],
            # RolePresetV2 fields (all with safe defaults → V1 agents compatible)
            role_preset_id=d.get("role_preset_id", ""),
            role_preset_version=int(d.get("role_preset_version", 1)),
            llm_tier=d.get("llm_tier", ""),
            llm_tier_overrides=d.get("llm_tier_overrides") or {},
            sop_template_id=d.get("sop_template_id", ""),
            quality_rules=d.get("quality_rules") or [],
            output_contract=d.get("output_contract") or {},
            input_contract=d.get("input_contract") or {},
            kpi_definitions=d.get("kpi_definitions") or [],
        )


# ---------------------------------------------------------------------------
# Event log entry
# ---------------------------------------------------------------------------

@dataclass
class AgentEvent:
    timestamp: float
    kind: str   # message | tool_call | tool_result | error | delegate | status | approval
    data: dict

    def to_dict(self) -> dict:
        return {"timestamp": self.timestamp, "kind": self.kind, "data": self.data}


# ---------------------------------------------------------------------------
# Retrieval Protocol — auto-injected into system prompt for RAG-bound agents
# ---------------------------------------------------------------------------
_RETRIEVAL_PROTOCOL_TEXT = (
    "【检索协议 — 强制执行】\n"
    "你绑定了专业领域知识库 (RAG)。对于任何涉及知识库内容的问题"
    "（事实、数量、文档内容、概念、术语、验收用例、架构、产品规格等），"
    "必须遵守：\n"
    "1. 第一步 ALWAYS 调用 `knowledge_lookup`。禁止用 bash / read_file "
    "/ search_files / grep 去工作区找知识库内容——工作区里没有 KB 文档。\n"
    "2. 根据问题类型选模式：\n"
    "   • 普通事实/解释题 → `knowledge_lookup(query=\"...\", mode=\"search\")`"
    "（默认，返回 top-8 chunk 含 content）。\n"
    "   • 聚合/计数题（\"有多少\"、\"总数\"、\"多少个\"）→ "
    "`knowledge_lookup(query=\"...关键词...\", mode=\"count\")`。"
    "这是对全库的精确扫描，不是 top-k 抽样——直接复用返回的数字，不要"
    "自己估算。\n"
    "   • 目录/清单题（\"列出所有\"、\"都有哪些\"、\"文档目录\"）→ "
    "`knowledge_lookup(query=\"...\", mode=\"list\")`。\n"
    "3. mode=search 返回 `{entries:[{content, source_file, heading_path, "
    "chunk_index, ...}]}`，直接从 `entries[*].content` 推理。禁止说"
    " \"只返回了索引/未提供内容\"——内容就在 entries 里。\n"
    "4. 召回不足时换 2-3 个关键词再试（中英同义词、换 query 角度）。"
    "单次 search 最多 8 块。\n"
    "5. 每个事实必须引用来源：`[source_file §heading_path]` 或 "
    "`[title #chunk_index]`。没有引用的回答不合格。\n"
    "6. 2-3 次检索后仍无结果 → 说「知识库中未找到直接答案」并建议用户"
    "补充什么材料。禁止编造、禁止用 bash 兜底。"
)


def _build_retrieval_protocol(profile: "AgentProfile") -> str:
    """Return the Retrieval Protocol block if this agent has a bound KB,
    else empty string.

    Triggers on either:
      * rag_collection_ids non-empty (user bound a domain KB), OR
      * agent_class == "advisor" with any non-empty rag_mode

    Kept in code (not agents.json) so the hub's periodic save of agent
    state cannot clobber it.
    """
    if profile is None:
        return ""
    mode = getattr(profile, "rag_mode", "") or ""
    if mode in ("", "none"):
        return ""
    has_kb = bool(getattr(profile, "rag_collection_ids", None))
    is_advisor = getattr(profile, "agent_class", "") == "advisor"
    if not (has_kb or is_advisor):
        return ""
    return _RETRIEVAL_PROTOCOL_TEXT


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

@dataclass
class Agent:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "Agent"
    role: str = "general"
    model: str = ""
    provider: str = ""
    # ── Learning model: cheap/local LLM used for self-growth tasks ──
    # If empty, falls back to the agent's main provider/model.
    learning_provider: str = ""
    learning_model: str = ""
    # ── Multimodal model: vision/audio-capable LLM used when the input
    #    contains image or audio parts. Falls back to primary when empty. ──
    multimodal_provider: str = ""
    multimodal_model: str = ""
    # Whether the multimodal model supports tool calling.
    # False (default): tools disabled during vision calls (e.g. llama3.2-vision)
    # True: tools kept enabled (e.g. gpt-4o, claude-3.5-sonnet)
    multimodal_supports_tools: bool = False
    # ── Coding model: code-optimized LLM for tool-calling & code generation ──
    # If empty, falls back to the agent's main provider/model.
    coding_provider: str = ""
    coding_model: str = ""
    # ── Extra LLM slots (N-labeled models) ──
    # 每一项: {"label": "code_review", "provider": "openai", "model": "gpt-4o",
    #          "purpose": "code_review", "note": ""}
    # task 运行时可以通过 ChatTask.llm_label 指定走哪个 slot；
    # 没指定就走默认的 provider/model（或 auto_route 的启发式）。
    extra_llms: list[dict] = field(default_factory=list)

    # ── Score-based auto-route (supersedes the old label-map) ──
    # 当前设计：每个 iteration 按消息/任务信号判定 category（tool-heavy /
    # multimodal / reasoning / analysis / complex / default），再按打分
    # 挑 extra_llms + primary 里最合适的那个。打分来源：
    #   1. slot 里自己写的 `scores: {category: float}`（用户实测，最高优先级）
    #   2. app/data/model_scores.json（公共 benchmark 归一，0.0-10.0）
    #   3. 都没覆盖 → 5.0（中性）
    # 结构：
    #   {
    #     "enabled": true,              # 总开关；false = 始终用 primary
    #     "complex_threshold_chars": 2000,  # prompt 超过多少字算 complex
    #   }
    # 没有 extra_llms 或所有候选分数 <= primary → 用 primary。
    # task 显式指定 llm_label 的优先级仍然高于 auto_route。
    # 每个路由决策会在 logger.info 里打印完整分数明细，方便排查。
    auto_route: dict = field(default_factory=dict)
    working_dir: str = ""
    system_prompt: str = ""
    profile: AgentProfile = field(default_factory=AgentProfile)
    status: AgentStatus = AgentStatus.IDLE
    agent_phase: AgentPhase = AgentPhase.IDLE  # State machine phase for task continuity
    node_id: str = "local"
    # ⚠️ DEPRECATED (2026-04-29) — DO NOT READ DIRECTLY.
    # An agent participates in N projects + M meetings simultaneously,
    # so a single shared_workspace value is structurally wrong. The
    # right path varies per active context. Use:
    #     self.get_active_shared_workspace()
    # which derives from `_active_context_id` + hub lookup at call
    # time. Field kept on the dataclass (and in persistence) only for
    # backward-compat with old agent.json files; new code MUST NOT
    # write to it. Read sites are progressively being migrated to the
    # method (see refs in tools / sandbox / system prompt).
    shared_workspace: str = ""
    project_id: str = ""  # Project ID if agent belongs to a project
    project_name: str = ""  # Project name for prompt context
    context_type: str = "solo"  # "solo" | "project" | "meeting"
    # Determines where produced files go:
    #   solo    → agent's private workspace (working_dir)
    #   project → project shared_workspace (no per-file decision)
    #   meeting → meeting shared_workspace (no per-file decision)
    # See prompt builder in _build_system_prompt(); callers should set this
    # at create-time based on whether a project_id / source_meeting_id is
    # present. Default "solo" keeps legacy single-agent behavior.
    parent_id: str = ""  # If set, this is a sub-agent (hidden from UI by default)
    priority_level: int = 3  # 1=CXO (highest), 2=PM, 3=Team Member (default)
    role_title: str = ""  # e.g. "CXO", "PM", "Developer", etc.
    department: str = ""  # Organizational unit: 研发/产品/运营/市场/... empty = 未分配
    # Ownership: user_id of the admin/user who created this agent.
    # Empty string = legacy/unowned (pre-migration); treated as
    # superAdmin-only to be safe. Regular admins can only manage
    # agents whose owner_id == their user_id OR whose id is in their
    # AdminUser.agent_ids delegation list. See app/permissions.py.
    owner_id: str = ""
    authorized_workspaces: list[str] = field(default_factory=list)  # List of agent IDs whose workspaces this agent can access
    soul_md: str = ""  # SOUL.md personality/persona in markdown
    robot_avatar: str = ""  # Robot avatar ID e.g. "robot_ceo"
    messages: list[dict] = field(default_factory=list)
    # ── Per-context message stores (2026-04-28) ──────────────────────
    # Maps context_id → its own message list. Each call surface (solo
    # chat, project chat, meeting) gets its own conversation history so
    # they don't bleed into each other (e.g. an image uploaded in solo
    # chat must NOT appear in a new project's first turn).
    #
    # Conventions:
    #   "solo"           — direct admin/user chat with the agent
    #   "project:{pid}"  — chat inside a Project group
    #   "meeting:{mid}"  — chat inside a Meeting
    #
    # `self.messages` is rebound to point at the active context's list
    # by `_switch_context()`; existing code that does `self.messages.append(...)`
    # / trim / etc. keeps working unchanged — it just operates on whatever
    # list is currently active. The agent's `_lock` serializes turns, so
    # there's no concurrent-context race within one agent.
    _messages_by_context: dict[str, list[dict]] = field(
        default_factory=lambda: {"solo": []}, repr=False)
    _active_context_id: str = field(default="solo", repr=False)
    events: list[AgentEvent] = field(default_factory=list)
    tasks: list[AgentTask] = field(default_factory=list)
    channel_ids: list[str] = field(default_factory=list)  # bound channel IDs
    # ── Success rate counters (P1, 2026-04-27) ──
    # Incremented when an AgentTask transitions to DONE / BLOCKED /
    # CANCELLED. Used by long_task.auto_assign as a tiebreaker when
    # multiple agents match the same role_hint — pick the one with
    # higher success rate. Smoothed: (success + 1) / (success + fail + 2)
    # so a brand-new agent (0/0) starts at 50% rather than NaN.
    role_success_count: int = 0
    role_fail_count: int = 0
    role_last_success_at: float = 0.0  # for "recent activity" surfacing
    # ── Preprocessor (small local LLM) — per-agent opt-in ─────────────
    # Empty preprocessor_model = disabled. When set (e.g. "qwen2.5:3b-instruct"),
    # the preprocessor bridge fires at 3 fixed nodes:
    #   ① first-turn task setup
    #   ② task decomposition (propose_decomposition)
    #   ③ prompt optimization
    # ``preprocessor_modes`` further selects which behaviours run at
    # node ①: "optimize_prompt" rewrites the system prompt for the task,
    # "task_understanding" emits recommended params (tier, RAG, decomp).
    preprocessor_model: str = ""
    preprocessor_modes: list[str] = field(default_factory=list)
    # Optional endpoint URL — defaults to Ollama (http://localhost:11434).
    # Set to e.g. "http://127.0.0.1:10240" for MLX-LM server. Both speak
    # OpenAI-compat /v1/chat/completions.
    preprocessor_endpoint: str = ""
    # Optional fallback chain. Each entry is "model" or "model@endpoint".
    # Tried in order when the primary fails (timeout / HTTP error). When
    # ALL entries fail, bridge returns ok=False and caller falls back to
    # the original non-preprocessor path — so worst case is "no benefit",
    # never broken behaviour. Example:
    #   ["gemma4:e4b@http://localhost:11434",
    #    "qwen2.5:3b-instruct@http://localhost:11434"]
    preprocessor_fallback: list[str] = field(default_factory=list)
    granted_skills: list[str] = field(default_factory=list)  # Skill IDs granted to this agent
    created_at: float = field(default_factory=time.time)
    # --- src integration ---
    cost_tracker: CostTracker = field(default_factory=CostTracker)
    history_log: HistoryLog = field(default_factory=HistoryLog)
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    # --- src memory engine (Claude Code architecture) ---
    transcript: TranscriptStore = field(default_factory=TranscriptStore)
    _query_engine: QueryEnginePort | None = field(default=None, repr=False)
    _execution_registry: ExecutionRegistry | None = field(default=None, repr=False)
    _port_runtime: PortRuntime | None = field(default=None, repr=False)
    _tool_pool: ToolPool | None = field(default=None, repr=False)
    _permission_ctx: ToolPermissionContext | None = field(default=None, repr=False)
    turn_count: int = 0
    max_turns: int = 20
    max_budget_tokens: int = 200000
    # --- Enhancement module ---
    enhancer: AgentEnhancer | None = field(default=None, repr=False)
    # --- Three-layer memory ---
    _memory_manager: Any = field(default=None, repr=False)
    _memory_consolidator: Any = field(default=None, repr=False)
    _memory_turn_counter: int = 0  # 累计轮次，用于 L1→L2 压缩判断
    # --- Self-Improvement engine (experience library) ---
    self_improvement: Any = field(default=None, repr=False)
    # --- Growth Tracker (养成量化) ---
    growth_tracker: Any = field(default=None, repr=False)
    # --- Execution Analyzer (自动分析) ---
    _execution_analyzer: ExecutionAnalyzer | None = field(default=None, repr=False)
    # --- Role Growth Path (角色成长路径) ---
    growth_path: RoleGrowthPath | None = field(default=None, repr=False)
    # --- Skill System (技能绑定) ---
    bound_prompt_packs: list[str] = field(default_factory=list)
    _active_skill_ids: list[str] = field(default_factory=list, repr=False)
    _chat_start_time: float = field(default=0.0, repr=False)
    # --- Execution Plans (real-time task decomposition) ---
    execution_plans: list[ExecutionPlan] = field(default_factory=list)
    _current_plan: ExecutionPlan | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last_save_time: float = field(default_factory=time.time, repr=False)  # For auto-save
    # --- Context compression state ---
    _previous_compression_summary: str = ""  # Iterative summary from previous compression
    _compression_cooldown: float = 0.0  # Cooldown until next LLM summarization attempt
    # --- Sub-Agent delegation (Hermes-style depth tracking & parallel execution) ---
    _delegate_depth: int = field(default=0, repr=False)  # 0 = top-level agent
    _max_delegate_depth: int = field(default=5, repr=False)  # configurable max depth
    _active_children: list[tuple[str, Any]] = field(default_factory=list, repr=False)  # List of (agent_id, Agent) tuples
    # ── Evolution goals: measurable targets for self-improvement ──
    # Each: {"id": "...", "description": "PPTX quality reaches professional level",
    #        "target_score": 90, "current_score": 0, "reference": "optional reference text or URL"}
    evolution_goals: list[dict] = field(default_factory=list)
    _active_children_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)  # Thread-safe access
    _cancellation_event: threading.Event = field(default_factory=threading.Event, repr=False)  # Signal children to stop
    # --- System prompt caching (avoid unnecessary rebuilds) ---
    _cached_static_prompt: str = field(default="", repr=False)
    _static_prompt_hash: str = field(default="", repr=False)  # Hash of inputs that affect static prompt
    _cached_git_context: str = field(default="", repr=False)
    _git_context_ts: float = field(default=0.0, repr=False)  # Timestamp of last git context fetch
    _GIT_CONTEXT_COOLDOWN: float = field(default=60.0, repr=False)  # seconds between git context refreshes
    _dynamic_context_tag: str = field(default="__DYNAMIC_CONTEXT__", repr=False, init=False)
    # --- Per-task LLM routing: while a task is executing, its provider/model
    #     (if set) override the agent's default for any LLM call. ---
    _current_task: AgentTask | None = field(default=None, repr=False)
    # --- Self-growth scheduling ---
    _last_growth_tick: float = field(default=0.0, repr=False)
    # --- Credential vault: runtime-only, never serialized or sent to LLM ---
    # Maps placeholder key (e.g. "CRED_abc123") → real credential value.
    # Used by request_web_login to keep passwords out of LLM context.
    _credential_vault: dict = field(default_factory=dict, repr=False)
    # --- LoginGuard: transparent login-wall handler (lazy-init) ---
    _login_guard: Any = field(default=None, repr=False)
    # --- Streaming buffer (2026-04-28): agent.chat() pushes each text_delta
    # into this buffer so external readers (project chat polling endpoint)
    # can show "typing" content in real time without an SSE connection.
    # Cleared at chat() entry; finalised + cleared at chat() exit.
    _streaming_buffer: str = field(default="", repr=False)

    # ---- per-context message routing ----

    def __post_init__(self):
        """Wire `self.messages` to the solo-context bucket so that legacy
        code paths (which all read/write `self.messages` directly) target
        the right per-context list. The first time an agent is created,
        `self.messages` is whatever the constructor / from_persist_dict
        passed in (could be the legacy single list — that becomes the
        "solo" context).

        from_persist_dict may overwrite `_messages_by_context` AFTER
        construction with the new multi-context dict; in that case it
        must call `_switch_context("solo")` (or whatever default) again
        to rebind `self.messages`.
        """
        # If _messages_by_context still has the default singleton {"solo": []}
        # but `messages` field was passed non-empty (legacy path), seed solo
        # bucket with that legacy list so we don't lose history.
        if (self._messages_by_context.get("solo") is None
                or self._messages_by_context.get("solo") == []):
            self._messages_by_context["solo"] = self.messages
        # Bind self.messages to the active context's list (same object,
        # so existing append() calls write through).
        self.messages = self._messages_by_context.setdefault(
            self._active_context_id, [])

    def _switch_context(self, context_id: str) -> None:
        """Switch the active conversation context.

        After this call:
          * ``self._active_context_id`` == context_id
          * ``self.messages`` is the same object as
            ``self._messages_by_context[context_id]`` — so all
            ``self.messages.append(...)`` / trim calls write to the
            right bucket.

        Idempotent. Lazy-creates a new bucket on first use of a context.
        Caller must hold ``self._lock`` (chat() does).
        """
        cid = (context_id or "solo").strip() or "solo"
        if cid == self._active_context_id and self.messages is self._messages_by_context.get(cid):
            return
        if cid not in self._messages_by_context:
            self._messages_by_context[cid] = []
        self._active_context_id = cid
        self.messages = self._messages_by_context[cid]

    def __setattr__(self, name, value):
        """Keep the per-context message dict in sync when callers
        reassign ``self.messages = new_list`` (used by trim/compress/
        recap helpers across agent.py and agent_llm.py). Without this
        hook those reassignments would orphan the dict's bucket pointer
        and the next context switch would lose the new list.

        Only the literal name ``messages`` is special-cased; everything
        else passes through unchanged."""
        super().__setattr__(name, value)
        if (name == "messages"
                and hasattr(self, "_messages_by_context")
                and hasattr(self, "_active_context_id")
                and isinstance(value, list)):
            self._messages_by_context[self._active_context_id] = value

    # ---- persistence serialisation ----

    def to_persist_dict(self) -> dict:
        """Serialise agent config + memory for disk persistence."""
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "model": self.model,
            "provider": self.provider,
            "learning_provider": self.learning_provider,
            "learning_model": self.learning_model,
            "multimodal_provider": self.multimodal_provider,
            "multimodal_model": self.multimodal_model,
            "multimodal_supports_tools": self.multimodal_supports_tools,
            "coding_provider": self.coding_provider,
            "coding_model": self.coding_model,
            "extra_llms": list(self.extra_llms),
            "auto_route": dict(self.auto_route or {}),
            "working_dir": self.working_dir,
            "agent_phase": self.agent_phase.value,
            "system_prompt": self.system_prompt,
            "profile": self.profile.to_dict(),
            "node_id": self.node_id,
            "shared_workspace": self.shared_workspace,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "context_type": self.context_type,
            "parent_id": self.parent_id,
            "priority_level": self.priority_level,
            "role_title": self.role_title,
            "department": self.department,
            "owner_id": self.owner_id,
            "authorized_workspaces": self.authorized_workspaces,
            "soul_md": self.soul_md,
            "robot_avatar": self.robot_avatar,
            "channel_ids": self.channel_ids,
            "granted_skills": list(self.granted_skills),
            # Preprocessor settings (per-agent opt-in for small local LLM)
            "preprocessor_model": self.preprocessor_model,
            "preprocessor_modes": list(self.preprocessor_modes or []),
            "preprocessor_endpoint": self.preprocessor_endpoint,
            "preprocessor_fallback": list(self.preprocessor_fallback or []),
            "created_at": self.created_at,
            # --- src integration: persist memory ---
            "session_id": self.session_id,
            # Per-context messages: each surface (solo / project / meeting)
            # keeps its own bounded history. Limit each bucket to last 200
            # messages so an agent in many projects doesn't blow up disk.
            # Empty buckets are dropped on save to keep the file tidy.
            "messages_by_context": {
                cid: msgs[-200:]
                for cid, msgs in (self._messages_by_context or {}).items()
                if msgs
            },
            # Legacy `messages` field kept for back-compat readers — emits
            # the SOLO bucket only, since that's the closest match to what
            # the legacy single-list field meant. New readers should prefer
            # messages_by_context above.
            "messages": (self._messages_by_context.get("solo") or [])[-200:],
            # Chat UI events (user/assistant bubbles, tool calls, approvals).
            # The UI replays these on load so the conversation history is
            # preserved across app restarts. Keep last 500 to bound file size.
            "events": [e.to_dict() for e in self.events[-500:]],
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "cost_events": self.cost_tracker.events[-200:],
            "cost_total_units": self.cost_tracker.total_units,
            "history_events": [
                {"title": e.title, "detail": e.detail}
                for e in self.history_log.events[-100:]
            ],
            # --- src memory engine persistence ---
            "transcript_entries": list(self.transcript.replay()),
            "turn_count": self.turn_count,
            "max_turns": self.max_turns,
            "max_budget_tokens": self.max_budget_tokens,
            # --- Enhancement module persistence ---
            "enhancer": self.enhancer.to_dict() if self.enhancer else None,
            # --- Self-Improvement persistence ---
            "self_improvement": self.self_improvement.to_dict() if self.self_improvement else None,
            # --- Skill system persistence ---
            "bound_prompt_packs": self.bound_prompt_packs,
            # --- Role Growth Path persistence ---
            "growth_path": self.growth_path.to_dict() if self.growth_path else None,
            # --- Execution analyzer persistence ---
            "execution_analyzer": self._execution_analyzer.to_dict() if self._execution_analyzer else None,
            # --- Execution plans persistence (keep last 20) ---
            "execution_plans": [p.to_dict() for p in self.execution_plans[-20:]],
            "evolution_goals": self.evolution_goals,
        }

    @staticmethod
    def from_persist_dict(d: dict) -> "Agent":
        """Restore agent from persisted dict (including memory)."""
        profile = AgentProfile.from_dict(d.get("profile", {}))
        # Restore cost tracker
        ct = CostTracker()
        ct.total_units = d.get("cost_total_units", 0)
        ct.events = d.get("cost_events", [])
        # Restore history log
        hl = HistoryLog()
        for he in d.get("history_events", []):
            hl.add(he.get("title", ""), he.get("detail", ""))
        agent = Agent(
            id=d.get("id", uuid.uuid4().hex[:12]),
            name=d.get("name", "Agent"),
            role=d.get("role", "general"),
            model=d.get("model", ""),
            provider=d.get("provider", ""),
            learning_provider=d.get("learning_provider", "") or "",
            learning_model=d.get("learning_model", "") or "",
            multimodal_provider=d.get("multimodal_provider", "") or "",
            multimodal_model=d.get("multimodal_model", "") or "",
            multimodal_supports_tools=bool(d.get("multimodal_supports_tools", False)),
            coding_provider=d.get("coding_provider", "") or "",
            coding_model=d.get("coding_model", "") or "",
            extra_llms=list(d.get("extra_llms", []) or []),
            auto_route=dict(d.get("auto_route", {}) or {}),
            working_dir=d.get("working_dir", ""),
            agent_phase=AgentPhase(d.get("agent_phase", "idle")),
            system_prompt=d.get("system_prompt", ""),
            profile=profile,
            node_id=d.get("node_id", "local"),
            shared_workspace=d.get("shared_workspace", ""),
            project_id=d.get("project_id", ""),
            project_name=d.get("project_name", ""),
            context_type=d.get("context_type", "solo"),
            parent_id=d.get("parent_id", ""),
            priority_level=d.get("priority_level", 3),
            role_title=d.get("role_title", ""),
            department=d.get("department", "") or "",
            owner_id=d.get("owner_id", "") or "",
            authorized_workspaces=d.get("authorized_workspaces", []),
            soul_md=d.get("soul_md", ""),
            robot_avatar=d.get("robot_avatar", ""),
            channel_ids=d.get("channel_ids", []),
            granted_skills=list(d.get("granted_skills", []) or []),
            preprocessor_model=str(d.get("preprocessor_model", "") or ""),
            preprocessor_modes=list(d.get("preprocessor_modes", []) or []),
            preprocessor_endpoint=str(d.get("preprocessor_endpoint", "") or ""),
            preprocessor_fallback=list(d.get("preprocessor_fallback", []) or []),
            evolution_goals=list(d.get("evolution_goals", []) or []),
            created_at=d.get("created_at", time.time()),
            # --- src integration: restore memory ---
            cost_tracker=ct,
            history_log=hl,
            session_id=d.get("session_id", uuid.uuid4().hex),
            # `messages` field still passed for back-compat; __post_init__
            # will route it into the solo bucket if messages_by_context
            # isn't set below.
            messages=list(d.get("messages", []) or []),
            total_input_tokens=d.get("total_input_tokens", 0),
            total_output_tokens=d.get("total_output_tokens", 0),
            # --- src memory engine restore ---
            turn_count=d.get("turn_count", 0),
            max_turns=d.get("max_turns", 20),
            max_budget_tokens=d.get("max_budget_tokens", 200000),
        )
        # Restore per-context message buckets. Prefer the new
        # `messages_by_context` field; fall back to legacy single-list
        # `messages` (already routed into solo bucket by __post_init__).
        mbc = d.get("messages_by_context")
        if isinstance(mbc, dict) and mbc:
            cleaned: dict[str, list[dict]] = {}
            for cid, msgs in mbc.items():
                if not isinstance(msgs, list):
                    continue
                cleaned[str(cid)] = list(msgs)
            if cleaned:
                if "solo" not in cleaned:
                    cleaned["solo"] = []
                agent._messages_by_context = cleaned
                # Re-bind self.messages to the (possibly new) solo bucket
                # since __post_init__ aliased it before we overwrote the dict.
                agent._switch_context("solo")
        # Restore transcript entries
        for entry in d.get("transcript_entries", []):
            agent.transcript.append(entry)
        # Restore chat events so the UI can show history after a restart
        for ed in d.get("events", []):
            try:
                agent.events.append(AgentEvent(
                    timestamp=ed.get("timestamp", 0.0),
                    kind=ed.get("kind", "message"),
                    data=ed.get("data", {}) or {},
                ))
            except Exception as e:
                logger.debug("Failed to restore event: %s", e)
        # Restore enhancement module
        if d.get("enhancer"):
            agent.enhancer = AgentEnhancer.from_dict(d["enhancer"])
        # Note: `active_thinking` field removed — Think button is now an
        # on-demand self-summary action, not a persistent engine. Any
        # legacy active_thinking payload in the JSON is silently ignored.
        # Restore self-improvement engine
        if d.get("self_improvement"):
            try:
                from .experience_library import SelfImprovementEngine
                agent.self_improvement = SelfImprovementEngine.from_dict(
                    d["self_improvement"], agent=agent)
            except Exception as e:
                logger.debug("Failed to restore self_improvement: %s", e)
        # Restore skill bindings
        agent.bound_prompt_packs = d.get("bound_prompt_packs", d.get("bound_skill_ids", []))
        # Restore role growth path
        if d.get("growth_path"):
            agent.growth_path = RoleGrowthPath.from_dict(d["growth_path"])
        # Restore execution analyzer
        if d.get("execution_analyzer"):
            agent._execution_analyzer = ExecutionAnalyzer.from_dict(d["execution_analyzer"])
        # Restore execution plans
        for pd in d.get("execution_plans", []):
            agent.execution_plans.append(ExecutionPlan.from_dict(pd))
        return agent

    # ---- API serialisation ----

    def to_dict(self, include_messages: bool = False,
                include_events: bool = False) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            # Return the agent's OWN model/provider — no fallback to the
            # global config. Empty strings signal "not configured" so the
            # UI can disable the chat input and prompt for selection.
            "model": self.model or "",
            "provider": self.provider or "",
            # --- Additional LLM slots for 方案甲(learning+multimodal) + 乙(extra_llms) ---
            "learning_provider": self.learning_provider,
            "learning_model": self.learning_model,
            "multimodal_provider": self.multimodal_provider,
            "multimodal_model": self.multimodal_model,
            "multimodal_supports_tools": self.multimodal_supports_tools,
            "coding_provider": self.coding_provider,
            "coding_model": self.coding_model,
            "extra_llms": list(self.extra_llms),
            "auto_route": dict(self.auto_route or {}),
            "working_dir": self.working_dir or str(self._effective_working_dir()),
            "status": self.status.value,
            "node_id": self.node_id,
            "parent_id": self.parent_id,
            "priority_level": self.priority_level,
            "role_title": self.role_title,
            "department": self.department,
            "owner_id": self.owner_id,
            "robot_avatar": self.robot_avatar,
            "created_at": self.created_at,
            "message_count": len(self.messages),
            "event_count": len(self.events),
            "task_count": len(self.tasks),
            "tasks_summary": {
                "todo": sum(1 for t in self.tasks if t.status == TaskStatus.TODO),
                "in_progress": sum(1 for t in self.tasks if t.status == TaskStatus.IN_PROGRESS),
                "done": sum(1 for t in self.tasks if t.status == TaskStatus.DONE),
                "blocked": sum(1 for t in self.tasks if t.status == TaskStatus.BLOCKED),
            },
            "agent_class": self.profile.agent_class,
            "memory_mode": self.profile.memory_mode,
            "profile": self.profile.to_dict(),
            "channel_ids": self.channel_ids,
            # --- src integration: cost & usage ---
            "cost_summary": {
                "total_units": self.cost_tracker.total_units,
                "input_tokens": self.total_input_tokens,
                "output_tokens": self.total_output_tokens,
            },
            "session_id": self.session_id,
            # --- src memory engine stats ---
            "engine": {
                "turn_count": self.turn_count,
                "max_turns": self.max_turns,
                "max_budget_tokens": self.max_budget_tokens,
                "transcript_size": len(self.transcript.entries),
                "has_query_engine": self._query_engine is not None,
                "has_execution_registry": self._execution_registry is not None,
                "has_tool_pool": self._tool_pool is not None,
            },
            # --- Enhancement module ---
            "enhancement": self.enhancer.get_stats() if self.enhancer else None,
            # --- Self-Improvement ---
            "self_improvement": self.self_improvement.get_stats() if self.self_improvement else None,
            # --- Execution plans (current + recent) ---
            "current_plan": self._current_plan.to_dict() if self._current_plan else None,
            "plan_count": len(self.execution_plans),
            # --- Skill System (技能绑定) ---
            # granted_skills: authoritative skill grants from the skill
            #   registry (runtime skills like take_screenshot). This is
            #   what the Capability panel's SKILLS column should reflect.
            # bound_prompt_packs: prompt-enhancer packs — a separate
            #   class of capability, surfaced in the detail dialog.
            "granted_skills": list(self.granted_skills),
            "bound_prompt_packs": self.bound_prompt_packs,
            "active_skill_count": len(self._active_skill_ids),
            # --- Preprocessor (small local LLM) ---
            "preprocessor_model": self.preprocessor_model,
            "preprocessor_modes": list(self.preprocessor_modes or []),
            "preprocessor_endpoint": self.preprocessor_endpoint,
            "preprocessor_fallback": list(self.preprocessor_fallback or []),
            # --- Role Growth Path (角色成长路径) ---
            "growth_path": self.growth_path.get_summary() if self.growth_path else None,
            # --- Execution Analyzer (最近分析) ---
            "recent_analyses": (
                [a.to_dict() for a in self._execution_analyzer.get_recent_analyses(5)]
                if self._execution_analyzer else []
            ),
            # --- Workspace ---
            "shared_workspace": self.shared_workspace,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "context_type": self.context_type,
            # --- Evolution goals ---
            "evolution_goals": self.evolution_goals,
        }
        # --- Authoritative capability view (live, not cached profile) ---
        #
        # These three fields are what the portal UI reads to render the
        # Capability panel (Skills / MCPs / Tools). They MUST reflect live
        # state, not whatever `self.profile.mcp_servers` happens to hold
        # from the last workspace layout regeneration — otherwise the
        # count stays at 0 until the agent next boots, which is exactly
        # the bug we're fixing.
        #
        # Invariant: the agent serializer consults the authoritative
        # source at serialization time.
        try:
            from .mcp.manager import get_mcp_manager as _gmm
            _mgr = _gmm()
            if _mgr is not None:
                _live_mcps = _mgr.get_agent_effective_mcps(
                    getattr(self, "node_id", "local") or "local", self.id
                ) or []
                d["mcp_servers"] = [
                    (m.to_dict() if hasattr(m, "to_dict") else m)
                    for m in _live_mcps
                ]
            else:
                d["mcp_servers"] = []
        except Exception as e:
            logger.warning("Failed to get mcp_servers: %s", e)
            d["mcp_servers"] = []
        # Tools (capability view): tools the agent has GAINED from its
        # MCP bindings. We deliberately do NOT count the ~180 built-in
        # tools (read/write/bash/git/web_fetch/…) here — every agent has
        # those, so including them would make the Capability panel
        # useless for telling agents apart.
        #
        # Authoritative source: ToolManifestCache. For each bound MCP,
        # look up its discovered tool list and emit one entry per tool.
        # If an MCP hasn't been probed yet, it contributes 0 here — the
        # count will fill in as the background preloader finishes.
        try:
            _mcp_tools: list[dict] = []
            if _mgr is not None and d.get("mcp_servers"):
                for _m in d["mcp_servers"]:
                    _mid = _m.get("id") if isinstance(_m, dict) else getattr(_m, "id", "")
                    if not _mid:
                        continue
                    try:
                        _entry = _mgr.get_tool_manifest(_mid)
                    except Exception as e:
                        logger.debug("Failed to get tool manifest for %s: %s", _mid, e)
                        _entry = None
                    if _entry is None or not _entry.tools:
                        continue
                    for _t in _entry.tools:
                        _tname = _t.get("name") if isinstance(_t, dict) else None
                        if not _tname:
                            continue
                        _mcp_tools.append({
                            "name": _tname,
                            "mcp_id": _mid,
                            "source_hint": f"mcp:{_mid}",
                        })
            d["tools"] = _mcp_tools
        except Exception as e:
            logger.warning("Failed to build mcp_tools list: %s", e)
            d["tools"] = []
        # Full runtime tool pool size (builtin + mcp + skill) — exposed
        # separately so the UI / debug views can still see it without
        # confusing it with the Capability panel's "MCP tools" count.
        try:
            if self._tool_pool is not None:
                d["tool_pool_size"] = len(self._tool_pool.tools)
            else:
                d["tool_pool_size"] = None  # not yet assembled
        except Exception as e:
            logger.debug("Failed to get tool_pool_size: %s", e)
            d["tool_pool_size"] = None
        if include_messages:
            d["messages"] = self.messages
        if include_events:
            d["events"] = [e.to_dict() for e in self.events[-200:]]
        return d

    # ---- Role Growth Path helpers ----

    def ensure_growth_path(self) -> RoleGrowthPath | None:
        """Ensure agent has a growth path. Auto-creates from template if role matches."""
        if self.growth_path:
            return self.growth_path
        import copy
        template = ROLE_GROWTH_PATHS.get(self.role)
        if template:
            self.growth_path = copy.deepcopy(template)
            logger.info("Agent %s: auto-created growth path for role=%s", self.id[:8], self.role)
        return self.growth_path

    def get_next_learning_objective(self):
        """Get the next uncompleted learning objective from current growth stage."""
        gp = self.ensure_growth_path()
        if not gp:
            return None
        objs = gp.get_next_objectives(limit=1)
        return objs[0] if objs else None

    # ---- src integration: session persistence ----

    def save_memory(self, directory: Path | None = None) -> str:
        """Persist conversation memory using src.SessionStore."""
        stored = StoredSession(
            session_id=self.session_id,
            messages=tuple(
                json.dumps(m, ensure_ascii=False) for m in self.messages[-100:]
            ),
            input_tokens=self.total_input_tokens,
            output_tokens=self.total_output_tokens,
        )
        path = save_session(stored, directory)
        self.history_log.add("session_saved", str(path))
        return str(path)

    def load_memory(self, directory: Path | None = None) -> bool:
        """Restore conversation memory from src.SessionStore."""
        try:
            stored = load_session(self.session_id, directory)
            restored = []
            for raw in stored.messages:
                try:
                    restored.append(json.loads(raw))
                except (json.JSONDecodeError, TypeError):
                    restored.append({"role": "user", "content": str(raw)})
            self.messages = restored
            self.total_input_tokens = stored.input_tokens
            self.total_output_tokens = stored.output_tokens
            self.history_log.add("session_loaded",
                                 f"msgs={len(restored)} tokens_in={stored.input_tokens}")
            return True
        except Exception as e:
            logger.warning("Failed to load memory: %s", e)
            return False

    def _auto_save_check(self):
        """Auto-save conversation periodically (every 60 s during active chat).

        Flushes both the session memory AND the agent events/messages to
        disk so chat history survives a server restart.
        """
        # Skip during scheduled execution — messages are temporarily isolated
        # and saving here would persist the wrong (scheduled) context.
        if getattr(self, '_scheduled_context', False):
            return
        try:
            current_time = time.time()
            elapsed = current_time - self._last_save_time
            if elapsed > 60:  # flush events at least once per minute
                self.save_memory()
                self._last_save_time = current_time
                # Also flush agent events to disk via the hub so that
                # chat history (events) survives a crash/restart.
                try:
                    from .hub import get_hub as _get_hub
                    _hub = _get_hub()
                    if _hub is not None:
                        _hub._save_agent_workspace(self)
                except Exception:
                    pass
        except Exception as e:
            logger.debug("Auto-save failed: %s", e)

    def get_memory_usage_stats(self) -> dict:
        """
        返回 agent 的记忆使用比例统计。
        - last_mem_chars: 最近一次注入的记忆字符数
        - last_budget: 动态上下文预算上限（字符）
        - last_total_chars: 最近一次动态上下文实际总字符数
        - last_ratio: mem_chars / budget （0~1）
        - last_in_dynamic_ratio: mem_chars / total_chars （0~1，记忆在动态上下文里占比）
        - ema_ratio: 近期 ratio 的指数移动平均
        - samples: 累计样本数
        """
        stats = getattr(self, "_memory_usage_stats", None) or {}
        mem_chars = stats.get("last_mem_chars", 0)
        budget = stats.get("last_budget", 0)
        total = stats.get("last_total_chars", 0)
        in_dyn = (mem_chars / total) if total > 0 else 0.0
        hits_counts = getattr(self, "_memory_hit_counts", None) or {"hits": 0, "misses": 0}
        h = hits_counts.get("hits", 0)
        m = hits_counts.get("misses", 0)
        hit_rate = h / (h + m) if (h + m) > 0 else 0.0
        return {
            "last_mem_chars": mem_chars,
            "last_total_chars": total,
            "last_budget": budget,
            "last_ratio": stats.get("last_ratio", 0.0),
            "last_in_dynamic_ratio": in_dyn,
            "ema_ratio": stats.get("ema_ratio", 0.0),
            "samples": stats.get("samples", 0),
            "last_query_ts": stats.get("last_query_ts", 0.0),
            "hits": h,
            "misses": m,
            "hit_rate": hit_rate,
        }

    def get_token_stats(self) -> dict:
        """累计 LLM token 使用（由 llm._log_token_usage 写入）。"""
        stats = getattr(self, "_token_stats", None) or {
            "in": 0, "out": 0, "calls": 0,
        }
        return {
            "prompt_tokens": stats.get("in", 0),
            "completion_tokens": stats.get("out", 0),
            "total_tokens": stats.get("in", 0) + stats.get("out", 0),
            "calls": stats.get("calls", 0),
        }

    def get_cost_summary(self) -> dict:
        """Return cost/usage summary from CostTracker."""
        return {
            "total_units": self.cost_tracker.total_units,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "event_count": len(self.cost_tracker.events),
            "recent_events": self.cost_tracker.events[-20:],
        }

    def get_history_markdown(self) -> str:
        """Return activity history as markdown from HistoryLog."""
        return self.history_log.as_markdown()

    def get_workspace_context(self) -> str:
        """Build workspace context using src.PortContext."""
        try:
            wd = self._effective_working_dir()
            ctx = build_port_context(wd)
            return render_context(ctx)
        except Exception as e:
            logger.debug("Failed to build workspace context: %s", e)
            return ""

    def get_tool_surface(self, query: str = "", limit: int = 20) -> str:
        """Query the mirrored tool surface from src.tools."""
        return render_tool_index(limit=limit, query=query or None)

    # ---- src memory engine: lazy initialization (Claude Code architecture) ----

    def _ensure_permission_ctx(self) -> ToolPermissionContext:
        """Build permission context from agent profile."""
        if self._permission_ctx is None:
            deny_names = list(self.profile.denied_tools) if self.profile.denied_tools else []
            self._permission_ctx = ToolPermissionContext.from_iterables(
                deny_names=deny_names or None,
                deny_prefixes=None,
            )
        return self._permission_ctx

    def _ensure_execution_registry(self) -> ExecutionRegistry:
        """Lazy-init the ExecutionRegistry (mirrored commands + tools)."""
        if self._execution_registry is None:
            self._execution_registry = build_execution_registry()
            self.history_log.add("engine_init", "ExecutionRegistry built")
        return self._execution_registry

    def _ensure_tool_pool(self) -> ToolPool:
        """Lazy-init the ToolPool with permission filtering."""
        if self._tool_pool is None:
            perm = self._ensure_permission_ctx()
            self._tool_pool = assemble_tool_pool(
                simple_mode=False,
                include_mcp=True,
                permission_context=perm,
            )
            self.history_log.add("engine_init",
                                 f"ToolPool assembled: {len(self._tool_pool.tools)} tools")
        return self._tool_pool

    def _ensure_port_runtime(self) -> PortRuntime:
        """Lazy-init the PortRuntime for prompt routing."""
        if self._port_runtime is None:
            self._port_runtime = PortRuntime()
            self.history_log.add("engine_init", "PortRuntime initialized")
        return self._port_runtime

    def _ensure_query_engine(self) -> QueryEnginePort:
        """Lazy-init QueryEnginePort for session/turn management."""
        if self._query_engine is None:
            config = QueryEngineConfig(
                max_turns=self.max_turns,
                max_budget_tokens=self.max_budget_tokens,
                compact_after_turns=max(12, self.profile.max_context_messages // 3),
            )
            self._query_engine = QueryEnginePort.from_workspace()
            self._query_engine.config = config
            self._query_engine.session_id = self.session_id
            self.history_log.add("engine_init",
                                 f"QueryEnginePort created session={self.session_id[:8]}")
        return self._query_engine

    def route_prompt(self, prompt: str, limit: int = 5) -> list[RoutedMatch]:
        """Route a prompt through PortRuntime to find matching tools/commands."""
        runtime = self._ensure_port_runtime()
        matches = runtime.route_prompt(prompt, limit=limit)
        if matches:
            self.history_log.add("routing",
                                 f"matched {len(matches)} items: "
                                 + ", ".join(f"{m.kind}:{m.name}" for m in matches[:3]))
        return matches

    def submit_to_engine(self, prompt: str) -> TurnResult:
        """Submit a message through the QueryEngine for turn management."""
        engine = self._ensure_query_engine()
        matches = self.route_prompt(prompt)
        matched_commands = tuple(m.name for m in matches if m.kind == "command")
        matched_tools = tuple(m.name for m in matches if m.kind == "tool")

        # Check permission denials
        denied = []
        perm = self._ensure_permission_ctx()
        for m in matches:
            if m.kind == "tool" and perm.blocks(m.name):
                denied.append(PermissionDenial(tool_name=m.name,
                                                reason="Blocked by agent permission"))

        result = engine.submit_message(
            prompt,
            matched_commands=matched_commands,
            matched_tools=matched_tools,
            denied_tools=tuple(denied),
        )
        self.turn_count += 1
        # Track in transcript
        self.transcript.append(prompt)
        # Track usage
        self.total_input_tokens += result.usage.input_tokens
        self.total_output_tokens += result.usage.output_tokens
        apply_cost_hook(self.cost_tracker,
                        f"turn:{self.turn_count}",
                        result.usage.input_tokens + result.usage.output_tokens)
        self.history_log.add("turn_complete",
                             f"turn={self.turn_count} in={result.usage.input_tokens} "
                             f"out={result.usage.output_tokens} stop={result.stop_reason}")
        return result

    def execute_src_tool(self, tool_name: str, payload: str = "") -> ToolExecution:
        """Execute a tool through the src ExecutionRegistry."""
        registry = self._ensure_execution_registry()
        mirrored = registry.tool(tool_name)
        if mirrored:
            result_msg = mirrored.execute(payload)
            self.history_log.add("src_tool_exec", f"{tool_name}: {result_msg[:100]}")
            return ToolExecution(name=tool_name, source_hint=mirrored.source_hint,
                                payload=payload, handled=True, message=result_msg)
        # Fall back to direct src execute
        result = src_execute_tool(tool_name, payload)
        self.history_log.add("src_tool_exec",
                             f"{tool_name}: handled={result.handled} {result.message[:100]}")
        return result

    def execute_src_command(self, command_name: str, prompt: str = "") -> CommandExecution:
        """Execute a command through the src ExecutionRegistry."""
        registry = self._ensure_execution_registry()
        mirrored = registry.command(command_name)
        if mirrored:
            result_msg = mirrored.execute(prompt)
            self.history_log.add("src_cmd_exec", f"{command_name}: {result_msg[:100]}")
            return CommandExecution(name=command_name, source_hint=mirrored.source_hint,
                                   prompt=prompt, handled=True, message=result_msg)
        result = src_execute_command(command_name, prompt)
        self.history_log.add("src_cmd_exec",
                             f"{command_name}: handled={result.handled} {result.message[:100]}")
        return result

    def run_turn_loop(self, prompt: str, max_turns: int = 3) -> list[TurnResult]:
        """Execute multiple turns through PortRuntime (Claude Code style turn loop)."""
        runtime = self._ensure_port_runtime()
        results = runtime.run_turn_loop(
            prompt, limit=5, max_turns=max_turns,
            structured_output=False,
        )
        for r in results:
            self.turn_count += 1
            self.total_input_tokens += r.usage.input_tokens
            self.total_output_tokens += r.usage.output_tokens
            self.transcript.append(r.prompt)
            apply_cost_hook(self.cost_tracker,
                            f"loop_turn:{self.turn_count}",
                            r.usage.input_tokens + r.usage.output_tokens)
        self.history_log.add("turn_loop",
                             f"turns={len(results)} total_turn_count={self.turn_count}")
        return results

    def compact_memory(self):
        """Compact transcript and query engine messages (prevent unbounded growth)."""
        keep = max(10, self.profile.max_context_messages // 3)
        self.transcript.compact(keep_last=keep)
        engine = self._ensure_query_engine()
        engine.compact_messages_if_needed()
        self.history_log.add("memory_compact", f"kept_last={keep}")

    def replay_transcript(self) -> tuple[str, ...]:
        """Replay all user messages from transcript store."""
        return self.transcript.replay()

    def persist_engine_session(self, directory: Path | None = None) -> str:
        """Persist the full query engine session to disk."""
        engine = self._ensure_query_engine()
        # Sync engine state with agent state
        engine.total_usage = UsageSummary(
            input_tokens=self.total_input_tokens,
            output_tokens=self.total_output_tokens,
        )
        path = engine.persist_session()
        self.history_log.add("engine_session_persisted", path)
        return path

    def restore_engine_session(self) -> bool:
        """Restore query engine session from disk."""
        try:
            self._query_engine = QueryEnginePort.from_saved_session(self.session_id)
            # Sync back to agent
            self.total_input_tokens = self._query_engine.total_usage.input_tokens
            self.total_output_tokens = self._query_engine.total_usage.output_tokens
            # Restore transcript
            for msg in self._query_engine.mutable_messages:
                if msg not in self.transcript.entries:
                    self.transcript.append(msg)
            self.history_log.add("engine_session_restored",
                                 f"msgs={len(self._query_engine.mutable_messages)}")
            return True
        except Exception as e:
            self.history_log.add("engine_session_restore_failed", str(e))
            return False

    def get_engine_summary(self) -> str:
        """Get a summary of the query engine state."""
        engine = self._ensure_query_engine()
        return engine.render_summary()

    def get_routed_tools_for_prompt(self, prompt: str) -> dict:
        """Route a prompt and return structured match info."""
        matches = self.route_prompt(prompt)
        return {
            "matches": [
                {"kind": m.kind, "name": m.name,
                 "source_hint": m.source_hint, "score": m.score}
                for m in matches
            ],
            "commands": [m.name for m in matches if m.kind == "command"],
            "tools": [m.name for m in matches if m.kind == "tool"],
            "total": len(matches),
        }

    def get_tool_pool_info(self) -> dict:
        """Return info about the assembled tool pool."""
        pool = self._ensure_tool_pool()
        return {
            "tool_count": len(pool.tools),
            "simple_mode": pool.simple_mode,
            "include_mcp": pool.include_mcp,
            "tools": [
                {"name": t.name, "responsibility": t.responsibility[:80],
                 "source_hint": t.source_hint}
                for t in pool.tools[:30]
            ],
        }

    def _log(self, kind: str, data: dict):
        # Skip event logging during scheduled task execution — scheduled
        # prompts/replies must NOT appear in the agent's chat UI.
        if getattr(self, '_scheduled_context', False):
            return
        self.events.append(AgentEvent(time.time(), kind, data))
        if len(self.events) > 2000:
            self.events = self.events[-1500:]

        # Forward to the ConversationTask observer. Best-effort — never
        # block the chat loop on an observer error. The observer checks
        # internally whether this agent has a task in progress before
        # mutating anything.
        if kind in ("message", "tool_call", "tool_result"):
            try:
                from .conversation_observer import on_agent_event
                on_agent_event(self.id, {
                    "timestamp": time.time(),
                    "kind": kind,
                    "data": data,
                })
            except Exception as e:   # noqa: BLE001
                # Never crash the chat loop on observer failure, but
                # make the failure visible (debug level — observer
                # hiccups are noise, not bugs we'd page on).
                logger.debug("conversation_observer forward failed: %s", e)

            # Mirror to the progress bus so a single SSE subscriber on
            # "agent:<id>" sees both these classic events AND the new
            # plan/step progress frames (coming in Block 1/2). Also
            # best-effort — never let a bus hiccup kill chat.
            try:
                from .progress_bus import mirror_agent_event
                mirror_agent_event(agent_id=self.id, kind=kind, data=data)
            except Exception as _mb:
                logger.debug("progress_bus mirror failed: %s", _mb)

    # ---- system prompt ----

    def _get_git_context(self) -> str:
        """Auto-inject git context: branch, status, recent commits."""
        import subprocess as _sp
        wd = str(self._effective_working_dir())
        parts = []
        try:
            # Check if it's a git repo
            _sp.run(["git", "rev-parse", "--git-dir"],
                    cwd=wd, capture_output=True, timeout=3, check=True)
        except Exception:
            return ""  # Not a git repo

        cmds = {
            "branch": ["git", "branch", "--show-current"],
            "status": ["git", "status", "--short", "--branch"],
            "log": ["git", "log", "--oneline", "-5", "--no-decorate"],
            "diff_stat": ["git", "diff", "--stat", "HEAD"],
        }
        for label, cmd in cmds.items():
            try:
                r = _sp.run(cmd, cwd=wd, capture_output=True, text=True, timeout=5)
                if r.returncode == 0 and r.stdout.strip():
                    parts.append(f"[git {label}]\n{r.stdout.strip()}")
            except Exception:
                pass
        if not parts:
            return ""
        return "<git_context>\n" + "\n\n".join(parts) + "\n</git_context>"

    def _get_skill_context(self) -> str:
        """Load SKILL.md files from project directory for knowledge injection."""
        wd = self._effective_working_dir()
        skill_content = []
        # Look for SKILL.md in working dir and common locations
        skill_paths = [
            wd / "SKILL.md",
            wd / ".claude" / "SKILL.md",
            wd / ".claw" / "SKILL.md",
            wd / "docs" / "SKILL.md",
        ]
        # Also scan for skill files in .claude/skills/ directory
        skills_dir = wd / ".claude" / "skills"
        if skills_dir.is_dir():
            for skill_file in skills_dir.rglob("SKILL.md"):
                if skill_file not in skill_paths:
                    skill_paths.append(skill_file)
        # Same for .claw/skills/
        claw_skills_dir = wd / ".claw" / "skills"
        if claw_skills_dir.is_dir():
            for skill_file in claw_skills_dir.rglob("SKILL.md"):
                if skill_file not in skill_paths:
                    skill_paths.append(skill_file)

        for sp in skill_paths:
            if sp.exists() and sp.is_file():
                try:
                    content = sp.read_text(encoding="utf-8", errors="replace")[:3000]
                    rel_path = str(sp.relative_to(wd)) if sp.is_relative_to(wd) else str(sp)
                    skill_content.append(
                        f'<skill file="{rel_path}">\n{content}\n</skill>'
                    )
                except (OSError, ValueError):
                    pass
        if not skill_content:
            return ""
        return "\n".join(skill_content)

    def _get_agent_home(self) -> Path:
        """Return this agent's home directory under the node data root.

        Layout: ~/.tudou_claw/workspaces/agents/{agent_id}/
        """
        from . import DEFAULT_DATA_DIR
        data_dir = os.environ.get("TUDOU_CLAW_DATA_DIR") or DEFAULT_DATA_DIR
        return Path(data_dir) / "workspaces" / "agents" / self.id

    def _get_agent_workspace(self) -> Path:
        """Return this agent's workspace folder (where MD files live)."""
        return self._get_agent_home() / "workspace"

    # Workspace files the agent ITSELF produces during chats. Drives
    # the "Recent workspace artifacts" injection in _build_dynamic_context
    # so agent sees its own output without doing redundant fs tool calls.
    _ARTIFACT_EXTS = (
        ".pptx", ".docx", ".xlsx", ".pdf",
        ".md", ".json", ".csv",
        ".py", ".sh", ".html",
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp",
        ".mp4", ".mov", ".mp3", ".wav",
        ".zip", ".tar", ".tar.gz", ".tgz",
    )
    # System / config files that are NOT agent deliverables.
    _ARTIFACT_SKIP_NAMES = {
        "Project.md", "Tasks.md", "Skills.md", "MCP.md",
        "Scheduled.md", "ActiveThinking.md",
    }
    _ARTIFACT_SKIP_DIRS = {
        "tool_outputs", "skills", "cache", "__pycache__",
        ".shadow", ".git", "attachments",
    }

    def _build_recent_artifacts_context(self, max_files: int = 15) -> str:
        """List recent deliverable-style files in agent workspace +
        shared workspace. Injected into dynamic context so agent knows
        what it has produced and won't re-search for its own files."""
        from datetime import datetime
        ws = self._get_agent_workspace()
        shared_ws = getattr(self, "shared_workspace", "") or ""
        roots: list[Path] = []
        if ws.is_dir():
            roots.append(ws)
        if shared_ws and Path(shared_ws).is_dir() and \
                str(Path(shared_ws).resolve()) != str(ws.resolve()):
            roots.append(Path(shared_ws))

        entries: list[tuple[float, Path, Path]] = []   # (mtime, abs, rel)
        for root in roots:
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                try:
                    rel = p.relative_to(root)
                except ValueError:
                    continue
                # Skip system dirs
                if any(part in self._ARTIFACT_SKIP_DIRS for part in rel.parts):
                    continue
                if p.name in self._ARTIFACT_SKIP_NAMES:
                    continue
                if p.name.startswith("."):
                    continue
                # Must be a recognizable deliverable extension
                if not any(p.name.lower().endswith(ext) for ext in self._ARTIFACT_EXTS):
                    continue
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                entries.append((mtime, p, rel))

        if not entries:
            return ""
        # Newest first, cap count
        entries.sort(key=lambda t: t[0], reverse=True)
        entries = entries[:max_files]

        lines = [
            "<workspace_artifacts>",
            "# 当前 workspace 里**已有的交付物**（按修改时间倒序）。",
            "# 用户要你处理/引用某文件时，**先查这个清单**，命中就直接用，",
            "# 不要再 `web_fetch` / `read_file` 重复查询同一资料。",
        ]
        for mtime, abs_path, rel_path in entries:
            ts = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")
            try:
                size_kb = abs_path.stat().st_size / 1024
            except OSError:
                size_kb = 0
            lines.append(
                f"- `{rel_path}` ({size_kb:.1f} KB, {ts})"
            )
        lines.append("</workspace_artifacts>")
        return "\n".join(lines)

    def _effective_working_dir(self) -> Path:
        """Return the agent's effective working directory.

        If ``self.working_dir`` is set, use it. Otherwise fall back to the
        agent's private workspace under ``~/.tudou_claw/workspaces/agents/``.

        CRITICAL: never fall back to ``os.getcwd()`` / ``Path.cwd()`` — that
        would leak runtime files into the server-process CWD, which is
        typically the code package directory (e.g.
        ``/Users/.../AIProjects/TudouClaw``). The code tree must never
        receive runtime artefacts.
        """
        if self.working_dir:
            try:
                return Path(self.working_dir)
            except Exception:
                pass
        try:
            return self._ensure_workspace_layout()
        except Exception:
            return self._get_agent_workspace()

    @staticmethod
    def get_shared_workspace_path(project_id: str) -> str:
        """Return the shared workspace path for a project.

        Layout: ~/.tudou_claw/workspaces/shared/{project_id}/
        """
        from . import DEFAULT_DATA_DIR
        data_dir = os.environ.get("TUDOU_CLAW_DATA_DIR") or DEFAULT_DATA_DIR
        return str(Path(data_dir) / "workspaces" / "shared" / project_id)

    def get_active_shared_workspace(self) -> str:
        """Resolve the shared-workspace path for the agent's CURRENT
        active context — the proper replacement for ``self.shared_workspace``.

        An agent participates in N projects + M meetings at once, so the
        old single-value field couldn't represent what was correct in
        any given turn. This method derives the right answer from
        ``_active_context_id``:

          ``solo``           → "" (no shared workspace; use working_dir)
          ``project:{pid}``  → the canonical
                               ``~/.tudou_claw/workspaces/shared/{pid}/``
                               (also matches ``project.working_directory``
                                when set on the project)
          ``meeting:{mid}``  → meeting's ``workspace_dir`` from the registry

        Looks up project / meeting via the active hub. Falls back to "" on
        any failure — callers are expected to handle empty string as
        "no shared workspace".
        """
        ctx = (getattr(self, "_active_context_id", "") or "solo")
        if not ctx or ctx == "solo":
            return ""
        try:
            import sys as _sys
            _llm_mod = _sys.modules.get(__package__ + ".llm") if __package__ else None
            hub = getattr(_llm_mod, "_active_hub", None) if _llm_mod else None
        except Exception:
            hub = None
        if ctx.startswith("project:"):
            pid = ctx.split(":", 1)[1]
            if hub is not None and hasattr(hub, "projects"):
                proj = hub.projects.get(pid)
                if proj is not None:
                    wd = getattr(proj, "working_directory", "") or ""
                    if wd:
                        return wd
            # Fallback to canonical convention even if hub/project lookup
            # missed — the directory is deterministic per project_id.
            return self.get_shared_workspace_path(pid)
        if ctx.startswith("meeting:"):
            mid = ctx.split(":", 1)[1]
            if hub is not None and hasattr(hub, "meeting_registry"):
                try:
                    mtg = hub.meeting_registry.get(mid)
                    if mtg is not None:
                        return getattr(mtg, "workspace_dir", "") or ""
                except Exception:
                    pass
        return ""

    def _ensure_workspace_layout(self) -> Path:
        """Create the standard agent directory layout and seed MD templates.

        Layout created:
            {agent_home}/workspace/{Scheduled.md, Tasks.md, Project.md}
            {agent_home}/workspace/shared -> {shared_workspace} (symlink if part of project)
            {agent_home}/{session, memory, logs}/
        Returns the workspace path.
        """
        home = self._get_agent_home()
        ws = home / "workspace"
        try:
            for sub in (ws, home / "session", home / "memory", home / "logs"):
                sub.mkdir(parents=True, exist_ok=True)
        except Exception:
            return ws

        # Create shared workspace symlink for the CURRENT active context
        # (project or meeting). When the agent is in solo, we deliberately
        # skip the symlink — solo work shouldn't have a "shared" link
        # pointing at a stale project from a prior turn.
        _active_sw = self.get_active_shared_workspace()
        if _active_sw:
            try:
                shared_link = ws / "shared"
                if shared_link.exists() or shared_link.is_symlink():
                    if shared_link.resolve() != Path(_active_sw).resolve():
                        shared_link.unlink()
                        shared_link.symlink_to(_active_sw)
                else:
                    shared_link.symlink_to(_active_sw)
            except Exception:
                pass  # Silently fail on symlink creation (may not be supported on all systems)

        # --- Scheduled.md ---
        sched = ws / "Scheduled.md"
        if not sched.exists():
            sched.write_text(
                "# Scheduled Tasks — Agent: " + (self.name or self.id) + "\n\n"
                "Recurring and scheduled tasks owned by this agent. The agent loads "
                "this file at the start of every conversation, uses it as the "
                "source of truth for what to run daily/weekly/monthly, and appends "
                "new entries here whenever the user asks to schedule something.\n\n"
                "## Format\n\n"
                "```\n"
                "### <short title>\n"
                "- id: <task_id>            # filled after task_update create\n"
                "- recurrence: daily|weekly|monthly|cron|once\n"
                "- spec: HH:MM  OR  DOW HH:MM  OR  D HH:MM  OR  cron expr\n"
                "- status: active|paused|done\n"
                "- last_run: <ISO timestamp or ->\n"
                "- next_run: <ISO timestamp or ->\n"
                "- description: |\n"
                "    what the agent should do when this fires.\n"
                "```\n\n"
                "## Active Schedules\n\n"
                "<!-- Agent appends entries below this line -->\n",
                encoding="utf-8")

        # --- Tasks.md ---
        tasks_md = ws / "Tasks.md"
        if not tasks_md.exists():
            tasks_md.write_text(
                "# Tasks — Agent: " + (self.name or self.id) + "\n\n"
                "Ad-hoc and one-off tasks. Use this for work items that are NOT "
                "recurring (recurring tasks go in Scheduled.md).\n\n"
                "## Format\n\n"
                "```\n"
                "- [ ] <task_id> — <title> (priority, deadline)\n"
                "    description / context\n"
                "```\n\n"
                "Mark done with `[x]` and optionally add `→ result: ...`.\n\n"
                "## Open\n\n"
                "<!-- Agent appends open tasks here -->\n\n"
                "## Done\n\n"
                "<!-- Agent moves completed tasks here -->\n",
                encoding="utf-8")

        # --- Project.md (seed once; user/agent curate over time) ---
        proj_md = ws / "Project.md"
        if not proj_md.exists():
            proj_md.write_text(
                "# Project — Agent: " + (self.name or self.id) + "\n\n"
                "Long-lived project context, goals, constraints, and decisions "
                "this agent is working on. Persists across conversations.\n\n"
                "## Role\n\n"
                f"- Role: {self.role}\n"
                f"- Expertise: {', '.join(self.profile.expertise) or '(not set)'}\n"
                f"- Skills: {', '.join(self.profile.skills) or '(not set)'}\n\n"
                "## Goals\n\n"
                "<!-- Summarize the user's longer-term objectives here -->\n\n"
                "## Constraints / Conventions\n\n"
                "<!-- Style, tech stack, deadlines, language, tone... -->\n\n"
                "## Key Decisions\n\n"
                "<!-- Notable decisions made so the agent can stay consistent -->\n",
                encoding="utf-8")

        # --- skills/ directory (for granted skill packages) ---
        skills_dir = ws / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        # --- granted_skills.md (renamed from Skills.md, 2026-04-28) ---
        # Slim ID+path manifest of skills granted to this agent — NOT the
        # full SKILL.md descriptions. Single source of truth for prompt
        # injection: `_get_scheduled_context` reads this one file; the
        # old `skills_registry` dynamic-context block (which re-rendered
        # every granted SKILL.md every turn — ~2K tokens) has been
        # removed in favour of on-demand reading.
        #
        # When the LLM needs a skill's usage, it does:
        #   read_file <path>/SKILL.md       (raw file)
        #   get_skill_guide(name=...)       (formatted)
        granted_md = ws / "granted_skills.md"
        try:
            _lines = [
                "# Granted Skills — Agent: " + (self.name or self.id),
                "",
                "本 agent 授权技能清单(自动维护,请勿手改)。需要某个技能的详细"
                "用法时,`read_file <path>/SKILL.md` 或 `get_skill_guide(name)`。",
                "",
            ]
            _granted_lines = []
            try:
                import sys as _sys
                _llm_mod = _sys.modules.get(__package__ + ".llm") if __package__ else None
                _hub = getattr(_llm_mod, "_active_hub", None) if _llm_mod else None
                if _hub is not None and getattr(_hub, "skill_registry", None) is not None:
                    # ── Reconcile: registry is the source of truth ──
                    # If a grant landed in the registry but never made it
                    # into agent.granted_skills (caller skipped that
                    # mirror, race during save, etc.), the md would be
                    # missing entries. Sync both directions so future
                    # writes are consistent.
                    _reg_ids = [
                        getattr(_inst, "id", "")
                        for _inst in _hub.skill_registry.list_for_agent(self.id)
                        if getattr(_inst, "id", "")
                    ]
                    if _reg_ids:
                        _gs = list(self.granted_skills or [])
                        _gs_set = set(_gs)
                        _added = [r for r in _reg_ids if r not in _gs_set]
                        if _added:
                            self.granted_skills = _gs + _added
                    for _inst in _hub.skill_registry.list_for_agent(self.id):
                        _m = _inst.manifest
                        _sid = getattr(_m, "id", "") or getattr(_m, "name", "") or "?"
                        _path = _inst.install_dir or ""
                        # One-line description only — agent reads
                        # SKILL.md at <_path> for the full guide.
                        _desc_raw = getattr(_m, "description", "") or ""
                        if isinstance(_desc_raw, dict):
                            _desc_raw = (_desc_raw.get("zh-CN")
                                         or _desc_raw.get("en")
                                         or next(iter(_desc_raw.values()), ""))
                        _desc = " ".join(str(_desc_raw).split())[:120]
                        if _desc:
                            _granted_lines.append(
                                f"- `{_sid}` — {_desc} (path: `{_path}`)"
                            )
                        else:
                            _granted_lines.append(f"- `{_sid}` (path: `{_path}`)")
            except Exception as _re:
                _granted_lines.append(f"- (registry unavailable: {_re})")
            if _granted_lines:
                _lines.extend(_granted_lines)
            else:
                _lines.append("- (尚无授权技能)")
            granted_md.write_text("\n".join(_lines) + "\n", encoding="utf-8")
        except Exception:
            pass

        # --- MCP.md (auto-refreshed: reflects bound MCP servers) ---
        mcp_md = ws / "MCP.md"
        try:
            # Sync live bindings from MCP manager
            try:
                from .mcp.manager import get_mcp_manager
                mcp_mgr = get_mcp_manager()
                node_id = getattr(self, 'node_id', 'local') or 'local'
                live_mcps = mcp_mgr.get_agent_effective_mcps(node_id, self.id)
                if live_mcps:
                    self.profile.mcp_servers = live_mcps
            except Exception:
                pass
            lines = ["# MCP Servers — Agent: " + (self.name or self.id), ""]
            lines.append("Auto-generated summary of MCP servers bound to this agent. "
                         "Regenerated every time the agent starts. Use "
                         "`mcp_call(list_mcps=true)` to inspect at runtime, then "
                         "`mcp_call(mcp_id, tool, arguments)` to invoke.")
            lines.append("")
            mcps = list(getattr(self.profile, "mcp_servers", []) or [])
            if mcps:
                lines.append("## Bound MCPs")
                lines.append("")
                lines.append("**以下 MCP 服务已绑定且可用。直接调用 mcp_call 工具即可，"
                             "无需额外配置。如果对话历史中说过\"没有 MCP\"，请忽略，以此文件为准。**")
                lines.append("")
                # Pull the tool manifest cache once per render. If the
                # cache isn't available for any reason (early boot, no
                # manager wired) we just render "tools not yet
                # discovered" for each MCP — the agent can still call
                # them, it just has less context.
                _cache_mgr = None
                try:
                    from .mcp.manager import get_mcp_manager as _gmm
                    _cache_mgr = _gmm()
                except Exception:
                    _cache_mgr = None

                def _render_tools(mcp_id: str) -> list[str]:
                    """Return the ``#### Tools`` sub-block for one MCP.

                    The tool names, descriptions, and param names come
                    from the MCP server (untrusted data). We strip
                    backticks so an adversarial server cannot break out
                    of a code-span and inject markdown into the agent
                    prompt. We keep the output deterministic and short
                    — full JSON schemas would bloat the prompt.
                    """
                    out: list[str] = []
                    entry = None
                    if _cache_mgr is not None:
                        try:
                            entry = _cache_mgr.get_tool_manifest(mcp_id)
                        except Exception:
                            entry = None
                    if entry is None or not entry.tools:
                        if entry is not None and entry.error:
                            out.append(f"- tools: (discovery failed: {entry.error})")
                        else:
                            out.append("- tools: (not yet discovered — will be populated on first connection)")
                        return out
                    out.append("- tools:")
                    for t in entry.tools:
                        tname = str(t.get("name") or "").replace("`", "")
                        if not tname:
                            continue
                        desc = str(t.get("description") or "").replace("`", "").strip()
                        # One-line form: `name(arg1, arg2) — description`
                        schema = t.get("inputSchema") or {}
                        props = schema.get("properties") if isinstance(schema, dict) else None
                        required = set(schema.get("required") or []) if isinstance(schema, dict) else set()
                        arglist = ""
                        if isinstance(props, dict):
                            parts = []
                            for pname, pspec in list(props.items())[:6]:
                                pname_clean = str(pname).replace("`", "")
                                if pname in required:
                                    parts.append(pname_clean)
                                else:
                                    parts.append(f"{pname_clean}?")
                            if len(props) > 6:
                                parts.append("...")
                            arglist = "(" + ", ".join(parts) + ")"
                        # Truncate description to keep prompts tight
                        desc_short = (desc[:120] + "…") if len(desc) > 120 else desc
                        suffix = f" — {desc_short}" if desc_short else ""
                        out.append(f"  - `{tname}{arglist}`{suffix}")
                    if entry.error:
                        out.append(f"- ⚠️ last refresh failed: {entry.error} "
                                   f"(showing previously-discovered tools)")
                    return out

                for m in mcps:
                    status = "enabled" if getattr(m, "enabled", True) else "disabled"
                    lines.append(f"### {getattr(m, 'name', '') or m.id}")
                    lines.append(f"- id: `{m.id}`")
                    lines.append(f"- transport: {getattr(m, 'transport', 'stdio')}")
                    lines.append(f"- status: {status}")
                    cmd = getattr(m, "command", "") or getattr(m, "url", "")
                    if cmd:
                        lines.append(f"- endpoint: `{cmd}`")
                    # Show configured env vars (keys only, no values for security)
                    env_vars = getattr(m, 'env', {}) or {}
                    if env_vars:
                        env_keys = ", ".join(sorted(env_vars.keys()))
                        lines.append(f"- configured_env: `{env_keys}`")
                        lines.append(f"- ⚠️ 凭据已配置完毕，可直接使用，无需再问用户要密码或配置")
                    # Tool manifest — this is the fix for the class of
                    # bugs where the agent had to guess tool names.
                    lines.extend(_render_tools(m.id))
                    lines.append("")
            else:
                lines.append("## Bound MCPs")
                lines.append("")
                lines.append("- (none — bind MCPs via Portal → MCP Manager, e.g. email, "
                             "slack, github, postgres)")
            mcp_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass

        return ws

    # ── Skill package sync (grant → copy to agent workspace) ──

    def sync_skill_to_workspace(self, install: Any) -> dict:
        """Copy the full skill package to this agent's workspace/skills/<name>/.

        Called when a skill is granted. Copies SKILL.md, scripts/, reference
        MDs, and any other files from the global install_dir into the
        agent-local skills directory so the agent can ``cd`` into it and
        run scripts directly.

        Also auto-adds a capability entry (``<name>:rw``) to
        ``profile.skill_capabilities`` if not already present.

        Args:
            install: A ``SkillInstall`` instance (from skills/engine.py).

        Returns:
            dict with ``ok``, ``skill_dir``, ``files_copied``, ``capability``.
        """
        import shutil as _shutil

        name = getattr(install, "manifest", None)
        skill_name = getattr(name, "name", "") if name else ""
        if not skill_name:
            skill_name = getattr(install, "id", "unknown")
        src = Path(getattr(install, "install_dir", ""))
        if not src.is_dir():
            return {"ok": False, "error": f"source install_dir not found: {src}"}

        ws = self._get_agent_workspace()
        dest = ws / "skills" / skill_name
        try:
            if dest.exists():
                _shutil.rmtree(dest)
            _shutil.copytree(str(src), str(dest))
            # Fix permissions — source files may be read-only
            for fp in dest.rglob("*"):
                try:
                    fp.chmod(0o644 if fp.is_file() else 0o755)
                except Exception:
                    pass
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        # Count files copied
        files_copied = [str(f.relative_to(dest)) for f in dest.rglob("*") if f.is_file()]

        # Auto-add capability
        cap = f"{skill_name}:rw"
        if cap not in self.profile.skill_capabilities:
            self.profile.skill_capabilities.append(cap)

        logger.info("sync_skill_to_workspace: %s → %s (%d files)",
                     skill_name, dest, len(files_copied))
        return {
            "ok": True,
            "skill_name": skill_name,
            "skill_dir": str(dest),
            "files_copied": files_copied,
            "capability": cap,
        }

    def remove_skill_from_workspace(self, skill_name: str) -> dict:
        """Remove a skill package from this agent's workspace on revoke.

        Also removes the corresponding capability from
        ``profile.skill_capabilities``.

        Args:
            skill_name: The skill name (directory name under workspace/skills/).

        Returns:
            dict with ``ok`` and details.
        """
        import shutil as _shutil

        ws = self._get_agent_workspace()
        dest = ws / "skills" / skill_name
        removed_files = 0
        if dest.exists():
            try:
                removed_files = sum(1 for f in dest.rglob("*") if f.is_file())
                _shutil.rmtree(dest)
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        # Remove capability
        cap = f"{skill_name}:rw"
        if cap in self.profile.skill_capabilities:
            self.profile.skill_capabilities.remove(cap)

        logger.info("remove_skill_from_workspace: %s removed (%d files)",
                     skill_name, removed_files)
        return {"ok": True, "skill_name": skill_name, "removed_files": removed_files}

    def get_skill_workspace_dir(self, skill_name: str) -> Path | None:
        """Return the agent-local skill directory if it exists."""
        ws = self._get_agent_workspace()
        d = ws / "skills" / skill_name
        return d if d.is_dir() else None

    def _get_scheduled_context(self) -> str:
        """Load workspace markdown files and inject into dynamic context.

        Per-context filtering (2026-04-28):
          * Solo chat       → all 5 files (agent's personal workspace view)
          * Project / Meeting → only `granted_skills.md` + `MCP.md`
            (agent's capability list — needed in every context).
            `Project.md` / `Tasks.md` / `Scheduled.md` are agent's
            **personal** memos and have no business showing up in a
            project's group chat — the project's own prompt
            (`_build_chat_prompt`) already supplies the right context.
        """
        try:
            ws = self._ensure_workspace_layout()
        except Exception:
            return ""
        # Pick file set by active context.
        # NOTE (2026-04-28): granted_skills.md NO LONGER injected here —
        # it moved to the STATIC system prompt (cache-friendly: only
        # invalidated on grant/revoke). The on-disk file still exists
        # as a human-readable mirror but is not part of the prompt
        # injection path anymore. Agent reads each SKILL.md on demand.
        _ctx = getattr(self, "_active_context_id", "solo") or "solo"
        if _ctx.startswith("project:") or _ctx.startswith("meeting:"):
            _files = (
                ("MCP.md", "mcp_servers"),
            )
        else:
            _files = (
                ("Project.md", "project"),
                ("MCP.md", "mcp_servers"),
                ("Tasks.md", "tasks"),
                ("Scheduled.md", "scheduled_tasks"),
            )
        blocks = []
        for fname, tag in _files:
            fp = ws / fname
            if not fp.exists():
                continue
            try:
                content = fp.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                continue
            blocks.append(f'<{tag} file="workspace/{fname}">\n{content}\n</{tag}>')
        if not blocks:
            return ""
        # Opt 3 (Nov 2026): shrunk the workspace header from ~1400 chars
        # to ~400 chars. The info was largely boilerplate that duplicated
        # what's already visible via the XML blocks below OR obvious from
        # tool descriptions. Kept only the decisions the LLM genuinely
        # needs to repeatedly remember (output location / scheduled-task
        # 3-step / MCP discovery pattern / config-over-history rule).
        # Minimal header — only structural info (workspace path + XML block
        # freshness notice). All behavioural rules (plan_update, MCP,
        # confirmations, etc.) must come from the user's global system
        # prompt / role preset / SOUL configuration, not from code.
        header = (
            f"\n[Agent workspace: {ws}]\n"
            "下方 <project>/<skills>/<mcp_servers>/<tasks>/... XML 块反映**当前实时配置**，"
            "与对话历史冲突时以它们为准。\n"
        )
        # Auto-route scores table — injected only when auto_route is enabled
        # AND extra_llms has real candidates. This is config-driven data,
        # not a behavioural rule. If operator doesn't want it they can
        # disable auto_route in agent config.
        try:
            ar = self.auto_route or {}
            if ar.get("enabled") and self.extra_llms:
                from . import llm_router as _router
                _scores_hint = _router.build_scores_hint_for_agent(
                    primary_provider=self.provider or "",
                    primary_model=self.model or "",
                    extra_llms=self.extra_llms,
                )
                if _scores_hint:
                    header = header + "\n" + _scores_hint
        except Exception as _sh_err:
            logger.debug("scores hint injection skipped: %s", _sh_err)
        return header + "\n" + "\n".join(blocks)

    # ------------------------------------------------------------------
    # System prompt: split into STATIC (cached) + DYNAMIC (per-call)
    # ------------------------------------------------------------------

    # Bump this whenever _build_static_system_prompt's body changes
    # in a way that should invalidate every cached prompt — e.g.
    # adding/editing a system-wide section like <file_display>.
    # This guarantees cache freshness even when no profile field
    # changed between two code versions running in the same process.
    _STATIC_PROMPT_BUILD_VERSION = "v7-no_plan_repeat"

    def _get_global_system_prompt(self) -> str:
        """Legacy compat — returns empty if global_system_prompt migrated to scene_prompts."""
        try:
            from . import llm as _llm
        except Exception:
            try:
                from app import llm as _llm  # type: ignore
            except Exception:
                return ""
        try:
            cfg = _llm.get_config()
        except Exception:
            return ""
        val = cfg.get("global_system_prompt", "") if isinstance(cfg, dict) else ""
        return val.strip() if isinstance(val, str) else ""

    def _get_scene_prompts_text(self) -> str:
        """Build unified system prompts text from scene_prompts + legacy global_system_prompt."""
        try:
            from . import llm as _llm_mod
            cfg = _llm_mod.get_config()
        except Exception:
            return ""
        parts = []

        # Legacy: if global_system_prompt still has content, include it first
        global_sp = ""
        try:
            val = cfg.get("global_system_prompt", "")
            global_sp = val.strip() if isinstance(val, str) else ""
        except Exception:
            pass
        if global_sp:
            parts.append(f"<system_prompt name=\"Global Rules\">\n{global_sp}\n</system_prompt>")

        # System prompts (unified list) — filter by scope/role
        agent_role = getattr(self, "role", "") or ""
        scene_prompts = cfg.get("scene_prompts", [])
        for sp in scene_prompts:
            if not isinstance(sp, dict):
                continue
            if not sp.get("enabled", True):
                continue
            # Scope filtering: "all" applies to every agent,
            # "roles" only applies to agents whose role is in the list
            scope = sp.get("scope", "all")
            if scope == "roles":
                allowed_roles = sp.get("roles", [])
                if agent_role not in allowed_roles:
                    continue
            name = sp.get("name", "").strip()
            prompt = sp.get("prompt", "").strip()
            if not prompt:
                continue
            if name:
                parts.append(f"<system_prompt name=\"{name}\">\n{prompt}\n</system_prompt>")
            else:
                parts.append(f"<system_prompt>\n{prompt}\n</system_prompt>")

        return "\n\n".join(parts) if parts else ""

    def _compute_static_prompt_hash(self) -> str:
        """Compute a lightweight hash of inputs that affect the static prompt.

        If this hash hasn't changed, the cached static prompt is still valid.
        """
        import hashlib
        p = self.profile
        parts = [
            self._STATIC_PROMPT_BUILD_VERSION,
            self.name, self.role, self.model or "",
            self.system_prompt or "",
            p.personality, p.communication_style,
            ",".join(p.expertise), ",".join(p.skills),
            p.language or "", p.custom_instructions or "",
            self.working_dir or "",
            (self.get_active_shared_workspace()
             if hasattr(self, "get_active_shared_workspace") else ""),
            self.project_id or "",
            self.project_name or "",
            self.soul_md or "",
            self._get_global_system_prompt(),
            self._get_scene_prompts_text(),
        ]
        # ── Project-context freshness ───────────────────────────────
        # mtime-nanoseconds of PROJECT_CONTEXT.md (and legacy siblings)
        # so edits invalidate the cached prompt at next chat(), without
        # requiring an agent restart.  Cheap stat() — no file read.
        try:
            from pathlib import Path as _HPath
            _hdirs: list[str] = []
            if self.working_dir:
                _hdirs.append(self.working_dir)
            _active_sw = self.get_active_shared_workspace()
            if _active_sw and _active_sw != self.working_dir:
                _hdirs.append(_active_sw)
            for _d in _hdirs:
                for _n in ("PROJECT_CONTEXT.md", "TUDOU_CLAW.md",
                           "CLAW.md", "README.md"):
                    try:
                        _f = _HPath(_d) / _n
                        if _f.exists():
                            parts.append(f"{_n}@{_d}:{_f.stat().st_mtime_ns}")
                    except OSError:
                        continue
        except Exception:
            pass
        # ── Granted-skill freshness ───────────────────────────────────
        # Skill grants are stable per-agent (changed only via Portal
        # grant/revoke). Use the **registry**'s view, not
        # ``self.granted_skills`` — the agent's local copy can drift out
        # of sync when a grant only updates the registry side (this
        # actually happened with drawio-skill on 2026-04-30: registry
        # had the grant, agent's list didn't, so the static prompt
        # cache key never changed and the agent kept reporting "no
        # such skill" forever). Falling back to ``self.granted_skills``
        # keeps the build working even if the registry is unreachable.
        #
        # Also fold each granted skill's SKILL.md mtime into the hash
        # (HANDOFF [F], 2026-05-01) so editing a SKILL.md propagates
        # to the next chat turn without a server restart. Before this,
        # SKILL.md content was baked into the cached static prompt and
        # only refreshed when grant_ids changed.
        try:
            _grant_ids: list[str] = []
            _md_mtimes: list[str] = []
            try:
                from .skills.engine import get_registry as _grs
                _r = _grs()
                if _r is not None:
                    for _i in _r.list_for_agent(self.id):
                        _gid = getattr(_i, "id", "") or ""
                        if _gid:
                            _grant_ids.append(_gid)
                        _idir = getattr(_i, "install_dir", "") or ""
                        if _gid and _idir:
                            _mp = os.path.join(_idir, "SKILL.md")
                            try:
                                _md_mtimes.append(
                                    f"{_gid}:{os.stat(_mp).st_mtime_ns}"
                                )
                            except OSError:
                                # SKILL.md missing or unreadable — skip
                                # silently; grant_id alone keys the hash.
                                continue
            except Exception:
                _grant_ids = []
            if not _grant_ids:
                _grant_ids = list(self.granted_skills or [])
            parts.append(",".join(sorted(_grant_ids)))
            if _md_mtimes:
                parts.append("skill_md|" + ",".join(sorted(_md_mtimes)))
        except Exception:
            pass
        # ── Active context flavor (2026-04-29) ────────────────────────
        # Static prompt now varies between solo / project / meeting
        # (shared_workspace is filtered out in solo). Without this
        # field in the hash, switching from a project chat into solo
        # would reuse the cached project-flavored prompt and the
        # filtering wouldn't take effect.
        try:
            _ctx_kind = (getattr(self, "_active_context_id", "solo") or "solo").split(":", 1)[0]
            parts.append(f"ctx_kind={_ctx_kind}")
        except Exception:
            pass
        return hashlib.md5("|".join(parts).encode()).hexdigest()

    def _build_static_system_prompt(self) -> str:
        """Build the STATIC portion of the system prompt.

        This includes: identity, personality, tools description, language,
        custom instructions, project context files (TUDOU_CLAW.md etc.), and
        model-specific guidance.  These change rarely (only on config edits).

        Cached via _cached_static_prompt / _static_prompt_hash.
        """
        current_hash = self._compute_static_prompt_hash()
        if self._cached_static_prompt and self._static_prompt_hash == current_hash:
            return self._cached_static_prompt

        p = self.profile
        wd = self._effective_working_dir()

        # ── DEFAULT + SETTINGS + PERSONA ────────────────────────────
        # Single source of truth: app/system_prompt.py composes the
        # platform contract (DEFAULT, hardcoded), the operator-config
        # block (SETTINGS — scene_prompts), and the per-agent persona
        # (system_prompt + soul_md + custom_instructions). Replaces
        # ~125 lines of branching here with one call.
        #
        # 3 persona fields with distinct semantics:
        #   self.system_prompt        → identity & expertise (what)
        #   self.soul_md              → communication & behavior (how)
        #   p.custom_instructions     → ad-hoc additions / overrides
        from . import system_prompt as _sp
        parts = [_sp.compose_full_prompt(
            name=self.name,
            role=self.role,
            language=p.language or "auto",
            ctx_type=getattr(self, "context_type", "solo") or "solo",
            working_dir=str(wd),
            shared_workspace=self.get_active_shared_workspace(),
            project_name=getattr(self, "project_name", "") or "",
            project_id=getattr(self, "project_id", "") or "",
            meeting_id=getattr(self, "source_meeting_id", "") or "",
            agent_system_prompt=self.system_prompt or "",
            agent_soul_md=getattr(self, "soul_md", "") or "",
            agent_custom_instructions=p.custom_instructions or "",
        )]

        # Auto-inject Retrieval Protocol for RAG-bound advisors (lives
        # outside system_prompt.py because it depends on profile.rag_*
        # fields — agent-runtime data).
        _rp = _build_retrieval_protocol(p)
        if _rp:
            parts.append(_rp)

        # File display contract — keeps the agent from writing broken
        # markdown image syntax for binary files, or "drag the file into
        # the chat" prose. The portal renders FileCards automatically
        # from the deliverable_dir, so the agent does not need to (and
        # must not) try to embed media inline in its reply text.
        # Text lives in app.system_prompt._FILE_DISPLAY_LONG so the v2
        # block catalog (Phase 2b) can mirror it without copy-paste.
        parts.append("")
        parts.append(_sp._FILE_DISPLAY_LONG)

        # Project context files — persistent project knowledge pinned into
        # every turn.  Modeled after Claude Code's CLAUDE.md convention:
        # drop a PROJECT_CONTEXT.md at the project root and its contents
        # automatically prime every agent that touches the directory.
        #
        # Search order:
        #   • working_dir (private)   + shared_workspace (team)
        #   • filename priority per dir: PROJECT_CONTEXT.md (canonical) >
        #     TUDOU_CLAW.md > CLAW.md > README.md  (first match per dir)
        #
        # Cost: lives in the STATIC system prompt, so it's captured by both
        # local KV-cache prefixes and Anthropic prompt-caching (2.2.5) —
        # after first turn it's effectively free tokens.
        from pathlib import Path as _PCPath
        _ctx_dirs: list[_PCPath] = [wd]
        # Resolve shared workspace from the CURRENT active context
        # (multi-project agents get the right project's dir per turn).
        # Empty string in solo → only working_dir is scanned.
        _active_sw = self.get_active_shared_workspace()
        if _active_sw:
            try:
                _sw = _PCPath(_active_sw)
                if _sw.resolve() != wd.resolve():
                    _ctx_dirs.append(_sw)
            except (OSError, ValueError):
                pass
        _seen_paths: set[str] = set()
        for _dir in _ctx_dirs:
            for name in ("PROJECT_CONTEXT.md", "TUDOU_CLAW.md",
                         "CLAW.md", "README.md"):
                ctx_file = _dir / name
                try:
                    if not ctx_file.exists():
                        continue
                    _rp = str(ctx_file.resolve())
                    if _rp in _seen_paths:
                        continue
                    _seen_paths.add(_rp)
                    # Cap at 1500 chars / file (was 4000) — multi-doc
                    # setups (working_dir + shared_workspace × 4 candidate
                    # filenames) could blow past 8K just on context docs,
                    # which dominated the static prompt. 1500 fits a full
                    # PROJECT_CONTEXT.md preface (purpose + main directives)
                    # while leaving room for the rest of the static prompt
                    # to stay under 20K total.
                    content = ctx_file.read_text(
                        encoding="utf-8", errors="replace")[:1500]
                    parts.append(
                        f"\n<project_context file=\"{name}\" "
                        f"dir=\"{_dir.name}\">\n{content}\n"
                        f"</project_context>"
                    )
                    break  # one file per directory (priority order)
                except OSError:
                    continue

        # ── Granted skills block (moved from dynamic ctx 2026-04-28) ──
        # Skill grants are stable per-agent — they only change when the
        # admin grants/revokes via the Portal. By living in the STATIC
        # prompt, this block hits prompt cache after the first turn
        # (effectively zero token cost) instead of being re-sent every
        # turn as it was in the old `scheduled` / `skills_registry`
        # dynamic-context paths.
        #
        # Single source of truth: hub.skill_registry. The on-disk
        # `granted_skills.md` is just a human-readable mirror — neither
        # injection layer depends on it.
        try:
            import sys as _sys
            _llm_mod = _sys.modules.get(__package__ + ".llm") if __package__ else None
            _hub = getattr(_llm_mod, "_active_hub", None) if _llm_mod else None
            if _hub is not None and getattr(_hub, "skill_registry", None) is not None:
                _gs_lines: list[str] = []
                for _inst in _hub.skill_registry.list_for_agent(self.id):
                    _m = _inst.manifest
                    _sid = getattr(_m, "id", "") or getattr(_m, "name", "") or "?"
                    _path = _inst.install_dir or ""
                    _desc_raw = getattr(_m, "description", "") or ""
                    if isinstance(_desc_raw, dict):
                        _desc_raw = (_desc_raw.get("zh-CN")
                                     or _desc_raw.get("en")
                                     or next(iter(_desc_raw.values()), ""))
                    _desc = " ".join(str(_desc_raw).split())[:120]
                    if _desc:
                        _gs_lines.append(f"- `{_sid}` — {_desc} (path: `{_path}`)")
                    else:
                        _gs_lines.append(f"- `{_sid}` (path: `{_path}`)")
                if _gs_lines:
                    parts.append(
                        "\n## Granted Skills\n\n"
                        "已授权给本 agent 的技能(ID + 一句话 + 安装路径)。需要某个技能的"
                        "详细用法时,`read_file <path>/SKILL.md` 或 "
                        "`get_skill_guide(name)` 拉完整指南。\n\n"
                        + "\n".join(_gs_lines)
                    )
        except Exception as _gse:
            logger.debug("granted-skills static block skipped: %s", _gse)

        # Model-specific tool use guidance (depends on model, rarely changes)
        guidance = security.get_model_tool_guidance(self.model or "")
        if guidance:
            parts.append(guidance)

        is_zh = (self.system_prompt and len(self.system_prompt) > 200)
        # --- Workspace awareness: tell the Agent exactly where to write files ---
        #
        # Routing rule (driven by Agent.context_type; see dataclass docstring):
        #   solo     → all produced files go to private working_dir
        #   project  → ALL produced files go to project shared_workspace
        #   meeting  → ALL produced files go to meeting shared_workspace
        #
        # Text + zh/en branches + project/meeting→solo degradation are all
        # in app.system_prompt._workspace_context_long so the v2 block
        # catalog mirrors it without duplicating ~60 lines.
        use_zh = is_zh or (p.language and p.language.startswith("zh"))
        ctx_type = (self.context_type or "solo").lower()
        parts.append("\n" + _sp._workspace_context_long(
            ctx_type=ctx_type,
            use_zh=bool(use_zh),
            working_dir=str(wd),
            shared_workspace=self.get_active_shared_workspace(),
            project_name=self.project_name or "",
            project_id=self.project_id or "",
        ))

        # --- Attachment contract: reminds the agent to actually attach
        # files when calling send_* tools. Text in app.system_prompt
        # so the v2 catalog can gate it on has_tools_in={send_*} without
        # copy-pasting.
        parts.append("\n" + (
            _sp._ATTACHMENT_CONTRACT_ZH if use_zh else _sp._ATTACHMENT_CONTRACT_EN
        ))

        # --- Inline image display: tell the agent how to surface images ---
        # Portal chat renders markdown `![alt](path)` as an inline <img> by
        # routing the path through /api/portal/attachment. Long-form text
        # lives in app.system_prompt for catalog reuse.
        parts.append("\n" + (
            _sp._IMAGE_DISPLAY_LONG_ZH if use_zh else _sp._IMAGE_DISPLAY_LONG_EN
        ))

        # ── Plan + step tracking protocol (for UI task-queue visuals) ─
        # Drives the TASK QUEUE panel: agent emits 📋 plan + ✓ markers,
        # host observes & updates UI. Forget → no harm, just no step
        # progress visualization that turn. Picks ZH/EN from
        # profile.language so English-only agents don't carry Chinese
        # rules they can't act on.
        parts.append("\n" + _sp.select_plan_protocol(p.language or "auto"))

        result = "\n".join(parts)

        # NOTE (2026-04-28 dedup): the previous prepend of
        # ``self._get_scene_prompts_text()`` was a literal duplicate of the
        # SETTINGS section that ``compose_full_prompt`` already emits via
        # ``build_settings_block(role)``. The two functions read the same
        # ``scene_prompts`` config and wrap it in the same
        # ``<system_prompt name="...">`` tags — net effect was that every
        # agent's static prompt carried its scene_prompts TWICE (cost
        # ~1,223 tokens per call on the typical 4,892-char block). Removing
        # the prepend keeps semantics: persona still comes AFTER settings
        # inside ``compose_full_prompt`` so persona-level overrides win.

        # ── B (Nov 2026): granted-skills roster ─────────────────────────
        # Short one-line-per-skill list so the LLM knows its toolkit
        # without having to call get_skill_guide to discover. ~40 tokens
        # per skill; at 10-15 skills that's ~500 tokens. Lives in static
        # prompt so it rides the KV-cache across all iterations.
        try:
            _roster = _build_granted_skills_roster(self)
            if _roster:
                result = result + "\n\n" + _roster
        except Exception as _rerr:
            logger.debug("granted skills roster build failed: %s", _rerr)

        # ── Preprocessor: optimize_prompt phase ────────────────────────
        # If this agent has preprocessor_model configured AND
        # "optimize_prompt" mode is on AND the static prompt is large
        # enough to benefit (≥ 5000 chars), run the prompt through the
        # small local LLM to compress redundancy. The optimized version
        # gets cached via the same hash, so subsequent calls reuse it
        # at zero cost. Failures silently fall back to the original —
        # this can never break the chat path.
        try:
            from .preprocessing import bridge as _prep_bridge
            if (_prep_bridge.is_enabled(self)
                    and "optimize_prompt" in _prep_bridge.get_modes(self)
                    and len(result) >= 5000):
                # 30s timeout: large system prompts (10-30K chars) need
                # ~10-25s on 3B-class models. Subsequent calls hit the LRU
                # cache (~0ms). TODO: pre-warm async at agent startup so
                # first chat doesn't pay the latency hit.
                _t0 = time.time()
                _res = _prep_bridge.invoke(
                    self,
                    kind="prompt_optimize",
                    payload={"prompt": result},
                    timeout_s=30.0,
                )
                _elapsed_ms = int((time.time() - _t0) * 1000)
                if _res.ok and isinstance(_res.value, dict):
                    _optimized = _res.value.get("prompt") or ""
                    _saved = int(_res.value.get("saved_tokens") or 0)
                    if _optimized and len(_optimized) < len(result):
                        logger.info(
                            "preprocessor.optimize_prompt agent=%s "
                            "%d→%d chars (saved ~%d tokens, %dms, cache_hit=%s)",
                            self.id, len(result), len(_optimized),
                            _saved, _elapsed_ms, _res.cache_hit,
                        )
                        result = _optimized
                else:
                    logger.debug(
                        "preprocessor.optimize_prompt skipped agent=%s reason=%s",
                        self.id, _res.skip_reason,
                    )
        except Exception as _pe:
            logger.debug(
                "preprocessor.optimize_prompt failed (non-fatal): %s", _pe,
            )

        self._cached_static_prompt = result
        self._static_prompt_hash = current_hash
        logger.debug("Static system prompt rebuilt (hash=%s, len=%d)",
                     current_hash[:8], len(result))

        # ── Phase 2b dry-run hook ───────────────────────────────────
        # When TUDOU_PROMPT_V2_DRYRUN=1, also compute the v2 declarative
        # assembly result and log a diff summary. v1 (this method's
        # return value) is unchanged — this is observation-only.
        # Used to verify v2 catalog conditions are correct before the
        # Stage B/C cutover. ALWAYS catches exceptions — dry-run must
        # not break the live path.
        if os.environ.get("TUDOU_PROMPT_V2_DRYRUN", "0") == "1":
            try:
                self._dry_run_prompt_v2(v1_text=result)
            except Exception as _e:
                logger.debug("prompt_v2 dry-run skipped: %s", _e)

        return result

    def _dry_run_prompt_v2(self, v1_text: str) -> None:
        """Compute v2 prompt assembly + log diff vs v1. No side effect on v1.

        Prefetches IO-bound block data (project_context_files,
        retrieval_protocol, model_guidance, granted_skills_roster) into
        ``extras`` so v2's output reflects what it WOULD produce in
        production. Without prefetch the diff_summary over-estimates the
        delta.
        """
        from .prompt_blocks import AssemblyContext
        from .prompt_block_catalog import get_default_catalog
        from .system_prompt_v2 import assemble_with_log, diff_summary

        p = self.profile
        granted_tools = set(getattr(self, "_granted_tool_names", None) or [])
        granted_skills = set(getattr(p, "granted_skills", None) or [])

        # ── Prefetch IO-bound block content ─────────────────────────
        # retrieval_protocol — RAG advisor only
        try:
            retrieval_protocol = _build_retrieval_protocol(p) or ""
        except Exception:
            retrieval_protocol = ""

        # model_guidance — model-specific
        try:
            from . import security as _sec
            model_guidance = _sec.get_model_tool_guidance(self.model or "") or ""
        except Exception:
            model_guidance = ""

        # granted_skills_roster — uses the same builder as v1
        try:
            granted_skills_roster = _build_granted_skills_roster(self) or ""
        except Exception:
            granted_skills_roster = ""

        # project_context_files — only for project/meeting agents.
        # Read PROJECT_CONTEXT.md / TUDOU_CLAW.md / CLAW.md / README.md
        # from working_dir + shared_workspace, mirroring agent.py inline.
        project_context_files: list = []
        try:
            from pathlib import Path as _PCPath
            wd = self._effective_working_dir()
            _ctx_dirs: list[_PCPath] = [wd]
            _active_sw = self.get_active_shared_workspace()
            if _active_sw:
                try:
                    _sw = _PCPath(_active_sw)
                    if _sw.resolve() != wd.resolve():
                        _ctx_dirs.append(_sw)
                except (OSError, ValueError):
                    pass
            _seen: set[str] = set()
            for _dir in _ctx_dirs:
                for name in ("PROJECT_CONTEXT.md", "TUDOU_CLAW.md",
                             "CLAW.md", "README.md"):
                    f = _dir / name
                    try:
                        if not f.exists():
                            continue
                        _rp = str(f.resolve())
                        if _rp in _seen:
                            continue
                        _seen.add(_rp)
                        project_context_files.append(
                            (name, f.read_text(encoding="utf-8",
                                               errors="replace")[:4000]),
                        )
                        break  # one file per dir, priority order
                    except OSError:
                        continue
        except Exception:
            project_context_files = []

        ctx = AssemblyContext.make(
            scope_tags=[],  # empty in dry-run; real wire-in passes scopes
            granted_tools=granted_tools,
            granted_skills=granted_skills,
            role_kind=(self.role or "").lower(),
            ctx_type=(getattr(self, "context_type", "solo") or "solo"),
            extras={
                "agent_name": self.name,
                "agent_role": self.role,
                "language": getattr(p, "language", "auto") or "auto",
                "agent_system_prompt": self.system_prompt or "",
                "agent_soul_md": getattr(self, "soul_md", "") or "",
                "agent_custom_instructions": getattr(p, "custom_instructions", "") or "",
                "working_dir": str(self._effective_working_dir()),
                "shared_workspace": (
                    self.get_active_shared_workspace()
                    if hasattr(self, "get_active_shared_workspace") else ""
                ),
                "project_name": getattr(self, "project_name", "") or "",
                "project_id": getattr(self, "project_id", "") or "",
                "meeting_id": getattr(self, "source_meeting_id", "") or "",
                # Phase 2b Stage B — prefetched IO-bound block data
                "retrieval_protocol": retrieval_protocol,
                "model_guidance": model_guidance,
                "granted_skills_roster": granted_skills_roster,
                "project_context_files": project_context_files,
            },
        )
        v2_text, _result = assemble_with_log(
            get_default_catalog(), ctx, agent_id=self.id,
        )
        # Diff summary at INFO so operator can see in-flight comparison
        d = diff_summary(v1_text, v2_text)
        logger.info(
            "[prompt_v2_diff] agent=%s v1=%dch v2=%dch delta=%+dch "
            "v1_only_lines=%d v2_only_lines=%d",
            self.id[:8],
            d["v1_chars"], d["v2_chars"], d["delta_chars"],
            d["only_in_v1_count"], d["only_in_v2_count"],
        )

    def _build_dynamic_context(self, current_query: str = "") -> str:
        """Build the DYNAMIC portion injected as a separate context message.

        This includes: git status, workspace/scheduled tasks, skill files,
        experience library, enhancement knowledge, and L2/L3 memory retrieval.
        These may change on every call, so they are kept separate from the
        static system prompt to preserve prompt caching.

        Budget-aware: limits total dynamic context to at most 30% of the
        context window, so conversation messages have room to breathe.
        """
        context_limit = self._get_context_limit()
        static_len = len(self._cached_static_prompt) if self._cached_static_prompt else 0
        # Reserve at least 50% of context for conversation; static prompt also counted
        # Rough: 1 token ≈ 3 chars for CJK, 4 chars for EN → use 3 as conservative
        static_tokens = static_len // 3
        max_dynamic_tokens = max(200, (context_limit - static_tokens) * 3 // 10)  # 30% of remaining
        max_dynamic_chars = max_dynamic_tokens * 3  # convert back to chars

        parts = []
        total_chars = 0
        # ── Per-section size tracking for the breakdown log ──
        # Helps locate which dynamic section is bloating the prompt.
        section_sizes: dict[str, int] = {}

        def _try_add(text: str, section: str = "_misc") -> bool:
            """Add text to parts if within budget. Returns True if added."""
            nonlocal total_chars
            if not text:
                return False
            if total_chars + len(text) > max_dynamic_chars:
                # Try truncated version
                remaining = max_dynamic_chars - total_chars
                if remaining > 200:
                    truncated = text[:remaining] + "\n...[truncated]"
                    parts.append(truncated)
                    section_sizes[section] = (
                        section_sizes.get(section, 0) + len(truncated)
                    )
                    total_chars = max_dynamic_chars
                return False
            parts.append(text)
            total_chars += len(text)
            section_sizes[section] = (
                section_sizes.get(section, 0) + len(text)
            )
            return True

        # Priority order: most important context first

        # 0.0 Source hint — when this turn was triggered by another agent
        # (source="agent:<id>") or by a project task / system event, tell
        # the LLM upfront so it understands the authority chain.
        # User-driven turns (source="user" / "admin") are the default
        # path and need no hint. Skipped silently if no last-user msg.
        try:
            _last_user = None
            for _m in reversed(self.messages):
                if _m.get("role") == "user":
                    _last_user = _m
                    break
            _src_raw = (_last_user or {}).get("source", "") or ""
            if _src_raw and ":" in _src_raw:
                _kind, _src_id = _src_raw.split(":", 1)
            else:
                _kind, _src_id = _src_raw, ""
            if _kind == "agent" and _src_id:
                # Resolve the source agent's display name via hub.
                import sys as __sys
                __llm_mod = __sys.modules.get(__package__ + ".llm") if __package__ else None
                __hub = getattr(__llm_mod, "_active_hub", None) if __llm_mod else None
                __src_a = __hub.agents.get(_src_id) if (__hub and hasattr(__hub, "agents")) else None
                if __src_a is not None:
                    _try_add(
                        f"## 当前任务来源\n"
                        f"本轮由 **{__src_a.role}-{__src_a.name}** 派单给你。"
                        f"完成后请把结论 / 产出回报给他(在当前群聊里 @ 他,或调 "
                        f"`send_message(to_agent=\"{_src_a.id if False else __src_a.id}\", ...)`)。",
                        "source_hint",
                    )
            elif _kind in ("task", "milestone") and _src_id:
                _try_add(
                    f"## 当前任务来源\n"
                    f"本轮由项目任务 / 里程碑(`{_src_raw}`)触发。"
                    f"完成后请按提示调 `submit_deliverable` / "
                    f"`update_milestone_status` 收尾。",
                    "source_hint",
                )
            elif _kind == "system":
                _try_add(
                    f"## 当前任务来源\n"
                    f"本轮由系统事件触发(`{_src_raw}` —— 通常是项目恢复 / 自动唤醒)。",
                    "source_hint",
                )
            # "user" / "admin" / empty → no hint (default human path)
        except Exception as _she:
            try:
                logger.debug("source_hint injection skipped: %s", _she)
            except Exception:
                pass

        # 0.0 Plan state injection (P0/L1) — authoritative "where am I"
        # snapshot so the LLM doesn't have to reconstruct its own
        # execution state from scattered tool_result history. See
        # agent.format_plan_state_for_llm() for format. Kept first so
        # budget truncation can't drop it.
        try:
            plan_ctx = self.format_plan_state_for_llm()
            if plan_ctx:
                _try_add(plan_ctx, "plan")
        except Exception as _pe:
            try:
                logger.debug("plan state injection skipped: %s", _pe)
            except Exception:
                pass

        # 1. Shared Knowledge Wiki (lightweight title list)
        try:
            from . import knowledge as _kb
            kb_summary = _kb.get_prompt_summary()
            _try_add(kb_summary, "kb_wiki")
        except Exception:
            pass

        # 2. Workspace files (MCP, Tasks, Scheduled — needed for tool usage)
        sched_ctx = self._get_scheduled_context()
        _try_add(sched_ctx, "scheduled")

        # 2.5 Recent artifacts in workspace (deliverables agent produced).
        # Without this, agent forgets files it created earlier in the same
        # session and does wasteful web_fetch / read_file loops trying to
        # re-source them. See user screenshot 2026-04-24 — agent claimed
        # to be "trapped in file-read loop" because it couldn't find its
        # own saudi_finance_cloud_compliance.pptx.
        try:
            artifacts_ctx = self._build_recent_artifacts_context()
            if artifacts_ctx:
                _try_add(artifacts_ctx, "recent_artifacts")
        except Exception as _ae:
            logger.debug("recent-artifacts injection skipped: %s", _ae)

        # 3. Git context (with cooldown)
        now = time.time()
        if now - self._git_context_ts >= self._GIT_CONTEXT_COOLDOWN:
            self._cached_git_context = self._get_git_context()
            self._git_context_ts = now
        _try_add(self._cached_git_context, "git")

        # 4. Three-layer memory: L2 + L3 retrieval (query-dependent)
        mm = self._get_memory_manager()
        if mm and current_query and total_chars < max_dynamic_chars:
            try:
                mem_config = self._get_memory_config()
                memory_context = mm.retrieve_for_prompt(
                    self.id, current_query, config=mem_config,
                )
                _try_add(memory_context or "", "memory_l2l3")
                # ── 记录本次记忆注入的体量，供 portal 展示"记忆使用比例" ──
                try:
                    mem_chars = len(memory_context or "")
                    stats = getattr(self, "_memory_usage_stats", None)
                    if stats is None:
                        stats = {
                            "last_mem_chars": 0,
                            "last_total_chars": 0,
                            "last_budget": 0,
                            "last_ratio": 0.0,
                            "ema_ratio": 0.0,
                            "samples": 0,
                            "last_query_ts": 0.0,
                        }
                        self._memory_usage_stats = stats
                    stats["last_mem_chars"] = mem_chars
                    stats["last_budget"] = max_dynamic_chars
                    stats["last_query_ts"] = time.time()
                    # ratio = 记忆字符 / 动态上下文预算
                    ratio = mem_chars / max(max_dynamic_chars, 1)
                    stats["last_ratio"] = ratio
                    stats["samples"] += 1
                    # 指数移动平均，便于展示稳定的"近期记忆占用"
                    alpha = 0.3
                    stats["ema_ratio"] = (
                        alpha * ratio + (1 - alpha) * stats["ema_ratio"]
                    )
                except Exception as _se:
                    logger.debug("memory_usage_stats update failed: %s", _se)
            except Exception as e:
                logger.debug("Memory retrieval failed: %s", e)

        # 5. SKILL.md knowledge
        if total_chars < max_dynamic_chars:
            _try_add(self._get_skill_context(), "skill_md")

        # 6. Enhancement module knowledge
        if total_chars < max_dynamic_chars and self.enhancer and self.enhancer.enabled:
            enhanced = self.enhancer.enhance_system_prompt("", context_hint=self.role)
            _try_add(enhanced or "", "enhance")

        # 7. Self-improvement experience library (LEGACY — index-only)
        if total_chars < max_dynamic_chars and self.self_improvement and self.self_improvement.enabled:
            exp_ctx = self.self_improvement.build_experience_context()
            _try_add(exp_ctx or "", "experience")

        # 7.5. Wiki index injection (Karpathy pattern). Per-role + global
        # markdown pages indexed by title — agent calls knowledge_lookup or
        # reads pages on-demand when it actually needs the content. Index
        # alone is ~50 chars per page so 30 pages = ~1.5K. Empty wiki returns
        # "" — no waste. Coexists with experience library above during
        # migration; eventually wiki replaces it.
        if total_chars < max_dynamic_chars:
            try:
                from .knowledge import get_wiki_store
                store = get_wiki_store()
                role_idx = store.render_index_for_prompt(
                    f"role:{self.role}", max_pages=20,
                )
                global_idx = store.render_index_for_prompt(
                    "global", max_pages=15,
                )
                if role_idx or global_idx:
                    parts_idx = []
                    if role_idx:
                        parts_idx.append(role_idx)
                    if global_idx:
                        parts_idx.append(global_idx)
                    _try_add("\n\n".join(parts_idx), "wiki_index")
            except Exception as _we:
                logger.debug("wiki index injection skipped: %s", _we)

        # 8. (REMOVED 2026-04-28) Granted skills used to be re-rendered
        #    here every turn via skill_registry.build_prompt_block() —
        #    ~2K tokens/turn of duplicated content. Single source of
        #    truth is now `granted_skills.md` (written in
        #    _ensure_workspace_layout, read in _get_scheduled_context).
        #    Agent reads each SKILL.md on demand via read_file or
        #    get_skill_guide(name).

        # 9. Structured context bundle via budget allocator (#4 wired-in).
        #    Replaces the ad-hoc "_build_shared_context_summary" path with
        #    the formal 5-section allocator (task_state / upstream_dependencies
        #    / decisions / knowledge / history_summary) — each section gets
        #    its own hard token budget so no single section can starve the
        #    others. Same per-agent personalization (handoffs to me, Q&A
        #    to me) is preserved.
        #
        #    Solo agents (no project_id) get an empty bundle → zero overhead.
        if total_chars < max_dynamic_chars:
            try:
                from .shared_context import get_agent_context
                # Budget for THIS section: 40% of the remaining dynamic budget
                # (caps the bundle so plan_state / intent / memory still fit).
                _remaining = max_dynamic_chars - total_chars
                _bundle_budget_chars = int(_remaining * 0.40)
                _bundle_budget_tokens = max(200, _bundle_budget_chars // 4)
                _bundle = get_agent_context(
                    agent_id=self.id,
                    project_id=getattr(self, "project_id", "") or "",
                    intent=current_query[:300] if current_query else "",
                    role=self.role or "",
                    budget=_bundle_budget_tokens,
                )
                if _bundle and _bundle.rendered:
                    _try_add(_bundle.rendered, "structured_context")
                    # Stash per-section breakdown for observability log
                    try:
                        self._dynamic_bundle_breakdown = _bundle.section_breakdown()
                    except Exception:
                        pass
            except Exception as _sce:
                logger.debug("structured context bundle failed: %s", _sce)

        if not parts:
            return ""
        result = "\n\n".join(parts)
        # ── Stash per-section sizes on the agent so the LLM-layer breakdown
        #    logger can include them. Stays in-process; not persisted.
        try:
            self._dynamic_section_sizes = dict(section_sizes)
        except Exception:
            pass
        # ── Per-section breakdown log (INFO level — this is the data needed
        #    to know which section is bloating dynamic context).
        try:
            sec_str = " ".join(
                f"{k}={v}" for k, v in sorted(
                    section_sizes.items(), key=lambda kv: -kv[1])
            )
            logger.info(
                "DYNAMIC_CONTEXT_BREAKDOWN agent=%s total=%d/%d sections[%s]",
                self.id[:8], len(result), max_dynamic_chars, sec_str,
            )
        except Exception:
            pass
        logger.debug("Dynamic context: %d chars / %d budget (%.0f%%)",
                     len(result), max_dynamic_chars,
                     len(result) / max(max_dynamic_chars, 1) * 100)
        # 顺便把"记忆 / 动态上下文实际占比"也算出来
        try:
            stats = getattr(self, "_memory_usage_stats", None)
            if stats is not None and stats.get("last_mem_chars", 0) > 0:
                stats["last_total_chars"] = len(result)
        except Exception:
            pass
        return result

    def _build_system_prompt(self) -> str:
        """Build full system prompt (backward compat — used by enable/disable methods).

        For the main chat loop, _ensure_system_message() uses the split
        static + dynamic approach instead.
        """
        static = self._build_static_system_prompt()
        dynamic = self._build_dynamic_context()
        if dynamic:
            return static + "\n\n" + dynamic
        return static

    def _get_memory_manager(self):
        """懒加载获取 MemoryManager 实例。"""
        if self._memory_manager is not None:
            return self._memory_manager
        if get_memory_manager is None:
            return None
        try:
            self._memory_manager = get_memory_manager()
            return self._memory_manager
        except Exception as e:
            logger.debug("Failed to init MemoryManager: %s", e)
            return None

    def _get_memory_consolidator(self):
        """懒加载获取 MemoryConsolidator 实例。"""
        if self._memory_consolidator is not None:
            return self._memory_consolidator
        mm = self._get_memory_manager()
        if mm is None:
            return None
        try:
            from .core.memory import MemoryConsolidator
        except ImportError:
            try:
                from app.core.memory import MemoryConsolidator
            except ImportError:
                return None
        self._memory_consolidator = MemoryConsolidator(mm)
        return self._memory_consolidator

    def _get_memory_config(self):
        """获取当前 agent 的记忆配置。"""
        mm = self._get_memory_manager()
        if mm is None or MemoryConfig is None:
            return None
        try:
            return mm.get_config(self.id)
        except Exception:
            return MemoryConfig() if MemoryConfig else None

    def _ensure_system_message(self, current_query: str = ""):
        """Ensure the system message is present AND up-to-date.

        Architecture for KV cache reuse (critical for LM Studio / Ollama):
          messages[0] = STATIC system prompt — only changes when config changes.
                        This ensures the prefix of the message array is STABLE,
                        so local inference servers can reuse their KV cache.

        Dynamic context (git, memory, experience) is NOT injected into the
        message array here.  Instead, it's injected as a transient message
        right before sending in the chat loop (see _inject_dynamic_context).
        This keeps self.messages stable between calls.
        """
        static_prompt = self._build_static_system_prompt()

        if not self.messages or self.messages[0].get("role") != "system":
            self.messages.insert(0, {"role": "system", "content": static_prompt})
        else:
            # Only update if actually changed (preserves KV cache prefix)
            if self.messages[0]["content"] != static_prompt:
                self.messages[0]["content"] = static_prompt

        # Clean up any old dynamic context messages left from previous versions
        for i in range(min(len(self.messages), 4) - 1, 0, -1):
            if self.messages[i].get("_dynamic"):
                self.messages.pop(i)

    def _build_shared_context_summary(self) -> str:
        """Per-agent shared-context block: project state + meeting state +
        handoffs/Q&A directed at me.

        Returns "" when agent is neither in a project nor in a meeting
        (so solo conversational agents pay zero overhead). Capped at
        ~1800 chars total via the underlying summary helpers.

        Fed into ``_build_dynamic_context``, which gets prepended into
        the LAST user message by ``_inject_dynamic_context`` — NOT a
        separate system message. Two reasons:
          * Avoids stacking a 4th system msg on top of static + step
            framing + verify retry (dilutes attention, confuses
            "stable rule" vs "current state" semantics).
          * KV cache stays hot — only the last user message grows.
        """
        project_id = (getattr(self, "project_id", "") or "").strip()
        meeting_id = (getattr(self, "meeting_id", "") or "").strip()
        if not project_id and not meeting_id:
            return ""
        blocks: list[str] = []
        # ─ Project summary
        if project_id:
            try:
                from .shared_context import get_shared_context_store
                store = get_shared_context_store()
                proj_md = store.project_summary_markdown(project_id)
                if proj_md:
                    blocks.append(proj_md)
                # Per-agent: handoffs to me + open questions to me
                handoffs_to_me = store.list_handoffs(
                    project_id=project_id, dst_agent=self.id,
                    status="pending", limit=5,
                )
                if handoffs_to_me:
                    lines = ["[给我的待处理 handoff]"]
                    for h in handoffs_to_me:
                        refs = ", ".join(h.get("artifact_refs") or []) or "无"
                        summary = (h.get("summary") or "").replace("\n", " ")[:80]
                        lines.append(
                            f"- 来自 {h['src_agent']}: {h['intent']}"
                            + (f"\n  refs: {refs}" if refs != "无" else "")
                            + (f"\n  备注: {summary}" if summary else "")
                        )
                    blocks.append("\n".join(lines))
                qs_to_me = store.list_pending_questions(
                    project_id=project_id, asked_to=self.id,
                    status="open", limit=3,
                )
                if qs_to_me:
                    lines = ["[等我回答的问题]"]
                    for q in qs_to_me:
                        question = (q.get("question") or "").replace("\n", " ")[:120]
                        lines.append(f"- 来自 {q.get('asked_by','?')}: {question}")
                    blocks.append("\n".join(lines))
            except Exception as e:
                logger.debug("project summary block failed: %s", e)
        # ─ Meeting summary (in-memory, ephemeral)
        if meeting_id:
            try:
                from .meeting_summary import meeting_summary_markdown
                meet_md = meeting_summary_markdown(meeting_id, viewer_agent_id=self.id)
                if meet_md:
                    blocks.append(meet_md)
            except Exception as e:
                logger.debug("meeting summary block failed: %s", e)
        return "\n\n".join(blocks)

    def _inject_dynamic_context(self, messages: list[dict], current_query: str = "") -> list[dict]:
        """Inject dynamic context by **prepending into the LAST user
        message** (NOT as a separate system msg).

        Why user-role + prepend (changed 2026-04-28):
          * Avoids stacking a 4th system msg on top of static + step
            framing + verify retry — dilutes attention, confuses
            "stable rule" vs "current state" semantics.
          * KV cache stays hot — only the LAST user message grows.
          * Dynamic context = "current environment" (user-facing
            context), not framing rules.

        Returns a new list — does NOT modify self.messages.
        """
        dynamic_ctx = self._build_dynamic_context(current_query=current_query)
        if not dynamic_ctx:
            return messages

        # Find the last user message
        last_user_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                last_user_idx = i
                break

        result = list(messages)
        if last_user_idx is None or last_user_idx <= 0:
            # No user message yet — fall back to system msg appended at end
            ctx_msg = {"role": "system", "content": dynamic_ctx, "_dynamic": True}
            result.append(ctx_msg)
            return result

        # Prepend into the last user message (copy first, never mutate).
        # Handle both string content (text-only turn) and list content
        # (multimodal: vision turns where content is a list of
        # {type:text|image_url, ...} blocks). Plain "+" between str and
        # list raises TypeError — see 2026-04-30 incident.
        last_user = dict(result[last_user_idx])
        if last_user.get("_has_dynamic_ctx"):
            return result  # idempotent
        original_content = last_user.get("content", "")
        prefix = (
            "[当前动态上下文 · 仅供参考，下方分隔线后才是我的实际请求]\n"
            + dynamic_ctx
            + "\n\n---\n\n"
        )
        if isinstance(original_content, list):
            # Multimodal: prepend dynamic context as a text block. If the
            # first block is already text, merge into it (saves one block);
            # otherwise insert a new text block at the head so images keep
            # their relative position after the prefix.
            new_content = list(original_content)
            if (new_content and isinstance(new_content[0], dict)
                    and new_content[0].get("type") == "text"):
                first = dict(new_content[0])
                first["text"] = prefix + str(first.get("text", ""))
                new_content[0] = first
            else:
                new_content = [{"type": "text", "text": prefix}] + new_content
            last_user["content"] = new_content
        else:
            # String content (or None / other) — keep original behavior.
            last_user["content"] = prefix + str(original_content or "")
        last_user["_has_dynamic_ctx"] = True
        result[last_user_idx] = last_user
        return result

    def _memory_write_back(self, user_message: str, assistant_response: str):
        """
        三层记忆 write-back:
        1. 累计轮次计数，达到阈值时将溢出的 L1 消息压缩为 L2 摘要
        2. 从对话中提取 L3 事实（异步，不阻塞主流程）
        """
        # Defensive: see agent_llm.py:_memory_write_back for context.
        user_message = _ensure_str_content(user_message)
        assistant_response = _ensure_str_content(assistant_response)

        mm = self._get_memory_manager()
        if mm is None:
            return

        mem_config = self._get_memory_config()
        if mem_config is None or not mem_config.enabled:
            return

        self._memory_turn_counter += 1

        try:
            # === L1→L2: 压缩溢出消息 ===
            if self._memory_turn_counter >= mem_config.l2_compress_threshold:
                overflow = mm.get_overflow_messages(
                    self.messages, max_turns=mem_config.l1_max_turns,
                )
                if overflow:
                    # 构建一个简单的 LLM 调用函数
                    llm_call = self._make_summary_llm_call()
                    mm.compress_to_episodic(
                        agent_id=self.id,
                        messages=overflow,
                        llm_call=llm_call,
                        turn_start=max(0, self._memory_turn_counter - len(overflow)),
                    )
                    self._log("memory", {
                        "action": "compress_to_episodic",
                        "overflow_msgs": len(overflow),
                    })
                self._memory_turn_counter = 0  # 重置计数

            # === L3: 提取事实 ===
            # Per-turn extraction was retired in favor of:
            #   1. task DONE hook (see Agent.update_task / project.py)
            #   2. strong-signal trigger (user explicitly asks to remember)
            #   3. K-turn fallback (pure-chat sessions never produce a task DONE)
            # All three paths route through app.core.l3_extractor; prompt is
            # hardcoded there, no extra_context, no tools.
            if mem_config.auto_extract_facts:
                try:
                    from .core import l3_extractor as _l3
                    _l3.note_chat_turn(self.id)
                    _lang = getattr(self.profile, "language", "auto") or "auto"
                    if _l3.has_strong_signal(user_message):
                        llm_call = self._make_summary_llm_call()
                        if llm_call:
                            facts = _l3.extract_on_strong_signal(
                                agent_id=self.id,
                                user_message=user_message,
                                assistant_response=assistant_response,
                                llm_call=llm_call,
                                store=mm,
                                lang=_lang,
                            )
                            if facts:
                                self._log("memory", {
                                    "action": "extract_facts",
                                    "trigger": "strong_signal",
                                    "count": len(facts),
                                    "facts": [f.content[:50] for f in facts[:3]],
                                })
                    elif _l3.should_run_fallback(self.id):
                        llm_call = self._make_summary_llm_call()
                        if llm_call:
                            facts = _l3.extract_fallback(
                                agent_id=self.id,
                                recent_messages=self.messages[-12:],
                                llm_call=llm_call,
                                store=mm,
                                lang=_lang,
                            )
                            if facts:
                                self._log("memory", {
                                    "action": "extract_facts",
                                    "trigger": "fallback_kturn",
                                    "count": len(facts),
                                    "facts": [f.content[:50] for f in facts[:3]],
                                })
                except Exception as _l3_err:  # noqa: BLE001
                    logger.debug("L3 extraction (chat path) failed: %s", _l3_err)

            # === Session-level action buffer flush ===
            # Aggregate buffered tool actions into a single outcome memory
            # instead of recording per-tool log entries.
            try:
                llm_call_flush = self._make_summary_llm_call()
                outcome = mm.flush_action_buffer(self.id, llm_call=llm_call_flush)
                if outcome:
                    self._log("memory", {
                        "action": "flush_action_buffer",
                        "outcome": outcome.content[:100],
                    })
            except Exception as _flush_err:
                logger.debug("flush_action_buffer failed: %s", _flush_err)

            # === L3: 记忆整理 (Consolidate) ===
            consolidator = self._get_memory_consolidator()
            if consolidator:
                llm_call = self._make_summary_llm_call()
                report = consolidator.consolidate(
                    agent_id=self.id, llm_call=llm_call)
                if not report.get("skipped"):
                    total = (report.get("plans_resolved", 0)
                             + report.get("facts_merged", 0)
                             + report.get("facts_decayed", 0)
                             + report.get("facts_deleted", 0))
                    if total > 0:
                        self._log("memory", {
                            "action": "consolidate",
                            "plans_resolved": report.get("plans_resolved", 0),
                            "facts_merged": report.get("facts_merged", 0),
                            "facts_decayed": report.get("facts_decayed", 0),
                            "facts_deleted": report.get("facts_deleted", 0),
                        })
                        parts = []
                        if report.get("plans_resolved"):
                            parts.append(f"intent→outcome={report['plans_resolved']}")
                        if report.get("facts_merged"):
                            parts.append(f"merged={report['facts_merged']}")
                        if report.get("facts_decayed"):
                            parts.append(f"decayed={report['facts_decayed']}")
                        if report.get("facts_deleted"):
                            parts.append(f"deleted={report['facts_deleted']}")
                        self.history_log.add(
                            "consolidate",
                            f"[Consolidate] 记忆整理: {', '.join(parts)}"
                        )

        except Exception as e:
            logger.debug("Memory write-back failed: %s", e)

    def _sync_enhancement_to_memory(self, learn_result):
        """将 Enhancement 自我学习的成果同步到 L3 记忆。

        Enhancement 模块有自己的 MemoryGraph，但那个只用于增强 prompt。
        我们把关键的学习成果也写入 L3，使得向量搜索能检索到 Agent 的经验。
        """
        mm = self._get_memory_manager()
        if mm is None:
            return
        try:
            # learn_result 是 MemoryNode 对象
            title = getattr(learn_result, 'title', '') or ''
            content = getattr(learn_result, 'content', '') or ''
            kind = getattr(learn_result, 'kind', '') or ''

            if not content or len(content) < 10:
                return

            # 映射 Enhancement kind → L3 category
            kind_to_category = {
                "error_fix": "learned",
                "success_pattern": "learned",
                "observation": "learned",
                "knowledge": "context",
                "rule": "rule",
            }
            category = kind_to_category.get(kind, "learned")
            fact_content = f"[自我学习] {title}: {content}" if title else f"[自我学习] {content}"

            from .core.memory import SemanticFact
            fact = SemanticFact(
                agent_id=self.id,
                category=category,
                content=fact_content[:500],
                source="enhancement:auto_learn",
                confidence=0.7,
            )
            mm.save_fact(fact)
            logger.debug("Synced enhancement learning to L3 memory: %s", title[:60])
        except Exception as e:
            logger.debug("Enhancement→memory sync failed: %s", e)

    # 高价值工具 — 这些操作值得记录到 Agent 记忆
    _MEMORY_WORTHY_TOOLS = {
        # 文件操作
        "write_file": "写入文件",
        "edit_file": "编辑文件",
        "create_file": "创建文件",
        "delete_file": "删除文件",
        # 系统操作
        "bash": "执行命令",
        "bash_exec": "执行命令",
        # MCP 调用
        "mcp_call": "MCP工具调用",
        # 通信
        "send_message": "发送消息",
        "send_email": "发送邮件",
        # 工作流
        "task_update": "更新任务",
        "plan_update": "更新计划",
        # 代码操作
        "run_code": "运行代码",
        "deploy": "部署",
    }

    def _record_tool_action(self, tool_name: str, result_str: str):
        """将 Agent 的关键工具操作记录到 L3 记忆。

        只记录修改性操作 (写文件、执行命令、发消息等)，
        不记录查询性操作 (搜索、列表、状态查询等)。
        """
        if tool_name not in self._MEMORY_WORTHY_TOOLS:
            return
        mm = self._get_memory_manager()
        if mm is None:
            return
        mem_config = self._get_memory_config()
        if mem_config is None or not mem_config.enabled:
            return
        try:
            action_label = self._MEMORY_WORTHY_TOOLS[tool_name]
            # 从结果中提取关键摘要 (首行或前100字)
            summary_line = result_str.strip().split("\n")[0][:150] if result_str else ""
            # 过滤错误结果 (不记录 DENIED、Error 等)
            if summary_line.startswith(("DENIED:", "Error:", "error:", "Failed")):
                return
            mm.record_agent_action(
                agent_id=self.id,
                action_type="tool_exec",
                tool_name=tool_name,
                summary=f"{action_label}: {tool_name}",
                details=summary_line,
            )
        except Exception as e:
            logger.debug("Failed to record tool action: %s", e)

    # ------------------------------------------------------------------
    # Memory context builder — inject top-k relevant memories into system
    # prompt as BACKGROUND; the LLM is always the one that answers. Memory
    # augments, it never substitutes for LLM reasoning.
    # ------------------------------------------------------------------

    def _build_memory_context(self, query: str, max_facts_per_cat: int = 3) -> str | None:
        """Retrieve top-k relevant memory facts and format them as a
        system-prompt context snippet.

        Returns a string to inject into the LLM's system context, or None
        if memory is disabled / no hits / no query.
        """
        if not query or not query.strip():
            return None

        mm = self._get_memory_manager()
        if mm is None:
            return None

        mem_config = self._get_memory_config()
        if mem_config is None or not mem_config.enabled:
            return None

        # ---- Pull structured progress from the active ExecutionPlan ----
        plan_summary = self._format_active_plan_summary()

        # ---- Retrieve relevant facts from L3 memory ----
        use_vector = mem_config.vector_search_enabled and mm._check_chromadb_available()
        facts_by_category: dict[str, list] = {}
        for cat in ("goal", "action_plan", "action_done", "decision", "context"):
            try:
                if use_vector:
                    facts = mm.search_facts_vector(
                        self.id, query, top_k=max_facts_per_cat, category=cat)
                else:
                    facts = mm.search_facts(
                        self.id, query, top_k=max_facts_per_cat, category=cat)
            except Exception:
                facts = []
            if facts:
                facts_by_category[cat] = facts

        # ---- P1: pull TopicMemory compiled blocks ----
        # Topics aren't query-filtered — they're "what we know about
        # this user / this agent's working style", relevant every turn.
        # Top-10 by recency × confidence (list_topics' default sort).
        topics: list = []
        try:
            topics = mm.list_topics(self.id, limit=10, touch=True)
        except Exception as _te:
            logger.debug("list_topics failed in memory ctx: %s", _te)

        if not plan_summary and not facts_by_category and not topics:
            mc = getattr(self, "_memory_hit_counts", None) or {"hits": 0, "misses": 0}
            mc["misses"] = mc.get("misses", 0) + 1
            self._memory_hit_counts = mc
            return None

        parts = [
            "<memory_context>",
            "以下是从 agent 私有记忆中检索到的与当前问题相关的背景信息。",
            "这些是【参考资料】而非【答案】：",
            "  • 仅在与用户问题直接相关时使用；",
            "  • 若与问题无关，请忽略并按你自己的理解回答；",
            "  • 禁止把整段记忆原样复述给用户；",
            "  • 回答必须基于对用户问题的真实理解，而非记忆字段的 dump。",
            "",
        ]

        if plan_summary:
            parts.append("【当前执行计划】")
            parts.append(plan_summary)
            parts.append("")

        _CAT_TITLES = {
            "goal": "目标/里程碑",
            "action_plan": "待办事项",
            "action_done": "已完成",
            "decision": "关键决策",
            "context": "项目上下文",
        }
        for cat, facts in facts_by_category.items():
            title = _CAT_TITLES.get(cat, cat)
            parts.append(f"【{title}】")
            for f in facts[:max_facts_per_cat]:
                parts.append(f"- {f.content}")
            parts.append("")

        # P1: render TopicMemory compiled blocks. These are the
        # "current best understanding" per topic — reflect lessons
        # accumulated over many turns. Notably we render ONLY compiled,
        # not timeline (timeline is for audit / explain, not steady
        # injection cost).
        if topics:
            parts.append("【主题级理解 (compiled)】")
            for t in topics:
                low_conf = " ⚠ 近期有冲突，置信度低" if t.confidence < 0.7 else ""
                parts.append(f"- [{t.topic}] {t.compiled}{low_conf}")
            parts.append("")

        parts.append("</memory_context>")
        ctx = "\n".join(parts)

        self._log("memory_context", {
            "query": query[:100],
            "plan_hit": bool(plan_summary),
            "fact_categories": list(facts_by_category.keys()),
            "fact_count": sum(len(v) for v in facts_by_category.values()),
            "chars": len(ctx),
        })

        mc = getattr(self, "_memory_hit_counts", None) or {"hits": 0, "misses": 0}
        mc["hits"] = mc.get("hits", 0) + 1
        self._memory_hit_counts = mc

        return ctx

    def _build_inbox_context(self, limit: int = 10):
        """Pull unread inbox messages for this agent and format as
        LLM system context.

        Returns ``(context_str_or_None, msg_ids_to_mark_read)``. Caller
        is responsible for calling ``mark_read`` AFTER the context has
        been committed to the LLM turn — that way a crash between build
        and commit doesn't silently drop the messages.
        """
        try:
            from .inbox import get_store
            store = get_store()
        except Exception as e:
            logger.debug("inbox context skipped (store unavailable): %s", e)
            return None, []

        try:
            msgs = store.fetch_unread(self.id, limit=max(1, int(limit)))
        except Exception as e:
            logger.debug("inbox context skipped (fetch failed): %s", e)
            return None, []

        if not msgs:
            return None, []

        from datetime import datetime as _dt
        parts = [
            "<inbox>",
            f"你有 {len(msgs)} 条未读消息（按优先级+时间排序）。"
            f"其他 agent 或用户发的。默认只读 summary+key+artifacts；"
            f"需要看原文时用 read_file(artifact) 或主动查：",
            "",
        ]
        for i, m in enumerate(msgs, 1):
            ts = ""
            try:
                ts = _dt.fromtimestamp(m.created_at).strftime("%Y-%m-%d %H:%M")
            except Exception:
                pass
            hdr = f"[{i}] from={m.from_agent} prio={m.priority} at={ts} id={m.id}"
            if m.thread_id and m.thread_id != m.id:
                hdr += f" thread={m.thread_id}"
            if m.reply_to:
                hdr += f" reply_to={m.reply_to}"
            parts.append(hdr)

            # P0-A: prefer envelope rendering over raw content. This is
            # the single biggest token saver on inbox injection —
            # rendered compact, always ≤ ~500 tokens regardless of how
            # much detail the sender stuffed into the inbox row.
            env = (m.metadata or {}).get("envelope") if m.metadata else None
            if env and isinstance(env, dict):
                summary = (env.get("summary") or "").strip()
                if summary:
                    if len(summary) > 600:
                        summary = summary[:600] + "…"
                    parts.append(f"📣 {summary}")
                kf = env.get("key_fields") or {}
                if kf:
                    try:
                        import json as _json
                        kf_txt = _json.dumps(kf, ensure_ascii=False,
                                             default=str)
                    except Exception:
                        kf_txt = str(kf)
                    if len(kf_txt) > 400:
                        kf_txt = kf_txt[:400] + "…"
                    parts.append(f"🔑 {kf_txt}")
                refs = env.get("artifact_refs") or []
                if refs:
                    parts.append(
                        "📎 " + ", ".join(refs[:5])
                        + (f" (+{len(refs)-5})" if len(refs) > 5 else "")
                        + "  → 需要内容时用 read_file(路径)"
                    )
                # If only summary+keys and no detail hint, skip the body
                # entirely. Detail lives in metadata.detail_full on
                # the inbox row for on-demand recall.
                detail_len = int(env.get("detail_len") or 0)
                if detail_len > 0:
                    parts.append(
                        f"  (详情 {detail_len} chars 保留在 inbox 元数据里，"
                        f"默认不展开以省 token；需要调 inbox 工具看原文)"
                    )
            else:
                # Legacy / no envelope: render raw content (bounded).
                body = (m.content or "").strip()
                if len(body) > 1200:
                    body = body[:1200] + "…(truncated)"
                parts.append(body)
            parts.append("")
        parts.append("</inbox>")

        ctx = "\n".join(parts)
        msg_ids = [m.id for m in msgs]
        return ctx, msg_ids

    def _build_resume_digest_context(self):
        """Pull a pending-delivery restored checkpoint for this agent
        and return (digest_text, checkpoint_id). Returns (None, "") if
        nothing is queued.

        Consumption is atomic — once this returns a digest, the
        pending flag is cleared in the store so the next chat turn
        won't re-inject the same block.
        """
        try:
            from .checkpoint import get_store as _ck_store
            from .digest import build_digest as _build_digest
            store = _ck_store()
        except Exception as e:
            logger.debug("resume digest skipped (import): %s", e)
            return None, ""

        try:
            c = store.consume_pending_resume(self.id)
        except Exception as e:
            logger.debug("resume digest skipped (consume): %s", e)
            return None, ""
        if c is None:
            return None, ""

        # Prefer a precomputed digest if present; otherwise build now.
        text = c.digest or ""
        if not text:
            try:
                r = _build_digest(c)
                text = r.text or ""
            except Exception as e:
                logger.debug("resume digest build failed: %s", e)
                text = ""
        if not text:
            return None, c.id

        wrapped = (
            "<checkpoint_resume>\n"
            f"你之前的工作已从检查点 {c.id} 恢复。以下是压缩后的历史摘要 —— "
            "请据此继续，不要重做已完成的部分：\n\n"
            f"{text}\n"
            "</checkpoint_resume>"
        )
        return wrapped, c.id

    def _format_active_plan_summary(self) -> str:
        """将当前活跃的 ExecutionPlan 格式化为可读摘要。"""
        active_plans = [p for p in self.execution_plans if p.status == "active"]
        if not active_plans:
            return ""

        plan = active_plans[-1]  # 最近的活跃计划
        progress = plan.get_progress()
        lines = [
            f"**当前任务: {plan.task_summary}**",
            f"进度: {progress['done']}/{progress['total']} "
            f"({progress['percent']}%)\n",
        ]
        for step in plan.steps:
            if step.status == StepStatus.COMPLETED:
                icon = "✅"
            elif step.status == StepStatus.IN_PROGRESS:
                icon = "🔄"
            elif step.status == StepStatus.FAILED:
                icon = "❌"
            elif step.status == StepStatus.SKIPPED:
                icon = "⏭️"
            else:
                icon = "⬜"
            line = f"{icon} {step.order + 1}. {step.title}"
            if step.result_summary:
                line += f" → {step.result_summary[:80]}"
            lines.append(line)

        return "\n".join(lines)

    def _build_checkpoint_context(self) -> str:
        """构建任务恢复上下文，注入到系统提示中。

        当 agent_phase 为 EXECUTING 或 PLANNING 时调用，
        让 Agent 知道之前做到哪了，避免重头开始。

        [F1] 过期过滤：若所有信号（active plan / action_done / action_plan）
        都早于 TUDOU_CHECKPOINT_STALE_HOURS（默认 24h），则返回空串，
        避免把一周前的任务当作"正在进行"反复复活。
        """
        import os as _os
        try:
            stale_hours = float(_os.environ.get("TUDOU_CHECKPOINT_STALE_HOURS", "24"))
        except (TypeError, ValueError):
            stale_hours = 24.0
        stale_cutoff = time.time() - stale_hours * 3600

        mm = self._get_memory_manager()
        parts = []

        # 1. 活跃计划的进度（仅当近期有活动）
        def _plan_latest_ts(p):
            ts = p.created_at
            for s in p.steps:
                if getattr(s, "completed_at", 0) and s.completed_at > ts:
                    ts = s.completed_at
                if getattr(s, "started_at", 0) and s.started_at > ts:
                    ts = s.started_at
            return ts

        fresh_active = any(
            p.status == "active" and _plan_latest_ts(p) >= stale_cutoff
            for p in self.execution_plans
        )
        if fresh_active:
            plan_summary = self._format_active_plan_summary()
            if plan_summary:
                parts.append(plan_summary)

        # 2/3. L3 facts — 只纳入 updated_at 在 cutoff 之后的条目
        if mm:
            recent_done = mm.get_recent_facts(self.id, limit=10, category="action_done")
            fresh_done = [f for f in recent_done
                          if getattr(f, "updated_at", 0) >= stale_cutoff]
            if fresh_done:
                parts.append("\n**最近完成的操作:**")
                for f in fresh_done[:10]:
                    parts.append(f"- {f.content}")

            plans_facts = mm.get_recent_facts(self.id, limit=5, category="action_plan")
            fresh_plans = [f for f in plans_facts
                           if getattr(f, "updated_at", 0) >= stale_cutoff]
            if fresh_plans:
                parts.append("\n**待办事项:**")
                for f in fresh_plans[:5]:
                    parts.append(f"- {f.content}")

        if not parts:
            return ""

        return (
            "\n<task_checkpoint>\n"
            "⚠️ 你正在继续之前的任务，以下是当前进展。\n"
            "请从断点继续，不要重复已完成的工作。\n"
            "已有文件请先检查再修改，不要重新创建。\n\n"
            + "\n".join(parts)
            + "\n</task_checkpoint>\n"
        )

    def _auto_stale_active_plans(self):
        """[F3] 自动把长时间无活动的 active plan 标记为 stale。

        防止 agent_phase 永久停留在 EXECUTING 导致每次启动/唤醒
        都触发 task_checkpoint 注入、让 LLM 反复重放同一段老任务。

        超过 TUDOU_PLAN_STALE_HOURS（默认 6h）无活动的 active plan
        → status="stale"，并联动调用 _update_agent_phase() 让阶段回落。
        """
        import os as _os
        try:
            stale_hours = float(_os.environ.get("TUDOU_PLAN_STALE_HOURS", "6"))
        except (TypeError, ValueError):
            stale_hours = 6.0
        cutoff = time.time() - stale_hours * 3600

        changed = False
        for p in self.execution_plans:
            if p.status != "active":
                continue
            latest_ts = p.created_at
            for s in p.steps:
                if getattr(s, "completed_at", 0) and s.completed_at > latest_ts:
                    latest_ts = s.completed_at
                if getattr(s, "started_at", 0) and s.started_at > latest_ts:
                    latest_ts = s.started_at
            if latest_ts < cutoff:
                p.status = "stale"
                changed = True
                try:
                    self._log("plan_auto_stale", {
                        "plan_id": p.id,
                        "task": (p.task_summary or "")[:80],
                        "age_hours": round((time.time() - latest_ts) / 3600, 2),
                    })
                except Exception:
                    pass
        if changed:
            try:
                self._update_agent_phase()
            except Exception:
                pass

    def _write_plan_to_memory(self, plan: "ExecutionPlan"):
        """将 ExecutionPlan 的里程碑/步骤写入 L3 记忆。

        在计划创建时调用，使得后续查询可以从记忆中直接获取。
        """
        mm = self._get_memory_manager()
        if mm is None:
            return
        try:
            from .core.memory import SemanticFact
        except ImportError:
            try:
                from app.core.memory import SemanticFact
            except ImportError:
                return

        try:
            # 写入目标 (goal)
            if plan.task_summary:
                mm.save_fact(SemanticFact(
                    agent_id=self.id,
                    category="goal",
                    content=f"[任务目标] {plan.task_summary}",
                    source=f"execution_plan:{plan.id}",
                    confidence=0.95,
                ))

            # 不再为每个 step 单独写 action_plan —— "步骤标题" 不是
            # reusable 知识，只会挤占 L3 top-K 检索槽位. 单个 plan 只
            # 保留一条 goal 承载任务上下文 (上方 task_summary 已写),
            # 步骤交付物通过 _write_step_completion_to_memory 的 outcome
            # 按需入库 (且有"≥12 字 + 非模板化" 门槛).
            self._log("memory", {
                "action": "plan_to_memory",
                "plan_id": plan.id,
                "steps": len(plan.steps),
                "facts_written": 1,
            })
        except Exception as e:
            logger.debug("Failed to write plan to memory: %s", e)

    def _write_step_completion_to_memory(self, plan: "ExecutionPlan",
                                          step: "ExecutionStep"):
        """写步骤完成到 L3 记忆 — 走 upsert 自带刷新语义 (新 A.5)。

        对"结论式"步骤 (含 result_summary 且长度 ≥ 12 字) 才入库；
        相同主题的重复完成会 refresh 已有条目而不是堆栈式累加。
        category 使用新 taxonomy 的 ``outcome``。
        """
        mm = self._get_memory_manager()
        if mm is None:
            return
        # Guard: skip trivial completions — "Done"/"ok"/"完成" don't teach
        # the agent anything for future recall. We want sentences the
        # agent can actually reuse later ("parsed 17 CSVs into dataset
        # X", "tests pass: 52 green 0 failed").
        summary = (step.result_summary or "").strip()
        if len(summary) < 12:
            return
        # Reject templated LLM aggregator noise — these phrases appear
        # when flush_action_buffer or step-summary LLM writes a generic
        # "N operations executed" placeholder instead of a real result.
        # Matching on normalized form so minor variations still hit.
        import re as _re
        _norm = _re.sub(r"[^\w\u4e00-\u9fa5]+", "", summary.lower())
        _TEMPLATE_NOISE = (
            "本次会话执行了",     # "本次会话执行了 N 个操作"
            "操作执行完毕",       # "[当前日期] 操作执行完毕，计划与命令均已成功"
            "最终结果未明确",     # "未提供日期，最终结果未明确"
            "未提供日期",
            "状态已更新无失败",   # "状态已更新，无失败原因"
            "无失败原因",
            "执行了该步骤",
            "步骤完成无异常",
        )
        if any(tok in _norm for tok in _TEMPLATE_NOISE):
            logger.debug(
                "Skip templated step-summary from L3 write "
                "(step=%s, summary=%r)", step.id[:8], summary[:80]
            )
            return

        try:
            from .core.memory import SemanticFact
        except ImportError:
            try:
                from app.core.memory import SemanticFact
            except ImportError:
                return

        try:
            # Compact content: title first (for retrieval anchoring),
            # then summary. Skip the timestamp header — updated_at on
            # the fact records recency for us.
            content = f"完成「{step.title}」 → {summary[:240]}"
            fact = SemanticFact(
                agent_id=self.id,
                category="outcome",      # new-taxonomy name for action_done
                content=content,
                source=f"plan:{plan.id[:10]}:step:{step.id[:8]}",
                confidence=0.9,
            )
            res = mm.upsert_fact(fact, threshold=0.75)
            self._log("memory", {
                "action": "step_done_to_memory",
                "result": res.get("action"),
                "similarity": res.get("similarity"),
                "fact_id": res.get("id"),
                "plan_id": plan.id,
                "step": step.title[:50],
            })
        except Exception as e:
            logger.debug("Failed to write step completion to memory: %s", e)

    def _update_agent_phase(self):
        """根据当前 ExecutionPlan 状态自动更新 agent_phase。"""
        active_plans = [p for p in self.execution_plans if p.status == "active"]
        if not active_plans:
            if self.agent_phase != AgentPhase.BLOCKED:
                self.agent_phase = AgentPhase.IDLE
            return

        plan = active_plans[-1]
        progress = plan.get_progress()

        if progress["total"] == 0:
            self.agent_phase = AgentPhase.PLANNING
        elif progress["done"] == progress["total"]:
            self.agent_phase = AgentPhase.REVIEWING
        elif progress["in_progress"] > 0 or progress["done"] > 0:
            self.agent_phase = AgentPhase.EXECUTING
        else:
            self.agent_phase = AgentPhase.PLANNING

    def _make_summary_llm_call(self):
        """
        构建用于记忆摘要/提取的 LLM 调用函数。
        复用当前 agent 的 provider/model 配置。
        """
        try:
            from .. import llm
        except ImportError:
            try:
                from app import llm
            except ImportError:
                return None

        _eff_provider, _eff_model = self._resolve_effective_provider_model()

        def _call(prompt: str) -> str:
            messages = [
                {"role": "system", "content": "你是一个信息提取助手，请精确按照要求的格式返回结果。"},
                {"role": "user", "content": prompt},
            ]
            resp = llm.chat_no_stream(
                messages, tools=None,
                provider=_eff_provider, model=_eff_model,
            )
            return resp.get("message", {}).get("content", "")

        return _call

    def _estimate_token_count(self) -> int:
        """Estimate total token count of current messages (rough: 1 token ≈ 4 chars for CJK, 4 chars for EN)."""
        total = 0
        for m in self.messages:
            content = _ensure_str_content(m.get("content"))
            if content:
                total += max(len(content) // 3, len(content.split()))
            # tool_calls in message also count
            tc = m.get("tool_calls", [])
            if tc:
                total += len(json.dumps(tc, ensure_ascii=False)) // 4
        return total

    def _get_context_limit(self) -> int:
        """Get the context window token limit based on model.

        Priority:
        1. Provider's configured context_length (if > 0)
        2. Model name heuristic
        3. Default 4096 (safe for local models like LM Studio)
        """
        # Check provider: explicit config or auto-detected from server
        try:
            reg = llm.get_registry()
            if self.provider:
                entry = reg.get(self.provider)
                if entry:
                    if entry.context_length > 0:
                        return entry.context_length
                    # Try auto-detect from the server API
                    detected = llm.detect_context_length(entry, model=model)
                    if detected > 0:
                        entry.context_length = detected  # Cache for future calls
                        return detected
        except Exception:
            pass

        # Heuristic based on model name
        model = (self.model or "").lower()
        if "128k" in model:
            return 128000
        if "32k" in model:
            return 32000
        if "claude" in model:
            return 200000
        if "gpt-4" in model:
            return 128000
        if "gpt-3.5" in model:
            return 16000
        # Local models: infer from model name or use a sensible default.
        # Users can override via provider.context_length for exact control.
        if "qwen3" in model or "qwen2.5" in model:
            return 32768  # Qwen 3/2.5 support 32k+ natively
        if "qwen" in model:
            return 8192
        if "deepseek" in model:
            return 16384
        if "llama" in model or "mistral" in model or "gemma" in model:
            return 8192
        return 8192  # safe default for most modern local models

    def _llm_summarize_context(self, messages_to_compress: list) -> str | None:
        """
        Call LLM to generate a structured summary of conversation turns.

        Serializes messages with labeled format, then uses the agent's own
        LLM provider/model to generate a structured summary with Goal/Progress/
        Decisions/Files/Next Steps sections. Handles iterative updates if a
        previous summary exists.

        Args:
            messages_to_compress: List of message dicts to summarize

        Returns:
            Summary string with prefix, or None if LLM call fails
        """
        import time as time_module

        # Check cooldown: don't retry for 10 minutes if previous attempt failed
        now = time_module.time()
        if now < self._compression_cooldown:
            logger.debug("Context summarization in cooldown (%.0fs remaining)",
                        self._compression_cooldown - now)
            return None

        # Serialize messages into labeled text format
        parts = []
        for msg in messages_to_compress:
            role = msg.get("role", "unknown")
            content = _ensure_str_content(msg.get("content"))

            # Tool results: keep significant detail (up to 2000 chars)
            if role == "tool":
                tool_id = msg.get("tool_call_id", "")
                if len(content) > 2000:
                    content = content[:1200] + "\n...[truncated]...\n" + content[-600:]
                parts.append(f"[TOOL RESULT {tool_id}]: {content}")
                continue

            # Assistant messages: include tool call names and truncated arguments
            if role == "assistant":
                if len(content) > 2000:
                    content = content[:1200] + "\n...[truncated]...\n" + content[-600:]
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    tc_parts = []
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            fn = tc.get("function", {})
                            name = fn.get("name", "?")
                            args = fn.get("arguments", "")
                            if len(args) > 300:
                                args = args[:250] + "..."
                            tc_parts.append(f"  {name}({args})")
                        else:
                            fn = getattr(tc, "function", None)
                            name = getattr(fn, "name", "?") if fn else "?"
                            tc_parts.append(f"  {name}(...)")
                    content += "\n[Tool calls:\n" + "\n".join(tc_parts) + "\n]"
                parts.append(f"[ASSISTANT]: {content}")
                continue

            # User and other roles
            if len(content) > 2000:
                content = content[:1200] + "\n...[truncated]...\n" + content[-600:]
            parts.append(f"[{role.upper()}]: {content}")

        content_to_summarize = "\n\n".join(parts)

        # Build prompt: iterative update if previous summary exists
        if self._previous_compression_summary:
            prompt = f"""You are updating a context compression summary. A previous compaction produced the summary below. New conversation turns have occurred and need to be incorporated.

PREVIOUS SUMMARY:
{self._previous_compression_summary}

NEW TURNS TO INCORPORATE:
{content_to_summarize}

Update the summary using this exact structure. PRESERVE all existing information that is still relevant. ADD new progress. Move items from "In Progress" to "Done" when completed. Remove information only if clearly obsolete.

## Goal
[What the user is trying to accomplish — preserve from previous summary, update if goal evolved]

## Constraints & Preferences
[User preferences, coding style, constraints, important decisions — accumulate across compressions]

## Progress
### Done
[Completed work — include specific file paths, commands run, results obtained]
### In Progress
[Work currently underway]
### Blocked
[Any blockers or issues encountered]

## Key Decisions
[Important technical decisions and why they were made]

## Relevant Files
[Files read, modified, or created — with brief note on each. Accumulate across compressions.]

## Next Steps
[What needs to happen next to continue the work]

## Critical Context
[Any specific values, error messages, configuration details, or data that would be lost without explicit preservation]

Target ~2000 tokens. Be specific — include file paths, command outputs, error messages, and concrete values.

Write only the summary body. Do not include any preamble or prefix."""
        else:
            prompt = f"""Create a structured handoff summary for a later assistant that will continue this conversation after earlier turns are compacted.

TURNS TO SUMMARIZE:
{content_to_summarize}

Use this exact structure:

## Goal
[What the user is trying to accomplish]

## Constraints & Preferences
[User preferences, coding style, constraints, important decisions]

## Progress
### Done
[Completed work — include specific file paths, commands run, results obtained]
### In Progress
[Work currently underway]
### Blocked
[Any blockers or issues encountered]

## Key Decisions
[Important technical decisions and why they were made]

## Relevant Files
[Files read, modified, or created — with brief note on each]

## Next Steps
[What needs to happen next to continue the work]

## Critical Context
[Any specific values, error messages, configuration details, or data that would be lost without explicit preservation]

Target ~2000 tokens. Be specific — include file paths, command outputs, error messages, and concrete values.

Write only the summary body. Do not include any preamble or prefix."""

        try:
            # Get effective provider/model
            _eff_provider, _eff_model = self._resolve_effective_provider_model()

            # Call LLM summarization
            response = llm.chat_no_stream(
                messages=[{"role": "user", "content": prompt}],
                provider=_eff_provider,
                model=_eff_model,
                max_tokens=4000,
            )

            # Extract content from response
            summary = response.get("content", "").strip() if isinstance(response, dict) else ""
            if not summary and hasattr(response, "choices"):
                # Handle structured response object
                summary = response.choices[0].message.content if response.choices else ""

            if not summary:
                logger.warning("LLM summarization returned empty content")
                return None

            # Store for iterative updates on next compression
            self._previous_compression_summary = summary
            self._compression_cooldown = 0.0

            # Add prefix for context
            prefix = (
                "[CONTEXT COMPACTION] Earlier turns in this conversation were compacted "
                "to save context space. The summary below describes work that was "
                "already completed, and the current session state may still reflect "
                "that work (for example, files may already be changed). Use the summary "
                "and the current state to continue from where things left off, and "
                "avoid repeating work:"
            )
            return f"{prefix}\n{summary}"

        except Exception as e:
            # Set cooldown: don't retry for 600 seconds (10 minutes)
            self._compression_cooldown = now + 600.0
            logger.warning(
                "Failed to generate context summary: %s. "
                "Further summary attempts paused for 600 seconds.",
                e,
            )
            return None

    def _compress_context(self):
        """
        LLM-powered context compression: when token usage exceeds 50% of context limit,
        compress earlier conversation turns using structured LLM summarization.

        Algorithm:
        1. Check if compression is needed (50% threshold, not 70%)
        2. Separate messages: system (preserve), head (first 2 exchanges), tail (last 20 or ~30%),
           middle (everything else to compress)
        3. Pre-pass: prune old tool results >200 chars to placeholder
        4. Call _llm_summarize_context() for structured summary
        5. Fall back to text-join approach if LLM fails
        6. Sanitize tool_call/tool_result pairs after compression
        """
        token_count = self._estimate_token_count()
        context_limit = self._get_context_limit()
        relative_threshold = int(context_limit * 0.5)  # 50% of context limit
        # Absolute cap: regardless of context_limit, once tokens exceed this
        # we always compress. Prevents "context_limit=128k + agent at 40k"
        # from staying uncompressed (40k < 50%*128k = 64k, which is already
        # very expensive per LLM call).
        absolute_threshold = int(
            getattr(self.profile, "compress_tokens_absolute", 0) or 30000
        )
        threshold = min(relative_threshold, absolute_threshold)

        if token_count <= threshold:
            return  # Below threshold, no compression needed

        self.history_log.add("context_compress",
                             f"tokens={token_count} limit={context_limit} threshold={threshold} (rel={relative_threshold}, abs={absolute_threshold})")

        # Separate system message
        system_msg = None
        non_system = self.messages
        if self.messages and self.messages[0].get("role") == "system":
            system_msg = self.messages[0]
            non_system = self.messages[1:]

        if len(non_system) <= 6:  # Need at least head + tail + middle
            return  # Too few messages to compress

        # Calculate boundary: head (first 2 exchanges = ~4 msgs), tail (last 20 or ~30%)
        head_count = min(4, len(non_system) // 3)  # First 2 user-assistant pairs
        tail_count = max(20, len(non_system) * 3 // 10)  # Last ~30% or 20, whichever is more

        if head_count + tail_count >= len(non_system) - 1:
            return  # Not enough middle to compress

        head = non_system[:head_count]
        tail = non_system[-(tail_count):]
        to_compress = non_system[head_count:-(tail_count)]

        # Phase 1: CHEAP pre-pass - prune old tool results >200 chars
        pruned_compress = []
        pruned_count = 0
        for msg in to_compress:
            if msg.get("role") == "tool":
                content = _ensure_str_content(msg.get("content"))
                if len(content) > 200 and content != "[Tool output cleared to save context]":
                    pruned_count += 1
                    pruned_compress.append({
                        **msg,
                        "content": "[Tool output cleared to save context]"
                    })
                else:
                    pruned_compress.append(msg)
            else:
                pruned_compress.append(msg)

        if pruned_count > 0:
            logger.debug("Pre-compression: pruned %d old tool result(s)", pruned_count)

        # Phase 2: Try LLM-powered summarization
        summary = self._llm_summarize_context(pruned_compress)

        # Phase 3: Fall back to text-join if LLM failed
        if summary is None:
            logger.debug("LLM summarization unavailable, falling back to text-join approach")
            summary_parts = []
            for m in pruned_compress:
                role = m.get("role", "unknown")
                content = _ensure_str_content(m.get("content"))
                if content.strip():
                    preview = content.replace("\n", " ").strip()
                    if len(preview) > 500:
                        preview = preview[:500] + "..."
                    if role == "user":
                        summary_parts.append(f"[User] {preview}")
                    elif role == "assistant":
                        summary_parts.append(f"[Assistant] {preview}")
                    elif role == "tool":
                        summary_parts.append(f"[Tool Result] {preview[:250]}")

            summary = (
                f"[Context Compressed: {len(pruned_compress)} messages summarized]\n"
                f"--- Earlier Conversation Summary ---\n"
                + "\n".join(summary_parts)
                + "\n--- End Summary ---"
            )

        summary_msg = {"role": "user", "content": summary}

        # Phase 4: Rebuild message list
        self.messages = (
            ([system_msg] if system_msg else [])
            + head
            + [summary_msg]
            + tail
        )

        # Phase 5: Sanitize tool_call/tool_result pairs
        self._sanitize_tool_pairs()

        new_token_count = self._estimate_token_count()
        self.history_log.add("context_compressed",
                             f"removed={len(to_compress)} msgs, "
                             f"tokens: {token_count} -> {new_token_count}")
        self._log("status", {
            "action": "context_compressed",
            "removed_messages": len(to_compress),
            "tokens_before": token_count,
            "tokens_after": new_token_count,
        })

    def _sanitize_tool_pairs(self):
        """
        Fix orphaned tool_call / tool_result pairs after compression.

        Two failure modes:
        1. A tool result references a call_id whose assistant tool_call was removed.
           The API rejects this: "No tool call found for function call output with call_id ..."
        2. An assistant message has tool_calls whose results were dropped.
           The API rejects because every tool_call must have a matching tool result.

        Removes orphaned results and inserts stub results for orphaned calls.
        """
        # Collect surviving tool call IDs
        surviving_call_ids = set()
        for msg in self.messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    cid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "")
                    if cid:
                        surviving_call_ids.add(cid)

        # Collect existing tool result IDs
        result_call_ids = set()
        for msg in self.messages:
            if msg.get("role") == "tool":
                cid = msg.get("tool_call_id")
                if cid:
                    result_call_ids.add(cid)

        # 1. Remove tool results with no matching tool_call
        orphaned_results = result_call_ids - surviving_call_ids
        if orphaned_results:
            self.messages = [
                m for m in self.messages
                if not (m.get("role") == "tool" and m.get("tool_call_id") in orphaned_results)
            ]
            logger.debug("Sanitizer: removed %d orphaned tool result(s)", len(orphaned_results))

        # 2. Add stub results for tool_calls with no result
        missing_results = surviving_call_ids - result_call_ids
        if missing_results:
            patched = []
            for msg in self.messages:
                patched.append(msg)
                if msg.get("role") == "assistant":
                    for tc in msg.get("tool_calls") or []:
                        cid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "")
                        if cid in missing_results:
                            patched.append({
                                "role": "tool",
                                "content": "[Result from earlier conversation — see context summary above]",
                                "tool_call_id": cid,
                            })
            self.messages = patched
            logger.debug("Sanitizer: added %d stub tool result(s)", len(missing_results))

    def _trim_context(self):
        """
        上下文管理（三层记忆增强版）：
        1. 如果启用了三层记忆，使用 L1 窗口裁剪（只保留最近 N 轮）
        2. 否则回退到原有的消息数量限制
        3. 按 token 用量 70% 阈值智能压缩（兜底）
        """
        # Phase 0: Memory-aware L1 windowing
        mm = self._get_memory_manager()
        mem_config = self._get_memory_config()
        if mm and mem_config and mem_config.enabled:
            # 使用 L1 窗口：只保留最近 N 轮 + system 消息
            l1_messages = mm.get_l1_messages(
                self.messages, max_turns=mem_config.l1_max_turns,
            )
            if l1_messages and len(l1_messages) < len(self.messages):
                self.messages = l1_messages
                self._log("memory", {
                    "action": "l1_window",
                    "kept": len(l1_messages),
                    "max_turns": mem_config.l1_max_turns,
                })
        else:
            # Phase 1: legacy message count limit
            max_msgs = self.profile.max_context_messages
            if max_msgs > 0 and len(self.messages) > max_msgs + 1:
                system = self.messages[0] if self.messages[0].get("role") == "system" else None
                trimmed = self.messages[-(max_msgs):]
                self.messages = ([system] if system else []) + trimmed

        # Phase 2: token-based compression at 70% (safety net)
        self._compress_context()

    # ---- tool execution with policy check ----

    # ── P0-B: auto-spill large tool results ─────────────────────────
    # Tool outputs over this many chars get written to
    # $workspace/tool_outputs/<ts>_<name>.md and replaced with a compact
    # ref + preview in the conversation. Agent can still read full
    # content via read_file(<path>) when needed.
    #
    # Threshold history:
    #   1500   (initial)  — ROOT CAUSE of the read_file death-loop in
    #                       小专 audit (30+ identical reads on a 5K
    #                       outline). 5K results got spilled, agent
    #                       re-read the spill file, that read also
    #                       got spilled, infinite bounce.
    #   20000  (interim)  — still too aggressive; routine 10-50K results
    #                       (web_fetch HTML, large docs extracted from
    #                       PDFs/PPTs) got spilled, forcing follow-up
    #                       read_file on the spill file.
    #   262144 (current)  — 256K chars (~64K tokens). Only spill when
    #                       a single result genuinely won't fit in a
    #                       reasonable LLM context budget. Most
    #                       day-to-day tool outputs flow inline.
    _SPILL_TOOL_RESULT_THRESHOLD: int = 262144   # 256K chars
    # Preview chars when we DO spill — 4K is enough for a meaningful
    # summary so the LLM rarely needs to read the full file.
    _SPILL_PREVIEW_CHARS: int = 4000

    # Tools whose outputs we never spill — usually because the full body
    # is itself the output the LLM must reason on (small JSON ACKs) or
    # because the tool already returns a file path (so spilling a path
    # is pointless).
    _SPILL_SKIP_TOOLS: frozenset = frozenset({
        "plan_update", "complete_step",
        "check_inbox", "ack_message", "reply_message",
        "memory_recall",
        "save_experience",
        # Pack v2: knowledge_lookup returns capped RAG chunks (top_k×1500
        # chars ≤ ~12KB). Spilling this hides citations from the LLM and
        # forces an extra read_file round-trip just to aggregate across
        # chunks. Keep the full result inline so the LLM can reason once.
        "knowledge_lookup",
    })

    # Max age of files in tool_outputs/ before _cleanup_stale_tool_outputs
    # deletes them. 7 days = long enough for debug, short enough that the
    # directory doesn't grow forever.
    _SPILL_MAX_AGE_SECONDS: int = 7 * 24 * 3600
    # Hard cap on per-agent file count — if this many pile up, prune the
    # oldest first regardless of age. Prevents runaway loops from filling
    # the disk before the 7-day timer fires.
    _SPILL_MAX_FILES: int = 200

    def cleanup_stale_tool_outputs(self, max_age_seconds: int | None = None,
                                   max_files: int | None = None) -> dict:
        """Prune {workspace}/tool_outputs/ entries. Safe to call anytime.

        Deletes files whose mtime is older than ``max_age_seconds``, then
        if still above ``max_files`` deletes the oldest until the count
        is back under the cap. Returns {"deleted_stale", "deleted_cap",
        "remaining"}.
        """
        import os as _os
        import time as _time
        deleted_stale = 0
        deleted_cap = 0
        remaining = 0
        try:
            ws = self._get_agent_workspace()
        except Exception:
            return {"deleted_stale": 0, "deleted_cap": 0, "remaining": 0}
        if not ws:
            return {"deleted_stale": 0, "deleted_cap": 0, "remaining": 0}
        out_dir = _os.path.join(str(ws), "tool_outputs")
        if not _os.path.isdir(out_dir):
            return {"deleted_stale": 0, "deleted_cap": 0, "remaining": 0}
        age_limit = int(max_age_seconds if max_age_seconds is not None
                        else self._SPILL_MAX_AGE_SECONDS)
        cap = int(max_files if max_files is not None
                  else self._SPILL_MAX_FILES)
        now = _time.time()
        try:
            entries = []
            for name in _os.listdir(out_dir):
                fp = _os.path.join(out_dir, name)
                if not _os.path.isfile(fp):
                    continue
                try:
                    mtime = _os.path.getmtime(fp)
                except OSError:
                    continue
                entries.append((fp, mtime))
            # Pass 1: age-based prune
            for fp, mtime in entries:
                if now - mtime > age_limit:
                    try:
                        _os.remove(fp)
                        deleted_stale += 1
                    except OSError:
                        pass
            # Pass 2: cap-based prune (oldest first)
            remaining_entries = [(fp, m) for fp, m in entries
                                 if _os.path.exists(fp)]
            if len(remaining_entries) > cap:
                remaining_entries.sort(key=lambda t: t[1])  # oldest first
                over = len(remaining_entries) - cap
                for fp, _m in remaining_entries[:over]:
                    try:
                        _os.remove(fp)
                        deleted_cap += 1
                    except OSError:
                        pass
            remaining = sum(1 for n in _os.listdir(out_dir)
                            if _os.path.isfile(_os.path.join(out_dir, n)))
        except OSError as e:
            logger.debug("cleanup_stale_tool_outputs failed: %s", e)
        return {"deleted_stale": deleted_stale,
                "deleted_cap": deleted_cap,
                "remaining": remaining}

    def _maybe_spill_tool_result(self, tool_name: str,
                                 result_str: str,
                                 call_id: str) -> str:
        """Return a content string suitable for appending as the tool
        message. Large results are spilled to disk + replaced by a
        compact ref. Idempotent on already-spilled content.
        """
        import os as _os
        if not isinstance(result_str, str):
            return result_str
        # Honour agent-level override if the profile carries one.
        threshold = int(
            getattr(self.profile, "spill_tool_result_chars", 0)
            or self._SPILL_TOOL_RESULT_THRESHOLD
        )
        if len(result_str) < threshold:
            return result_str
        if tool_name in self._SPILL_SKIP_TOOLS:
            return result_str
        # Heuristic: if the result already looks like a spill or a
        # "file saved" ack, leave it alone.
        first80 = result_str.lstrip()[:80].lower()
        if first80.startswith("[artifact:") or first80.startswith("[spilled:"):
            return result_str
        # Fix (Nov 2026): read_file against an already-spilled file would
        # re-spill the same content (same header + body), creating a
        # runaway cascade of near-duplicate tool_outputs files. Detect by
        # the spill header marker we write in _spill body below.
        first_line = result_str.lstrip().split("\n", 1)[0]
        if first_line.startswith("# Spilled tool result"):
            return result_str

        try:
            ws = self._get_agent_workspace()
        except Exception:
            ws = ""
        if not ws:
            return result_str

        try:
            out_dir = _os.path.join(str(ws), "tool_outputs")
            _os.makedirs(out_dir, exist_ok=True)
            # Lazy cleanup: every ~50th spill, sweep stale files + enforce
            # the per-agent file cap. Keeps the directory from growing
            # forever without a background job.
            try:
                import random as _random
                if _random.randint(0, 49) == 0:
                    self.cleanup_stale_tool_outputs()
            except Exception:
                pass
            ts = time.strftime("%Y%m%d_%H%M%S")
            # Sanitize tool_name for filename.
            safe = "".join(c if c.isalnum() or c in "-_" else "_"
                           for c in (tool_name or "tool"))[:40]
            # Add call_id tail so repeated same-tool calls don't collide.
            fname = f"{ts}_{safe}_{(call_id or '')[:6]}.md"
            path = _os.path.join(out_dir, fname)
            body = (
                f"# Spilled tool result — {tool_name}\n"
                f"# timestamp: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"# call_id:   {call_id}\n"
                f"# size:      {len(result_str)} chars\n"
                f"# agent:     {self.id} ({self.name})\n"
                f"# ─────────────────────────────────────\n\n"
                f"{result_str}\n"
            )
            with open(path, "w", encoding="utf-8") as f:
                f.write(body)
        except Exception as e:
            logger.debug("spill tool_result failed: %s", e)
            return result_str

        # Build compact replacement. Keep ONLY:
        #   1. Line 1: machine-parseable ref header
        #   2. Line 2: plain-text pointer the LLM reads
        #   3. Line 3+: 300-char preview so the LLM still has the gist
        preview = result_str.replace("\n", " ⏎ ")[: self._SPILL_PREVIEW_CHARS]
        if len(result_str) > self._SPILL_PREVIEW_CHARS:
            preview += "…"
        try:
            rel = _os.path.relpath(path, str(ws))
        except Exception:
            rel = path
        kb = max(1, len(result_str) // 1024)
        replacement = (
            f"[Artifact: path={rel} size={kb}KB tool={tool_name} "
            f"call_id={(call_id or '')[:8]}]\n"
            f"Full output spilled to disk — use read_file('{path}') "
            f"if you need details.\n\n"
            f"Preview:\n{preview}"
        )

        # Emit a progress frame so the UI can render the artifact.
        try:
            from .progress_bus import get_bus, ProgressFrame
            get_bus().publish(ProgressFrame(
                kind="tool_result_spilled",
                channel=f"agent:{self.id}",
                agent_id=self.id,
                data={
                    "tool": tool_name,
                    "path": path,
                    "size_chars": len(result_str),
                    "call_id": call_id,
                },
            ))
        except Exception:
            pass

        self._log("tool_result_spilled", {
            "tool": tool_name,
            "path": rel,
            "size_chars": len(result_str),
            "call_id": call_id,
        })
        return replacement

    # ── P1-C / P1-D: agent tier → default output budget ─────────────
    # A pragmatic mapping used ONLY when profile.max_output_tokens is
    # unset and the agent declares a tier. Overridable via profile.
    #   thinker    — plans/regards/explains; medium budget
    #   actor      — fires tools; short budget
    #   summarizer — produces compact outputs; very short budget
    _TIER_DEFAULT_OUTPUT_BUDGET: ClassVar[dict[str, int]] = {
        "thinker": 1800,
        "actor": 400,
        "summarizer": 250,
    }

    @classmethod
    def _tier_default_output_budget(cls, tier: str) -> int:
        if not tier:
            return 0
        return cls._TIER_DEFAULT_OUTPUT_BUDGET.get(tier.strip().lower(), 0)

    # ── Default minimal tool set ────────────────────────────────────
    # Used when profile.allowed_tools is empty AND the role preset also
    # has no allowed_tools. Sized to cover read / write / search / plan
    # / test / basic web / coordination — the 80% case. Avoids the old
    # "empty ⇒ all 40+ tools" behavior that blew LLM payloads past 12k
    # tokens of schema every turn.
    # Min distinctive tokens (≥5 chars, Latin/CJK) from a memory's
    # preview that must appear inside the final assistant reply for
    # the memory to count as "used". Bigram similarity leaked too many
    # false positives on generic English prose; word-overlap is stricter.
    _MEMORY_USED_MIN_MATCHES: int = 1
    _MEMORY_USED_MIN_TOKEN_LEN: int = 5
    # Recognised token: ≥ N Latin-letter-run OR ≥ 2 consecutive CJK chars.
    # Use the bash-analyzer's `re` alias-free import by lazy-compiling.
    _MEMORY_USED_TOKEN_RE = None  # compiled on first use (re_handoff)

    @classmethod
    def _extract_usage_tokens(cls, s: str) -> set[str]:
        """Lowercase set of distinctive content tokens in ``s``."""
        if not s:
            return set()
        if cls._MEMORY_USED_TOKEN_RE is None:
            cls._MEMORY_USED_TOKEN_RE = _re_handoff.compile(
                r"[A-Za-z][A-Za-z0-9_\-]{4,}|[\u4e00-\u9fff]{2,}"
            )
        return {m.group(0).lower()
                for m in cls._MEMORY_USED_TOKEN_RE.finditer(s)}

    @classmethod
    def _filter_memory_refs_by_usage(cls, refs: list[dict],
                                     final_content: str) -> list[dict]:
        """Keep only memory refs whose ``content_preview`` shares enough
        distinctive tokens with the final assistant reply.

        Rationale: we must NOT attach the 🧠 delete-this-memory button
        when the agent just recalled memory but answered from fresh
        sources — the user would otherwise delete a memory that wasn't
        actually on screen. Word-level overlap on 5+ char tokens gives
        precision-first behaviour (generic "uses/this/with/the" don't
        count, "pytest/terraform/conftest.py" do).

        Conservative guards:
          * final_content < 30 chars: too short to be an "answer".
          * ref with no content_preview: can't judge → skip.
          * ≥ ``_MEMORY_USED_MIN_MATCHES`` tokens must overlap.

        Returns a new list; never mutates the input.
        """
        text = (final_content or "").strip()
        if len(text) < 30 or not refs:
            return []
        text_tokens = cls._extract_usage_tokens(text)
        if not text_tokens:
            return []

        kept: list[dict] = []
        for r in refs:
            prev = (r or {}).get("content_preview") or ""
            if not prev:
                continue
            prev_tokens = cls._extract_usage_tokens(prev)
            if not prev_tokens:
                continue
            matched = prev_tokens & text_tokens
            if len(matched) >= cls._MEMORY_USED_MIN_MATCHES:
                entry = dict(r)
                entry["used_similarity"] = round(
                    len(matched) / max(1, len(prev_tokens)), 3)
                entry["matched_tokens"] = sorted(matched)[:5]
                kept.append(entry)
        return kept

    _MINIMAL_DEFAULT_TOOLS: tuple[str, ...] = (
        "read_file", "write_file", "edit_file",
        "search_files", "glob_files",
        "bash", "run_tests",
        "web_search", "web_fetch",
        "plan_update", "complete_step",
        "get_skill_guide",
        # Memory tools — cheap way to check "have I seen this?" before
        # expensive web lookups. memory_recall: per-agent L3 semantic;
        # knowledge_lookup: global shared knowledge wiki;
        # save_experience: write retrospective to role's experience lib.
        "memory_recall", "knowledge_lookup", "save_experience",
    )

    def _get_effective_tools(self) -> list[dict]:
        """Filter tool definitions based on profile + role preset + global
        denylist.

        Filters applied in order:
          0. RAG-only hard route (chat-header "RAG" toggle)  ← NEW
          1. profile.allowed_tools OR role_preset.allowed_tools OR
             MINIMAL_DEFAULT_TOOLS  (INTERSECT — only these ship to LLM)
          2. profile.denied_tools  (per-agent deny)
          3. GLOBAL denylist (admin-managed)
          4. capability-skill tier filter

        The "empty list ⇒ all tools" behavior was explicitly removed:
        too many agents ship with default-empty allowed_tools and end up
        sending 40 tool schemas (~12k tokens) per LLM call. Now an empty
        list means "use preset defaults or the minimal safe set".

        NOT narrowed by meeting/project scope — those contexts are for
        conversation, but users still expect the agent to be able to
        produce a pptx / send an email when asked mid-meeting. Scope-
        level tool restriction should be done explicitly via the global
        denylist UI, not implicitly.
        """
        all_tools = tools.get_tool_definitions()

        # ── RAG-only hard route ──
        # When the chat request flagged `rag_only=True` (chat-header
        # 🔍 RAG toggle on), restrict tool exposure to ``knowledge_lookup``
        # ONLY. Skips every other filter below — the LLM literally sees
        # one tool, so bash / read_file loops are architecturally
        # impossible. Defensive: if knowledge_lookup happens to be
        # denied by profile, fall through to the normal path rather
        # than returning an empty tools list (which would lock the
        # chat up).
        if getattr(self, "_rag_only_mode", False):
            denied_now = set(self.profile.denied_tools or [])
            if "knowledge_lookup" not in denied_now:
                kb = [t for t in all_tools
                      if t.get("function", {}).get("name") == "knowledge_lookup"]
                if kb:
                    return kb

        allowed = list(self.profile.allowed_tools or [])
        denied = set(self.profile.denied_tools)

        # ── resolve the effective allow-list ──
        if not allowed:
            # Tier 1: inherit from role_preset.allowed_tools if configured.
            try:
                preset = getattr(self, "_role_preset_v2", None)
                if preset is None:
                    from .role_preset_registry import get_registry
                    preset = get_registry().get(self.role or "") if self.role else None
                if preset is not None:
                    allowed = list(getattr(preset, "allowed_tools", []) or [])
            except Exception:
                pass
        if not allowed:
            # Tier 2: hard fallback to the minimal safe set.
            allowed = list(self._MINIMAL_DEFAULT_TOOLS)
            logger.debug(
                "agent %s has no allowed_tools and no role preset defaults; "
                "using MINIMAL_DEFAULT_TOOLS (%d tools)",
                self.id, len(allowed),
            )

        # Add infra tools to allowed set so their schemas also ship to LLM
        # (otherwise LLM doesn't know they exist and never calls them).
        # Matches the bypass in _execute_tool_with_policy above.
        _INFRA_TOOLS_SCHEMA = frozenset({
            "memory_recall", "knowledge_lookup", "save_experience",
            "get_skill_guide", "plan_update", "complete_step",
        })
        allowed_set = set(allowed) | _INFRA_TOOLS_SCHEMA
        all_tools = [t for t in all_tools
                     if t["function"]["name"] in allowed_set]

        if denied:
            all_tools = [t for t in all_tools
                         if t["function"]["name"] not in denied]

        # Global denylist — admin-level deny that affects every agent.
        # Lives on AuthManager.tool_policy.global_denylist (ToolPolicy).
        # Previously ONLY enforced at call time; tool schema still
        # shipped to the LLM, wasting ~750 tok per denied tool.
        try:
            from .auth import get_auth
            auth = get_auth()
            policy = getattr(auth, "tool_policy", None)
            g_denied = set(getattr(policy, "global_denylist", None) or ())
            if g_denied:
                all_tools = [t for t in all_tools
                             if t["function"]["name"] not in g_denied]
        except Exception:
            pass

        # Capability-skill tier filter — keeps a tool iff it is CORE or
        # its gating capability skill is in agent.granted_skills. This
        # is the main token-saving lever: a fresh meeting agent with
        # zero capability skills granted drops from 35 tools (~22k tok)
        # to ~19 core tools (~9k tok). Admins grant capability skills
        # per-agent via Portal UI to unlock specific tool bundles.
        try:
            from .tool_capabilities import filter_tools_by_capability
            all_tools = filter_tools_by_capability(
                all_tools, self.granted_skills)
        except Exception:
            # Fail open: better to expose all tools than hide legit
            # ones if the classification module has a bug.
            pass

        return all_tools

    def _message_is_multimodal(self, user_message: Any) -> bool:
        """Detect whether the pending user message contains vision/audio parts."""
        try:
            if isinstance(user_message, list):
                for part in user_message:
                    if isinstance(part, dict):
                        t = str(part.get("type", "")).lower()
                        if t in ("image", "image_url", "input_image",
                                 "audio", "input_audio"):
                            return True
            if isinstance(user_message, dict):
                content = user_message.get("content")
                if isinstance(content, list):
                    return self._message_is_multimodal(content)
        except Exception:
            pass
        return False

    def _resolve_effective_provider_model(self, user_message: Any = None) -> tuple[str, str]:
        """Re-resolve provider/model from registry before each LLM call.

        Resolution order (top → bottom; first non-empty wins):

          1. Per-task explicit override (``current_task.provider/model``)
          2. ★ V2 LLMRouter slot pick (5 slots: default/analysis/reasoning/
             coding/multimodal). Reads from agent's V1 fields via
             ``slots_from_v1_agent`` + signals from the user_message and
             current plan step. Replaces the legacy ``extra_llms`` label
             routing + ``auto_route`` score table — same contract, single
             entry point.
          3. ``agent.provider/model`` (the configured primary)
          4. Global default from ``config.yaml``

        Step 2 is opt-out: setting ``TUDOU_DISABLE_LLM_ROUTER=1`` skips
        the router and falls straight to step 3 — useful as a kill switch
        if a routing bug surfaces in prod.
        """
        # Per-task override takes top priority — task A uses LLM A, task B uses LLM B.
        ct = self._current_task
        if ct is not None and (getattr(ct, "provider", "") or getattr(ct, "model", "")):
            prov = ct.provider or self.provider
            mdl = ct.model or self.model
        else:
            prov = self.provider
            mdl = self.model

        # ── V2 LLMRouter (5-slot pick) ────────────────────────────────
        # Single entry point that replaces extra_llms label routing +
        # auto_route scoring. Builds slots from V1 agent fields, classifies
        # signals from the call, picks the best slot. Falls through to
        # legacy paths when slots are empty / router disabled.
        if os.environ.get("TUDOU_DISABLE_LLM_ROUTER", "0") != "1":
            try:
                from .v2.agent.llm_slots import slots_from_v1_agent
                from .v2.bridges.llm_router import get_router
                slots = slots_from_v1_agent(self)
                # Allow per-task explicit slot pick via llm_label
                explicit_function = ""
                if ct is not None:
                    explicit_function = (getattr(ct, "llm_label", "") or "").strip()
                # Compose signals
                signals: dict[str, Any] = {}
                if user_message is not None:
                    try:
                        signals["has_image_or_audio"] = self._message_is_multimodal(user_message)
                    except Exception:
                        pass
                    text = ""
                    if isinstance(user_message, str):
                        text = user_message
                    elif isinstance(user_message, dict):
                        c = user_message.get("content", "")
                        text = c if isinstance(c, str) else ""
                    if text:
                        signals["last_user_text"] = text
                        signals["prompt_chars"] = len(text)
                # Phase 1-B hint: in-progress step's llm_purpose
                try:
                    _plan = getattr(self, "_current_plan", None)
                    if _plan is not None and not explicit_function:
                        for s in _plan.steps:
                            _stat = s.status.value if hasattr(s.status, "value") else str(s.status)
                            if _stat == "in_progress":
                                _hint = (getattr(s, "llm_purpose", "") or "").strip()
                                # Map V1 categories → V2 slot names
                                _hint_map = {
                                    "tool-heavy": "default",
                                    "analysis": "analysis",
                                    "reasoning": "reasoning",
                                    "coding": "coding",
                                    "multimodal": "multimodal",
                                }
                                if _hint in _hint_map:
                                    explicit_function = _hint_map[_hint]
                                break
                except Exception:
                    pass
                decision = get_router().pick(
                    slots,
                    explicit_function=explicit_function,
                    signals=signals,
                )
                if decision.binding.is_set():
                    changed = (decision.binding.provider,
                                decision.binding.model) != (prov, mdl)
                    # Always log on change (INFO). Optionally log every
                    # decision (no-op kept) when TUDOU_LLMROUTER_VERBOSE=1
                    # — useful to confirm the router is actually running
                    # on a quiet config where slots == primary.
                    if changed:
                        logger.info(
                            "Agent %s: LLMRouter [%s] → %s (%s)",
                            self.id[:8], decision.slot,
                            decision.binding.to_str(), decision.reason,
                        )
                    elif os.environ.get("TUDOU_LLMROUTER_VERBOSE", "0") == "1":
                        logger.info(
                            "Agent %s: LLMRouter [%s] = %s (no change; %s)",
                            self.id[:8], decision.slot,
                            decision.binding.to_str(), decision.reason,
                        )
                    prov = decision.binding.provider
                    mdl = decision.binding.model
            except Exception as _re:
                logger.debug("LLMRouter skipped: %s", _re)

        # 方案乙: extra_llms 路由 —— 如果 task 带了 llm_label，优先从
        # agent.extra_llms 里找 label 或 purpose 命中的 slot，命中就覆盖
        # provider/model。这是最简形态：单层查找、无 fallback chain。
        # 以后要做按成本/上下文长度/模态自动挑，也只改这一段。
        try:
            label = ""
            if ct is not None:
                label = (getattr(ct, "llm_label", "") or "").strip()
            if label and self.extra_llms:
                for slot in self.extra_llms:
                    if not isinstance(slot, dict):
                        continue
                    slot_label = str(slot.get("label", "")).strip()
                    slot_purpose = str(slot.get("purpose", "")).strip()
                    if slot_label == label or slot_purpose == label:
                        sp = str(slot.get("provider", "")).strip()
                        sm = str(slot.get("model", "")).strip()
                        if sp or sm:
                            logger.info(
                                "Agent %s: extra_llms[%s] → routing to %s/%s",
                                self.id[:8], label, sp or prov, sm or mdl,
                            )
                            prov = sp or prov
                            mdl = sm or mdl
                        break
        except Exception as _el_err:
            logger.debug("extra_llms routing skipped: %s", _el_err)

        # Score-based auto-route — replaces the old label-map approach.
        # When auto_route.enabled = true, the router:
        #   1. Detects the current turn's category (tool-heavy / multimodal /
        #      reasoning / analysis / complex / default) from signals.
        #   2. Ranks each candidate slot (primary + extra_llms) by a score
        #      for that category: user-declared `slot.scores[category]` wins,
        #      otherwise falls back to the public benchmark table in
        #      app/data/model_scores.json, otherwise neutral 5.0.
        #   3. Picks the winner — primary wins ties (stable sort).
        # Emits a single info-level log line per decision so the operator
        # can see "Agent X: route[tool-heavy] primary=glm/6.8 qwen/8.7 →
        # pick qwen" without needing to guess.
        try:
            ar = self.auto_route or {}
            explicit_label = ""
            if ct is not None:
                explicit_label = (getattr(ct, "llm_label", "") or "").strip()
            if (
                ar.get("enabled")
                and self.extra_llms
                and not explicit_label  # explicit label was already handled above
            ):
                from . import llm_router as _router
                threshold = int(ar.get("complex_threshold_chars", 2000) or 2000)
                # Phase 1-B: if there's an active plan with an
                # in-progress step whose `llm_purpose` is set, use that
                # as the category — it's the LLM's own routing hint for
                # this specific sub-task, which is more accurate than
                # message-text keyword detection (which only sees the
                # original user prompt, not "which step am I on").
                category = ""
                _cat_source = "keyword"  # for logging
                try:
                    _plan = getattr(self, "_current_plan", None)
                    if _plan is not None and _plan.steps:
                        _step = None
                        for s in _plan.steps:
                            _st_val = s.status.value if hasattr(s.status, "value") else str(s.status)
                            if _st_val == "in_progress":
                                _step = s
                                break
                        if _step is not None:
                            _hint = str(getattr(_step, "llm_purpose", "") or "").strip()
                            if _hint in ("tool-heavy", "multimodal",
                                         "reasoning", "analysis",
                                         "coding", "default"):
                                category = _hint
                                _cat_source = f"plan_step({_step.id}).llm_purpose"
                                logger.info(
                                    "Agent %s: category=%s from in-progress "
                                    "step '%s' (llm_purpose)",
                                    self.id[:8], _hint, _step.title[:30])
                            else:
                                logger.info(
                                    "Agent %s: in-progress step '%s' has "
                                    "empty/invalid llm_purpose=%r — falling "
                                    "back to keyword detection",
                                    self.id[:8], _step.title[:30], _hint)
                        else:
                            logger.info(
                                "Agent %s: plan exists but no step in-progress "
                                "— using keyword detection",
                                self.id[:8])
                    else:
                        logger.info(
                            "Agent %s: no active plan — using keyword detection",
                            self.id[:8])
                except Exception as _step_err:
                    logger.warning("step-purpose lookup failed: %s", _step_err)
                if not category:
                    category = _router.detect_category(
                        user_message=user_message,
                        has_tools=True,
                        recent_tool_call_density=0.0,
                        complex_threshold_chars=threshold,
                    )
                data = _router.load_scores()
                # Build a decision breakdown for the log line — shows every
                # candidate's score so the operator can sanity-check why
                # a specific slot won.
                _breakdown: list[str] = []
                _primary_score = _router.score_for_model(mdl, category, data) if mdl else 5.0
                _breakdown.append(f"primary({prov}/{mdl})={_primary_score:.1f}")
                for _slot in self.extra_llms:
                    if not isinstance(_slot, dict):
                        continue
                    _lbl = (_slot.get("label") or _slot.get("purpose")
                            or _slot.get("model") or "?")
                    _s = _router._slot_score(_slot, category, data)
                    _breakdown.append(f"{_lbl}={_s:.1f}")
                new_prov, new_mdl, winner_slot = _router.best_slot_for_category(
                    self.extra_llms, category,
                    primary_provider=prov, primary_model=mdl, data=data)
                winner_label = (
                    "primary" if winner_slot is None
                    else str(winner_slot.get("label", "") or winner_slot.get("purpose", "") or "?"))
                if (new_prov, new_mdl) != (prov, mdl):
                    logger.info(
                        "Agent %s: route[%s] scores=[%s] → switch to %s/%s (%s)",
                        self.id[:8], category, ", ".join(_breakdown),
                        new_prov, new_mdl, winner_label)
                    prov, mdl = new_prov, new_mdl
                else:
                    logger.info(
                        "Agent %s: route[%s] scores=[%s] → keep primary %s/%s",
                        self.id[:8], category, ", ".join(_breakdown),
                        prov, mdl)
        except Exception as _ar_err:
            logger.debug("auto_route skipped: %s", _ar_err)

        # Multimodal routing: if the incoming message is multimodal and a
        # dedicated multimodal model is configured, prefer it.
        try:
            if user_message is not None and self._message_is_multimodal(user_message):
                if self.multimodal_provider or self.multimodal_model:
                    mm_prov = self.multimodal_provider or prov
                    mm_mdl = self.multimodal_model or mdl
                    logger.info(
                        "Agent %s: multimodal input → routing to %s/%s",
                        self.id[:8], mm_prov, mm_mdl,
                    )
                    prov, mdl = mm_prov, mm_mdl
                else:
                    logger.warning(
                        "Agent %s: multimodal input detected but no "
                        "multimodal_provider/model configured — sending "
                        "images to default %s/%s (ensure it supports vision)",
                        self.id[:8], prov, mdl,
                    )
        except Exception as _mm_err:
            logger.debug("multimodal routing skipped: %s", _mm_err)
        try:
            cfg = llm.get_config()
            if not prov:
                # No provider set — use global default
                prov = cfg.get("provider", "")
                mdl = mdl or cfg.get("model", "")
            else:
                # Provider set — verify it still exists and is enabled
                reg = llm.get_registry()
                entry = reg.get(prov)
                if entry is None or not entry.enabled:
                    prov = cfg.get("provider", "")
                    mdl = mdl or cfg.get("model", "")
                    logger.warning(
                        "Agent %s: provider '%s' unavailable, "
                        "falling back to '%s/%s'",
                        self.id[:8], self.provider, prov, mdl)
        except Exception as e:
            logger.error("Agent %s: provider resolution failed: %s",
                         self.id[:8], e)
        return prov, mdl

    def _resolve_fallback_llm(self) -> tuple[str, str]:
        """Return (provider, model) of the designated fallback LLM, if any.

        Looks into ``self.extra_llms`` for a slot whose ``purpose`` or
        ``label`` equals ``"fallback"`` (case-insensitive). Partial slots
        (provider only, or model only) are accepted — the caller should
        inherit the missing half from the primary. Returns ``("", "")``
        when no fallback is configured.
        """
        try:
            for slot in (self.extra_llms or []):
                if not isinstance(slot, dict):
                    continue
                tag = str(slot.get("purpose", "") or slot.get("label", "")).strip().lower()
                if tag != "fallback":
                    continue
                return (str(slot.get("provider", "")).strip(),
                        str(slot.get("model", "")).strip())
        except Exception as e:
            logger.debug("fallback LLM lookup failed: %s", e)
        return "", ""

    def _handle_large_result(self, tool_name: str, result: str) -> str:
        """If tool result exceeds 100KB, save to file and return a summary + path."""
        LARGE_RESULT_THRESHOLD = 100_000  # 100KB

        if len(result) <= LARGE_RESULT_THRESHOLD:
            return result

        # Save to working_dir or a results directory
        results_dir = os.path.join(
            self.working_dir or os.path.join(
                os.environ.get("TUDOU_CLAW_DATA_DIR", "."),
                "workspaces", self.id
            ),
            "large_results"
        )
        os.makedirs(results_dir, exist_ok=True)

        # Generate filename with timestamp
        import datetime
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = os.path.join(results_dir, f"{tool_name}_{timestamp}.txt")

        # Save result to file
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(result)
        except Exception as e:
            logger.error(f"Failed to save large result to {filepath}: {e}")
            # Fall back to returning truncated result if save fails
            return result[:LARGE_RESULT_THRESHOLD] + f"\n...[result truncated, failed to save to file: {e}]"

        # Return truncated preview + file path
        preview = result[:2000] + "\n...\n" + result[-500:]
        return f"[Result too large ({len(result)} chars), saved to {filepath}]\n\nPreview:\n{preview}"

    # ------------------------------------------------------------------ #
    # LoginGuard: transparent login-wall handling
    # ------------------------------------------------------------------ #

    def _get_login_guard(self):
        """Lazy-init the LoginGuard singleton for this agent."""
        if self._login_guard is None:
            from .login_guard import LoginGuard
            self._login_guard = LoginGuard()
        return self._login_guard

    def _execute_tool_guarded(
        self, tool_name: str, arguments: dict, *, on_event: Any = None,
    ) -> str:
        """Execute a tool and run the result through LoginGuard.

        If the tool result looks like a login page, the guard automatically
        shows a login card, waits for the user, and retries the tool call.
        The LLM receives the post-login result transparently.
        """
        result = tools.execute_tool(tool_name, arguments)
        guard = self._get_login_guard()
        return guard.guard(
            self, tool_name, arguments, result,
            retry_fn=lambda: tools.execute_tool(tool_name, arguments),
            on_event=on_event,
        )

    def _execute_tool_with_policy(self, tool_name: str, arguments: dict,
                                   on_event: Any = None) -> str:
        """Execute a tool, checking policy. May block for approval."""
        # Resolve alias (e.g. "exec" → "bash") BEFORE permission check
        tool_name = tools._TOOL_ALIASES.get(tool_name, tool_name)

        # Substitute credential placeholders ({{CRED_xxx}}) with real values
        # so sensitive data stays out of LLM context but reaches the tool.
        arguments = self._substitute_credentials(arguments)

        # Inject agent context for tools that need RAG routing
        if tool_name in ("knowledge_lookup",):
            arguments = dict(arguments)
            arguments["_agent_profile"] = self.profile
            arguments["agent_id"] = self.id

        # Check agent-level denied tools (legacy agents may have None;
        # defend with `or ()` so `in` doesn't trip NoneType iteration)
        if tool_name in (self.profile.denied_tools or ()):
            return f"DENIED: Tool '{tool_name}' is not permitted for this agent."

        # ── Infrastructure tools always available ──
        # 记忆 / 技能查询类工具不消耗副作用,不涉及写操作,属于"自查"范畴。
        # 老的 role preset (code_reviewer/researcher) 没在 allowed_tools 里
        # 列它们,但我们希望所有 agent 都能自查记忆 —— 否则 agent 会反复
        # 重新学,也无法 save_experience 沉淀经验。除非 denied_tools 显式禁。
        _INFRA_TOOLS = frozenset({
            "memory_recall", "knowledge_lookup", "save_experience",
            "get_skill_guide", "plan_update", "complete_step",
        })

        # Check agent-level allowed tools (empty list = all allowed;
        # infra tools bypass this check since they're universally safe)
        if (self.profile.allowed_tools
                and tool_name not in self.profile.allowed_tools
                and tool_name not in _INFRA_TOOLS):
            return f"DENIED: Tool '{tool_name}' is not in this agent's allowed list."

        # Scheduled / background task: skip approval (already authorized at creation)
        if getattr(self, '_scheduled_context', False):
            with self._sandbox_scope():
                result = self._execute_tool_guarded(tool_name, arguments, on_event=on_event)
            return result

        # Agent-level exec_policy: 'full' = auto-approve all tools
        if self.profile.exec_policy == "full":
            with self._sandbox_scope():
                result = self._execute_tool_guarded(tool_name, arguments, on_event=on_event)
            return result

        from .auth import get_auth
        auth = get_auth()
        policy = auth.tool_policy

        # Check if this agent auto-approves this tool.
        # `auto_approve_tools` should be a list[str] but legacy persisted
        # agents (saved before the dataclass had a default_factory) may
        # have it as None. Defend with `or ()`.
        if tool_name in (self.profile.auto_approve_tools or ()):
            _t0 = time.time()
            with self._sandbox_scope():
                result = self._execute_tool_guarded(tool_name, arguments, on_event=on_event)
            _elapsed_ms = int((time.time() - _t0) * 1000)
            auth.audit(
                "tool_executed", actor=self.name, target=tool_name,
                detail=result,
                agent_id=self.id,
                project_id=getattr(self, "project_id", "") or "",
                meeting_id=getattr(self, "source_meeting_id", "") or "",
                arguments=arguments,
                elapsed_ms=_elapsed_ms,
            )
            return result

        decision, reason = policy.check_tool(
            tool_name, arguments,
            agent_id=self.id, agent_name=self.name,
            agent_priority=getattr(self.profile, 'priority', 3),
        )

        # MODERATE risk: if agent_approvable and this agent (or a superior)
        # has authority, auto-approve it
        if decision == "agent_approvable":
            agent_pri = getattr(self.profile, 'priority', 3)
            if policy.can_agent_approve(self.id, agent_pri, "moderate"):
                decision = "allow"
                reason = f"Agent-approved (priority={agent_pri})"
                auth.audit("tool_agent_approved", actor=self.name,
                           target=tool_name, detail=reason)
            else:
                # Escalate to human approval
                decision = "needs_approval"

        if decision == "deny":
            auth.audit(
                "tool_denied", actor=self.name, target=tool_name,
                detail=f"Auto-denied: {reason}", success=False,
                agent_id=self.id,
                project_id=getattr(self, "project_id", "") or "",
                meeting_id=getattr(self, "source_meeting_id", "") or "",
                arguments=arguments,
            )
            evt = AgentEvent(time.time(), "approval", {
                "tool": tool_name, "status": "denied", "reason": reason,
                "agent_name": self.name,
            })
            self._log(evt.kind, evt.data)
            if on_event:
                on_event(evt)
            return f"DENIED: {reason}. This operation is not allowed for security reasons."

        if decision == "needs_approval":
            self.status = AgentStatus.WAITING_APPROVAL

            # Create the PendingApproval FIRST so approval_id is available
            # for the SSE event (clients need it to call the approve API).
            approval = policy.request_approval(
                tool_name, arguments,
                agent_id=self.id, agent_name=self.name,
                reason=reason,
            )

            evt = AgentEvent(time.time(), "approval", {
                "tool": tool_name, "status": "pending", "reason": reason,
                "arguments": _truncate_dict(arguments),
                "agent_name": self.name,
                "approval_id": approval.approval_id,
            })
            self._log(evt.kind, evt.data)
            if on_event:
                on_event(evt)

            auth.audit(
                "tool_approval_requested", actor=self.name,
                target=tool_name,
                detail=json.dumps(_truncate_dict(arguments),
                                  ensure_ascii=False),
                agent_id=self.id,
                project_id=getattr(self, "project_id", "") or "",
                meeting_id=getattr(self, "source_meeting_id", "") or "",
                arguments=arguments,
            )

            result_status = policy.wait_for_approval(approval)
            self.status = AgentStatus.BUSY

            if result_status != "approved":
                auth.audit(
                    "tool_denied", actor=self.name, target=tool_name,
                    detail=f"Human denied/expired: {approval.decided_by}",
                    success=False,
                    agent_id=self.id,
                    project_id=getattr(self, "project_id", "") or "",
                    meeting_id=getattr(self, "source_meeting_id", "") or "",
                    arguments=arguments,
                )
                evt = AgentEvent(time.time(), "approval", {
                    "tool": tool_name, "status": "denied",
                    "reason": f"{result_status} by {approval.decided_by or 'timeout'}",
                "agent_name": self.name,
                })
                self._log(evt.kind, evt.data)
                if on_event:
                    on_event(evt)
                return (f"DENIED: Tool execution was {result_status}. "
                        f"Decided by: {approval.decided_by or 'timeout'}. "
                        f"Please try an alternative approach.")

            auth.audit("tool_approved", actor=self.name, target=tool_name,
                       detail=f"Approved by {approval.decided_by}")
            evt = AgentEvent(time.time(), "approval", {
                "tool": tool_name, "status": "approved",
                "decided_by": approval.decided_by,
                "agent_name": self.name,
            })
            self._log(evt.kind, evt.data)
            if on_event:
                on_event(evt)

        # ── Middleware: PRE_TOOL (lint check, etc.) ──
        try:
            from .middleware import ensure_pipeline, MiddlewareContext, Stage
            pipe = ensure_pipeline()
            pre_ctx = MiddlewareContext(
                agent_id=self.id, agent_name=self.name,
                tool_name=tool_name, tool_arguments=arguments,
            )
            pre_result = pipe.run(Stage.PRE_TOOL, pre_ctx)
            if pre_result.short_circuited:
                return pre_result.value  # Lint check failed — return error to LLM
        except Exception as _mw_err:
            logger.debug("pre_tool middleware skipped: %s", _mw_err)

        _t0 = time.time()
        with self._sandbox_scope():
            result = self._execute_tool_guarded(tool_name, arguments, on_event=on_event)
        _elapsed_ms = int((time.time() - _t0) * 1000)

        # ── Middleware: POST_TOOL (truncation, etc.) ──
        try:
            from .middleware import ensure_pipeline, MiddlewareContext, Stage
            pipe = ensure_pipeline()
            post_ctx = MiddlewareContext(
                agent_id=self.id, agent_name=self.name,
                tool_name=tool_name, tool_arguments=arguments,
                tool_result=result,
            )
            post_result = pipe.run(Stage.POST_TOOL, post_ctx)
            if post_ctx.tool_result != result:
                result = post_ctx.tool_result  # middleware modified the result
        except Exception as _mw_err:
            logger.debug("post_tool middleware skipped: %s", _mw_err)

        # Forensic audit: capture full arguments + result + elapsed.
        # The smoking gun for the 2026-04-29 self-sent-email incident
        # was that the audit log only had "SUCCESS [exit code: 0]" with
        # no trace of what bash ran — now `arguments.command` is logged.
        auth.audit(
            "tool_executed", actor=self.name, target=tool_name,
            detail=result,
            success=not (isinstance(result, str) and result.lstrip().lower().startswith("error")),
            agent_id=self.id,
            project_id=getattr(self, "project_id", "") or "",
            meeting_id=getattr(self, "source_meeting_id", "") or "",
            arguments=arguments,
            elapsed_ms=_elapsed_ms,
        )
        return result

    def _sandbox_scope(self):
        """Install a sandbox policy rooted at this agent's working_dir."""
        from . import sandbox as _sandbox
        import os as _os
        # Use the agent's working_dir as jail root. Fall back to the per-agent
        # workspace directory if no working_dir is configured.
        root = self.working_dir
        if not root:
            from . import DEFAULT_DATA_DIR as _DEFAULT_DD
            root = _os.path.join(
                _os.environ.get("TUDOU_CLAW_DATA_DIR") or _DEFAULT_DD,
                "workspaces", self.id, "sandbox")
        # Honor per-agent sandbox mode if set on the profile, otherwise
        # use the global default from TUDOU_SANDBOX env var.
        mode = getattr(self.profile, "sandbox_mode", "") or ""
        allow_list = list(getattr(self.profile, "sandbox_allow_commands", []) or [])

        # Build allowed_dirs from authorized workspaces + shared workspace.
        # Resolve the shared workspace for the CURRENT active context
        # (project/meeting → that surface's dir; solo → empty). Multi-
        # project agents naturally get the right project's dir per
        # turn instead of always-pointing-at-first-joined.
        allowed_dirs = []
        _shared_ws = self.get_active_shared_workspace()
        if _shared_ws:
            allowed_dirs.append(_shared_ws)
        # Add workspaces of authorized agents
        from . import DEFAULT_DATA_DIR as _DEFAULT_DD2
        data_dir = _os.environ.get("TUDOU_CLAW_DATA_DIR") or _DEFAULT_DD2
        for other_agent_id in self.authorized_workspaces:
            ws_path = _os.path.join(data_dir, "workspaces", other_agent_id)
            allowed_dirs.append(ws_path)
        # Allow access to agent's skills directory so granted skill scripts
        # can be executed without sandbox violations.
        agent_skills_dir = _os.path.join(str(self._get_agent_workspace()), "skills")
        if _os.path.isdir(agent_skills_dir):
            allowed_dirs.append(agent_skills_dir)

        # Granted skills: open read/cd to each install_dir so get_skill_guide's
        # skill_dir is actually reachable via read_file / bash cd. Only skills
        # granted to THIS agent — ungranted skill dirs remain blocked.
        try:
            from .skills.engine import get_registry as _get_skill_registry
            _reg = _get_skill_registry()
            if _reg is not None:
                for _inst in _reg.list_for_agent(self.id):
                    _sd = getattr(_inst, "install_dir", "") or ""
                    if _sd and _os.path.isdir(_sd) and _sd not in allowed_dirs:
                        allowed_dirs.append(_sd)
        except Exception:
            # Non-fatal — sandbox without extended skill access is still usable.
            pass

        # ── Read-only allow list ──────────────────────────────────────
        # Let agents READ any installed skill's source for reference
        # (manifest patterns, helper module structure, prompt examples)
        # without granting the skill or risking writes. Writes still
        # rejected with a clear "request a grant" error message.
        # Granted skills' install_dirs above are still in allowed_dirs
        # (read+write); this only opens the broader skills_installed/
        # tree for read-only reference.
        readonly_dirs: list[str] = []
        try:
            from . import DEFAULT_DATA_DIR as _DEFAULT_DD3
            _data_dir3 = _os.environ.get("TUDOU_CLAW_DATA_DIR") or _DEFAULT_DD3
            _skills_root = _os.path.join(_data_dir3, "skills_installed")
            if _os.path.isdir(_skills_root):
                readonly_dirs.append(_skills_root)
        except Exception:
            pass

        policy = _sandbox.SandboxPolicy(
            root=root, mode=mode, allow_list=allow_list,
            agent_id=self.id, agent_name=self.name,
            allowed_dirs=allowed_dirs,
            readonly_dirs=readonly_dirs,
        )
        return _sandbox.sandbox_scope(policy)

    # ---- chat ----

    def chat(self, user_message, on_event: Any = None,
             abort_check: Any = None, source: str = "admin",
             context_id: str = "solo") -> str:
        """
        Run a chat turn. If abort_check is a callable returning True,
        the chat loop will stop early.

        user_message: str for text-only, or list[dict] for multimodal content
                      (OpenAI vision format: [{type:"text",text:...},{type:"image_url",...}])
        source: "admin" for messages from portal UI, "agent:{agent_name}" for inter-agent,
                "system" for system messages
        context_id: which conversation context this turn belongs to —
                    "solo" (default; direct admin/user chat),
                    "project:{project_id}" (Project group chat),
                    "meeting:{meeting_id}" (Meeting). Each context has its
                    own message history; turns from one context never
                    leak into another's LLM payload.
        """
        # ── Token logging context: 让本次 chat 内所有 LLM 调用 ──
        # ── 都能归属到这个 agent/project/meeting，token 统计才能落到 ──
        # ── agent.stats / project.stats / meeting.stats 。project_id ──
        # ── 和 source_meeting_id 都是 agent 对象的属性(Agent dataclass) ──
        try:
            llm.set_token_context(
                agent_id=self.id,
                project_id=getattr(self, "project_id", "") or "",
                meeting_id=getattr(self, "source_meeting_id", "") or "",
            )
        except Exception:
            pass

        with self._lock:
            # Switch to the right per-context message bucket BEFORE any
            # _ensure_system_message / append / trim runs — they all read
            # and write `self.messages`, which now points at the bucket
            # for this context.
            self._switch_context(context_id)
            self.status = AgentStatus.BUSY
            # Reset streaming buffer at the start of every turn so a
            # previous turn's text doesn't bleed into the "typing"
            # preview the polling endpoint shows.
            self._streaming_buffer = ""

            # 新 A.8: reset per-turn memory_recall bucket so refs from
            # the previous turn don't leak into this turn's assistant msg.
            try:
                self._turn_memory_refs = []
            except Exception:
                pass
            # Reset per-turn query-dedup cache (memory_recall / knowledge_lookup)
            try:
                self._turn_query_cache = {}
            except Exception:
                pass

            # ── Multimodal content handling ──
            # user_message can be str (text-only) or list[dict] (multimodal)
            _is_multimodal = isinstance(user_message, list)
            if _is_multimodal:
                _user_text = " ".join(
                    p.get("text", "") for p in user_message
                    if isinstance(p, dict) and p.get("type") == "text"
                ).strip() or "(multimodal input)"
                _msg_content = user_message  # preserve list for LLM
            else:
                _user_text = str(user_message or "")
                _msg_content = _user_text

            # ── Memory augmentation: inject relevant facts as LLM CONTEXT ──
            # Memory is background reference, NOT a substitute for LLM reasoning.
            # The LLM always generates the actual answer.
            memory_context: str | None = None
            try:
                memory_context = self._build_memory_context(_user_text)
            except Exception as _mem_err:
                logger.debug("Failed to build memory context: %s", _mem_err)
                memory_context = None

            self._ensure_system_message(current_query=_user_text)
            self._trim_context()
            msg = {"role": "user", "content": _msg_content, "source": source}
            self.messages.append(msg)
            self._log("message", {"role": "user", "content": _user_text[:500], "source": source})

            # --- agent_state shadow (phase-1 grey rollout) ---
            # Mirror this user turn into the new typed state model.
            # Failures here MUST NOT affect the live agent path.
            try:
                from .agent_state.shadow import install_into_agent
                _shadow = getattr(self, "_shadow", None) or install_into_agent(self)
                if _shadow is not None:
                    _shadow.record_user(_user_text, source=source)
            except Exception:
                pass

            # --- Memory augmentation: inject retrieved facts as system context ---
            if memory_context:
                self.messages.append({
                    "role": "system",
                    "content": memory_context,
                })

            # --- Enhancement module: pre-thinking injection ---
            if self.enhancer and self.enhancer.enabled:
                pre_think = self.enhancer.pre_think(_user_text)
                if pre_think:
                    # Inject as a system-level context hint before LLM processes
                    self.messages.append({
                        "role": "system",
                        "content": pre_think,
                    })
                    self._log("enhancement", {"action": "pre_think",
                                               "pattern": pre_think[:100]})

            # --- Template Library: auto-match and inject templates ---
            try:
                tpl_lib = get_template_library()
                matched_templates = tpl_lib.match_templates(
                    _user_text, role=self.role, limit=2)
                if matched_templates:
                    tpl_context = tpl_lib.render_for_agent(
                        matched_templates, max_chars=4000)
                    if tpl_context:
                        self.messages.append({
                            "role": "system",
                            "content": tpl_context,
                        })
                        tpl_names = [t.name for t in matched_templates]
                        self._log("template_match", {
                            "templates": tpl_names,
                            "chars": len(tpl_context),
                        })
            except Exception:
                pass  # template library is optional

            # ── Inbox pull: inject unread inter-agent messages ──
            # Other agents' send_message calls persist to the durable
            # inbox. At the start of each chat we surface any unread
            # messages addressed to THIS agent so the LLM can decide
            # whether/how to react in-turn.
            try:
                _inbox_ctx, _inbox_msg_ids = self._build_inbox_context(limit=10)
                if _inbox_ctx:
                    self.messages.append({
                        "role": "system",
                        "content": _inbox_ctx,
                    })
                    # Mark read ONLY after injection is committed.
                    try:
                        from .inbox import get_store
                        get_store().mark_read(_inbox_msg_ids, self.id)
                    except Exception as _mr_err:
                        logger.debug("inbox mark_read skipped: %s", _mr_err)
                    self._log("inbox_pull", {
                        "count": len(_inbox_msg_ids),
                        "chars": len(_inbox_ctx),
                    })
            except Exception as _ibx_err:
                logger.debug("inbox injection skipped: %s", _ibx_err)

            # ── Checkpoint resume: inject digest once on resume ──
            # When the user clicks "Restore" in the portal, the
            # checkpoint row is flipped to status=restored AND the
            # metadata.pending_chat_delivery flag is set. This block
            # atomically consumes it and prepends the digest as LLM
            # context on this turn. Delivered exactly once.
            try:
                _ckpt_ctx, _ckpt_id = self._build_resume_digest_context()
                if _ckpt_ctx:
                    self.messages.append({
                        "role": "system",
                        "content": _ckpt_ctx,
                    })
                    self._log("checkpoint_resumed", {
                        "checkpoint_id": _ckpt_id,
                        "chars": len(_ckpt_ctx),
                    })
            except Exception as _cke:
                logger.debug("checkpoint resume injection skipped: %s", _cke)

            # ── RolePresetV2 Pre-hook: SOP stage injection ──
            # Runs ONLY for V2 agents with sop_template_id configured.
            self._active_sop_instance = None
            try:
                sop_tpl_id = getattr(self.profile, "sop_template_id", "") or ""
                role_preset_version = getattr(self.profile, "role_preset_version", 1)
                if sop_tpl_id and role_preset_version == 2:
                    from .role_sop import get_sop_manager
                    sop_mgr = get_sop_manager()
                    sop_session = f"chat_{int(time.time())}"
                    inst = sop_mgr.get_or_start(self.id, sop_session, sop_tpl_id)
                    if inst is not None:
                        stage_prompt = sop_mgr.current_stage_prompt(inst)
                        if stage_prompt:
                            self.messages.append({
                                "role": "system",
                                "content": stage_prompt,
                            })
                        self._active_sop_instance = inst
                        self._log("sop_stage_enter", {
                            "sop_id": inst.sop_id,
                            "stage_id": inst.current_stage,
                            "instance_id": inst.instance_id,
                        })
            except Exception as _sop_err:
                logger.debug("SOP pre-hook skipped: %s", _sop_err)

            # --- Skill System: auto-match and inject skills ---
            self._active_skill_ids = []
            self._chat_start_time = time.time()
            try:
                registry = get_prompt_pack_registry()
                if registry.store.get_active():
                    matched_skills = registry.match_skills(
                        _user_text, top_k=3,
                        agent_skills=self.bound_prompt_packs or None)
                    # Filter: if the skill's name / skill_id / content references
                    # a tool currently in the global denylist, drop it. This
                    # prevents revoked skills (e.g. pptx_advanced after
                    # create_pptx_advanced is globally disabled) from still
                    # being injected into the system prompt.
                    try:
                        from app.auth import get_auth as _get_auth
                        _denied = set(_get_auth().tool_policy.list_global_denylist())
                    except Exception:
                        _denied = set()
                    if matched_skills and _denied:
                        def _refs_denied(pack) -> bool:
                            n = (pack.name or "").lower()
                            sid = (pack.skill_id or "").lower()
                            body = (pack.content or "").lower()
                            for tool in _denied:
                                t = tool.lower()
                                if not t:
                                    continue
                                # name/id contains the denied tool stub
                                if t in n or t in sid:
                                    return True
                                # content invokes the denied tool by name
                                if t in body:
                                    return True
                            return False
                        matched_skills = [p for p in matched_skills
                                          if not _refs_denied(p)]
                    if matched_skills:
                        skill_ids = [s.skill_id for s in matched_skills]
                        # Default to summary mode (5000 chars ≈ 1250 tokens
                        # for 3-6 skills). Agent can call
                        # get_skill_guide(name, brief=false) for full body
                        # when it actually needs to execute.
                        context_text = registry.build_context_injection(
                            skill_ids, max_chars=5000)
                        if context_text:
                            self.messages.append({
                                "role": "system",
                                "content": context_text,
                            })
                            self._active_skill_ids = skill_ids
                            self._log("skill_match", {
                                "skills": [s.name for s in matched_skills],
                                "chars": len(context_text),
                            })
            except Exception:
                pass  # skill system is optional

            # ── P1-C: inject per-agent output budget hint ──
            # The LLM usually respects explicit budget hints — this adds
            # a final compact system note on the messages list stating
            # the expected token ceiling for THIS turn's output. Agents
            # with max_output_tokens=0 get no hint (default provider
            # behaviour). Tier defaults kick in when an agent has a
            # tier set but no explicit ceiling yet (see _tier_budgets()).
            try:
                budget = int(getattr(self.profile, "max_output_tokens", 0) or 0)
                if budget <= 0:
                    budget = self._tier_default_output_budget(
                        getattr(self.profile, "agent_tier", "") or "")
                if budget > 0:
                    tier_tag = ""
                    _t = (getattr(self.profile, "agent_tier", "") or "").strip()
                    if _t:
                        tier_tag = f" [tier={_t}]"
                    self.messages.append({
                        "role": "system",
                        "content": (
                            f"⚠️ 本轮输出预算：≤{budget} tokens{tier_tag}。"
                            f"请用精简语言，能用 summary + key_fields + artifact_refs "
                            f"的就用，避免长篇 prose；大产出先 write_file 再通过 "
                            f"send_message(artifact_refs=[...]) 传 ref。"
                        ),
                    })
            except Exception:
                pass

            # --- src memory engine: transcript + routing ---
            self.transcript.append(_user_text)
            self.turn_count += 1
            # Route prompt through PortRuntime for context enrichment
            try:
                routed = self.route_prompt(_user_text, limit=3)
                if routed:
                    route_info = ", ".join(f"{m.kind}:{m.name}({m.score})"
                                           for m in routed[:3])
                    self._log("routing", {"matches": route_info})
            except Exception:
                routed = []

            # Dedup guard — the tool-calling loop sometimes emits the same
            # assistant text multiple times (once per iteration when the LLM
            # keeps repeating a bridge sentence between tool calls, and once
            # as the final consolidated output). The user sees 4 identical
            # "好的，我已经收集到..." bubbles. Squash consecutive duplicates.
            _last_emitted_text_ref = [None]
            def _emit(evt: AgentEvent):
                # Accumulate streaming text deltas into the public
                # buffer regardless of whether on_event is bound. This
                # lets the project chat polling endpoint surface the
                # in-progress reply ("typing" preview) — option A from
                # the 2026-04-28 streaming UX discussion.
                try:
                    if evt.kind == "text_delta":
                        _chunk = (evt.data or {}).get("content", "")
                        if _chunk:
                            self._streaming_buffer = (
                                self._streaming_buffer or ""
                            ) + _chunk
                except Exception:
                    pass
                if on_event:
                    try:
                        # Only dedupe assistant-message events; everything
                        # else (tool_call / tool_result / text_delta / …)
                        # flows through untouched.
                        if (evt.kind == "message"
                                and evt.data.get("role") == "assistant"):
                            c = (evt.data.get("content") or "").strip()
                            if c and c == _last_emitted_text_ref[0]:
                                return  # suppress exact repeat
                            _last_emitted_text_ref[0] = c
                        on_event(evt)
                    except Exception:
                        pass

            def _is_aborted() -> bool:
                if abort_check and callable(abort_check):
                    return abort_check()
                return False

            tool_defs = self._get_effective_tools()
            final_content = ""

            # History: record chat start
            self.history_log.add("chat_start",
                                 f"user={_user_text[:80]}")

            try:
                old_cwd = os.getcwd()
                if self.working_dir and Path(self.working_dir).is_dir():
                    os.chdir(self.working_dir)

                # ── Real-time provider/model refresh (multimodal-aware) ──
                _eff_provider, _eff_model = self._resolve_effective_provider_model(
                    user_message=user_message,
                )

                # ── Middleware: PRE_LLM (compaction + model routing) ──
                try:
                    from .middleware import ensure_pipeline, MiddlewareContext, Stage
                    _mw_pipe = ensure_pipeline()
                    _mw_ctx = MiddlewareContext(
                        agent_id=self.id, agent_name=self.name,
                        messages=self.messages,
                        provider=_eff_provider, model=_eff_model,
                        data={"context_limit": self._get_context_limit()},
                    )
                    _mw_result = _mw_pipe.run(Stage.PRE_LLM, _mw_ctx)

                    # Handle compaction signal
                    compaction = _mw_ctx.data.get("compaction_needed")
                    if compaction in ("hard", "critical"):
                        self._compress_context()
                    elif compaction == "soft":
                        self._trim_context()

                    # Handle model routing suggestion
                    model_route = _mw_ctx.data.get("model_route")
                    if model_route and self.auto_route.get("enabled") and self.extra_llms:
                        route_label = str(self.auto_route.get(model_route, "")).strip()
                        if route_label:
                            for _slot in self.extra_llms:
                                if not isinstance(_slot, dict):
                                    continue
                                if str(_slot.get("label", "")).strip() == route_label:
                                    _sp = str(_slot.get("provider", "")).strip()
                                    _sm = str(_slot.get("model", "")).strip()
                                    if _sp or _sm:
                                        logger.info(
                                            "Agent %s: middleware model_route[%s] → %s/%s",
                                            self.id[:8], model_route,
                                            _sp or _eff_provider, _sm or _eff_model,
                                        )
                                        _eff_provider = _sp or _eff_provider
                                        _eff_model = _sm or _eff_model
                                    break
                except Exception as _mw_err:
                    logger.debug("pre_llm middleware skipped: %s", _mw_err)

                logger.info("Agent %s (%s) using provider=%s model=%s",
                            self.name, self.id[:8], _eff_provider, _eff_model)

                max_iters = 20

                # ── Per-turn tool-call budgets (anti infinite-loop) ──
                # Catches the failure mode where the agent hops between
                # web_search / mcp_call / web_fetch indefinitely without
                # converging. Iteration counter alone misses this because
                # one iteration can contain many tool calls.
                #
                # Soft cap → inject a budget-pressure system message
                #   ("你已经调用 X N 次,请基于已有结果给结论")
                # Hard cap → break out of the iteration loop with a
                #   user-visible "已强制终止" final response.
                #
                # Per-tool soft caps are tuned so research-heavy tools
                # (web_*, mcp_call) get more headroom than directed tools
                # (read_file, knowledge_lookup) which usually converge
                # in 2-3 calls.
                _PER_TOOL_SOFT_CAP = {
                    "web_search": 5, "web_fetch": 5, "mcp_call": 5,
                    "knowledge_lookup": 4, "memory_recall": 4,
                    "read_file": 6, "bash": 8,
                }
                _DEFAULT_SOFT_CAP = 4
                # 2026-04-30: We don't hard-cap on total count anymore. Long
                # research / coding tasks legitimately need 50+ tool calls.
                # Instead, at AUTH_THRESHOLD we synchronously request user
                # approval to extend the budget. Each approval grants +AUTH_BUMP.
                # MAX_EXTENSIONS bounds the absolute ceiling (130 by default)
                # to keep a runaway loop from being silently approved.
                _AUTH_THRESHOLD = int(
                    os.environ.get("TUDOU_TOOL_AUTH_THRESHOLD", "30") or 30)
                _AUTH_BUMP = int(
                    os.environ.get("TUDOU_TOOL_AUTH_BUMP", "20") or 20)
                _AUTH_MAX_EXTENSIONS = int(
                    os.environ.get("TUDOU_TOOL_AUTH_MAX_EXT", "5") or 5)
                # Per-turn local state — resets every chat() invocation.
                _budget_extensions_used = 0
                _budget_denied = False
                _milestone_announced: set[int] = set()
                # Bookkeeping / state-mutation tools — these have legit
                # reasons to be called many times in a turn (planning,
                # task tracking, milestone progress). They do no external
                # I/O and can't loop. Excluded from BOTH the per-tool cap
                # and the total cap so they don't kill a productive agent.
                _UNCOUNTED_TOOLS = frozenset({
                    "plan_update", "complete_step",
                    "task_update",
                    "submit_deliverable",
                    "create_goal", "update_goal_progress",
                    "create_milestone",
                    "update_milestone_status",
                    "update_milestone_responsibility",
                    "request_tool_budget",  # reserved for future request mechanism
                })
                _per_tool_count: dict[str, int] = {}
                _budget_warned: set[str] = set()
                # ── Per-(tool, args-signature) repeat detection ──
                # Loops aren't "called X 10 times" — they're "called X
                # with the *same args* 5 times". 5 web_searches with 5
                # different queries = legit research. 5 web_searches with
                # the same query = stuck. We track signature count per
                # turn and block the call BEFORE execution once a
                # signature is repeated past _REPEAT_HARD.
                #
                # _DEDUP_KEY[tool] extracts a normalized arg key. Tools
                # not in the table are NOT dedup-tracked (e.g. plan_update
                # is fine to call repeatedly with different deltas).
                # IMPORTANT: the keys here MUST match the actual tool
                # parameter names exposed in tools.py — otherwise a().get()
                # returns "" for every call, the signature collapses to
                # an empty string, and the dedup matches *every* call as
                # "same args" (false positive). Confirmed against
                # tools.py: bash uses `command` (not `cmd`), read_file
                # uses `path` (not `file_path`), search_files uses
                # `pattern`+`directory`, etc.
                _DEDUP_KEY: dict[str, callable] = {
                    "read_file": lambda a: f"path={a.get('path', '') or a.get('file_path', '')}",
                    "web_search": lambda a: f"q={' '.join(str(a.get('query', '')).lower().split())}",
                    "web_fetch": lambda a: f"url={a.get('url', '')}",
                    "web_screenshot": lambda a: f"url={a.get('url', '')}",
                    "knowledge_lookup": lambda a: f"q={' '.join(str(a.get('query', '')).lower().split())}",
                    "memory_recall": lambda a: f"q={' '.join(str(a.get('query', '')).lower().split())}",
                    "mcp_call": lambda a: (
                        f"mcp={a.get('mcp_id', '')}|tool={a.get('tool', '')}"
                        f"|args={json.dumps(a.get('arguments', {}) or {}, sort_keys=True, default=str)[:200]}"
                    ),
                    "bash": lambda a: f"cmd={' '.join(str(a.get('command', '') or a.get('cmd', '')).split())[:240]}",
                    "search_files": lambda a: f"q={a.get('pattern', '')}|dir={a.get('directory', '')}",
                    "glob_files": lambda a: f"pat={a.get('pattern', '')}|dir={a.get('directory', '')}",
                }
                # Soft = warn but allow; Hard = block this exact signature
                # but allow OTHER signatures of the same tool.
                _REPEAT_SOFT = 2  # 3rd identical call → soft warn
                _REPEAT_HARD = 4  # 5th identical call → block THIS sig
                _signature_count: dict[str, int] = {}
                _signature_warned: set[str] = set()
                # Per-MCP-id repeat counter — separate from signature
                # dedup (which keys on full mcp_id|tool|args). When the
                # LLM keeps hammering the SAME MCP server with slightly-
                # varying args (the 2026-04-28 incident: 13× calls to
                # mcp=42d8ca5e trying to send email), per-mcp count
                # catches it even though no two args are byte-identical.
                _mcp_id_count: dict[str, int] = {}
                _mcp_id_warned: set[str] = set()
                _MCP_REPEAT_SOFT = 3   # 4th call to same MCP → warn
                _MCP_REPEAT_HARD = 5   # 6th call → block this batch's calls to it

                # ── Task Checkpoint Injection: 任务恢复上下文 ──
                # [F3] 先老化 stale active plan，防止 phase 卡死
                try:
                    self._auto_stale_active_plans()
                except Exception as _stale_err:
                    logger.debug("auto_stale_active_plans failed: %s", _stale_err)

                # [F2] checkpoint 改为瞬态注入
                _checkpoint_ctx = ""
                if self.agent_phase in (AgentPhase.EXECUTING, AgentPhase.PLANNING):
                    _checkpoint_ctx = self._build_checkpoint_context()
                    if _checkpoint_ctx:
                        self._log("checkpoint_inject", {
                            "phase": self.agent_phase.value,
                            "chars": len(_checkpoint_ctx),
                            "transient": True,
                        })
                        self.history_log.add("checkpoint",
                                              f"[Checkpoint] 注入任务恢复上下文 phase={self.agent_phase.value}")

                # Build messages-to-send once per iteration: self.messages
                # (stable prefix) + dynamic context injected at the end.
                # This preserves LM Studio / Ollama KV cache across turns.
                _msgs_to_send = self._inject_dynamic_context(
                    self.messages, current_query=_user_text)

                # [F2] 瞬态插入 checkpoint（不污染 self.messages）
                if _checkpoint_ctx:
                    _last_user_idx = None
                    for _i in range(len(_msgs_to_send) - 1, -1, -1):
                        if _msgs_to_send[_i].get("role") == "user":
                            _last_user_idx = _i
                            break
                    _ctx_msg = {
                        "role": "system",
                        "content": _checkpoint_ctx,
                        "_dynamic": True,
                    }
                    if _last_user_idx is not None and _last_user_idx > 0:
                        _msgs_to_send.insert(_last_user_idx, _ctx_msg)
                    else:
                        _msgs_to_send.append(_ctx_msg)

                # ── Strip base64 images from older messages ──
                # Only the LAST user message (current turn) keeps its images.
                # Older images are replaced with a text placeholder to save
                # tokens and avoid confusing the model with stale images.
                _msgs_to_send = _strip_old_images(_msgs_to_send)
                # ── C: Hoist skill-guide bodies out of tool_results ──
                # Big SKILL.md pulled via get_skill_guide gets moved to ONE
                # system block at the front; tool_result becomes a short
                # placeholder. 省掉每轮重发几 KB 的 markdown,同时让它落到
                # prefix-cacheable 的位置。
                _msgs_to_send = _hoist_skill_guides(_msgs_to_send)
                # ── Multimodal: elide old image base64 ──
                # base64 images are 100K-1M+ chars each; the model has
                # already "seen" earlier turn's images and described
                # them in subsequent assistant text — we don't need to
                # re-send the bytes. Only keeps the LAST image-bearing
                # user turn intact; older images become a small
                # "[N image(s) from earlier turn — omitted]" placeholder.
                # Pure-text histories return unchanged (zero cost).
                # Was previously only wired into the iteration>0 chain
                # below — first call leaked all base64 through.
                _msgs_to_send = _strip_old_images(_msgs_to_send)
                # ── History summary — 最上位的裁剪,大幅压成一段摘要 ──
                # threshold: TUDOU_HISTORY_SUMMARY_CHARS (默认 30000 chars)
                # 或 tool 消息 > 8 个时触发; 结果 cached 在 agent 上。
                _msgs_to_send = _summarize_old_history(_msgs_to_send, self)
                # Same idea for stale tool bodies (web_fetch / search
                # results from earlier iterations). Keep the newest 4
                # in full; older ones get a 600-char head preview.
                _msgs_to_send = _compress_old_tool_results(_msgs_to_send)
                # Also trim OLD write_file / edit_file tool_call `content`
                # / `new_string` args — once the write committed, resending
                # the 10KB file body on every turn is pure waste.
                _msgs_to_send = _compress_old_write_tool_calls(_msgs_to_send)
                # Final safety: drop any orphan tool messages (DeepSeek strict).
                _msgs_to_send = _drop_orphan_tool_messages(_msgs_to_send)

                # ── Multimodal diagnostic: verify images survive pipeline ──
                if _is_multimodal:
                    _has_mm = any(
                        isinstance(m.get("content"), list)
                        for m in _msgs_to_send if m.get("role") == "user"
                    )
                    logger.info(
                        "MULTIMODAL CHECK: input=True, pipeline_preserved=%s, "
                        "provider=%s model=%s",
                        _has_mm, _eff_provider, _eff_model,
                    )
                    if on_event and _has_mm:
                        _img_count = sum(
                            1 for m in _msgs_to_send
                            if isinstance(m.get("content"), list)
                            for p in m["content"]
                            if isinstance(p, dict) and p.get("type") in (
                                "image_url", "image", "input_image")
                        )
                        _emit(AgentEvent(time.time(), "message", {
                            "role": "system",
                            "content": (
                                f"📎 {_img_count} image(s) received — "
                                f"sending to {_eff_provider}/{_eff_model}"
                            ),
                        }))

                # Vision models (e.g. llama3.2-vision) typically don't
                # support tool calling. When multimodal routing switched
                # to a dedicated vision model, disable tools unless the
                # model is known to support both (e.g. gpt-4o, claude-3.5).
                _effective_tools = tool_defs
                if (_is_multimodal
                        and (self.multimodal_provider or self.multimodal_model)
                        and not self.multimodal_supports_tools):
                    _effective_tools = None

                # (The previous "lock-out" approach was reverted —
                # legit deep research needs to keep going. The soft-cap
                # message + `request_tool_budget` self-service are now
                # the way to extend; nothing is filtered from the tools
                # list here.)

                # Narrator-stall nudge: at most one corrective injection per
                # turn, so a persistently-broken model can't pin us in a loop.
                # Track number of nudges this turn. Old code used a bool
                # `_nudged_this_turn = True` once and never nudged again,
                # but real LLMs (especially DeepSeek thinking-mode) often
                # stall multiple times per turn — narrator-stall on iter 5,
                # then again on iter 8 after a tool result. We allow up to
                # _MAX_NUDGES_PER_TURN nudges so each fresh stall gets a
                # corrective injection instead of breaking the loop.
                _nudge_count = 0
                _MAX_NUDGES_PER_TURN = 3

                # Plan D: handoff-trigger force. Only active on iteration 0,
                # only when handoff_request is actually in the tool list, and
                # only when the user message matches the trigger pattern.
                # Bypass with TUDOU_FORCE_HANDOFF=0.
                _forced_tool_choice = None
                try:
                    if (os.getenv("TUDOU_FORCE_HANDOFF", "1") != "0"
                            and _effective_tools
                            and _user_msg_triggers_handoff(_user_text)
                            and any(t.get("function", {}).get("name")
                                    == "handoff_request"
                                    for t in _effective_tools)):
                        _forced_tool_choice = {
                            "type": "function",
                            "function": {"name": "handoff_request"},
                        }
                        try:
                            self._log("handoff_force", {
                                "trigger_matched": True,
                                "user_text_preview": (_user_text or "")[:120],
                            })
                        except Exception:
                            pass
                except Exception:
                    _forced_tool_choice = None

                for iteration in range(max_iters):
                    if _is_aborted():
                        final_content = final_content or "[Aborted]"
                        break
                    # Rebuild messages-to-send each iteration (self.messages
                    # may have grown with tool results from previous iteration).
                    # Dynamic context is appended at the end — keeps prefix stable.
                    # Order matters: hoist skill-guides → summarize →
                    # compress tool results → compress old writes. Summary
                    # runs on already-hoisted array so guide bodies don't
                    # show up in the summary-transcript either.
                    if iteration > 0:
                        _msgs_to_send = _drop_orphan_tool_messages(
                            _compress_old_write_tool_calls(
                                _compress_old_tool_results(
                                    _summarize_old_history(
                                        _hoist_skill_guides(
                                            _strip_old_images(
                                                self._inject_dynamic_context(
                                                    self.messages,
                                                    current_query=_user_text))),
                                        self))))
                    # Strategy: always try streaming first (with tools).
                    # If the provider doesn't support streaming+tools,
                    # it falls back to non-streaming internally.
                    # For the first attempt when we have on_event, try
                    # streaming WITHOUT tools to get fast text output.
                    # If the model wants to call a tool, we retry with tools.

                    # Plan D: if we're forcing a tool call this iteration,
                    # the streaming-no-tools preflight is useless (it'd strip
                    # tools and free-text hallucinate). Skip straight to the
                    # non-stream tool-enabled path below.
                    _force_this_iter = (iteration == 0 and _forced_tool_choice)
                    if on_event and not _effective_tools and not _force_this_iter:
                        # Pure stream, no tools at all
                        try:
                            gen = llm.chat(
                                _msgs_to_send, tools=None, stream=True,
                                provider=_eff_provider, model=_eff_model,
                            )
                            content = ""
                            for chunk in gen:
                                if _is_aborted():
                                    break
                                content += chunk
                                evt = AgentEvent(time.time(), "text_delta",
                                                 {"content": chunk})
                                _emit(evt)
                            if _is_aborted():
                                final_content = content or "[Aborted]"
                                break
                            final_content = content
                            self.messages.append({"role": "assistant",
                                                  "content": content,
                                                  "_source": "llm"})
                            self._log("message",
                                      {"role": "assistant",
                                       "content": content,
                                       "source": "llm"})
                            break
                        except Exception:
                            pass  # Fall through

                    # Tool-capable LLM path. Prefer STREAMING when on_event
                    # is bound (UI wants live text_delta), falling back to
                    # non-streaming chat_no_stream only when forced
                    # tool_choice is in play (the streaming adapter doesn't
                    # support tool_choice — that's a first-iteration escape
                    # hatch and shouldn't be the common case).
                    #
                    # Previously this branch *always* called chat_no_stream,
                    # which meant the in-process agent.chat() path never
                    # emitted text_delta events — the UI only saw the full
                    # final message. Streaming was working in the isolated
                    # worker (agent_execution.py:1179) but not here. User
                    # reported: "chat页面没有全流程流形式输出" — only visible
                    # after page refresh. This route fixes that.
                    if _is_aborted():
                        final_content = final_content or "[Aborted]"
                        break
                    _can_stream = bool(on_event) and not (
                        iteration == 0 and _forced_tool_choice)
                    # _effective_temperature 来自 AgentLLMMixin; 某些旧 Agent
                    # 实例可能未继承到 (比如反序列化场景), 用 hasattr 兜底。
                    _temp = (self._effective_temperature()
                             if hasattr(self, "_effective_temperature")
                             else None)
                    try:
                        if _can_stream:
                            response = _stream_chat_to_response(
                                llm, _msgs_to_send, _effective_tools,
                                provider=_eff_provider, model=_eff_model,
                                temperature=_temp,
                                on_event=_emit,
                                is_aborted=_is_aborted,
                            )
                        else:
                            response = llm.chat_no_stream(
                                _msgs_to_send, tools=_effective_tools,
                                provider=_eff_provider, model=_eff_model,
                                tool_choice=(_forced_tool_choice
                                             if iteration == 0 else None),
                            )
                    except (ConnectionError, OSError) as conn_err:
                        # Provider unreachable — stop retrying immediately
                        raise RuntimeError(
                            f"LLM provider '{_eff_provider}' connection failed "
                            f"(model={_eff_model}): {conn_err}"
                        ) from conn_err
                    except Exception as llm_err:
                        # Other LLM errors (timeout, auth, etc.)
                        if "timeout" in str(llm_err).lower() or "timed out" in str(llm_err).lower():
                            raise RuntimeError(
                                f"LLM provider '{_eff_provider}' timed out "
                                f"(model={_eff_model}): {llm_err}"
                            ) from llm_err
                        raise
                    msg = response.get("message", {})
                    content = _ensure_str_content(msg.get("content"))
                    tool_calls = msg.get("tool_calls", [])
                    # DeepSeek thinking-mode: capture reasoning_content for
                    # round-trip (see agent_execution.py for full rationale).
                    _reasoning_content = msg.get("reasoning_content") or ""
                    # Canonical stop_reason plumbed from llm.py:
                    # end_turn | tool_use | length | stop_sequence | content_filter
                    stop_reason = response.get("stop_reason") or ""

                    # ── 碎碎念 / meta-promise guard ─────────────────────
                    # LLM 反复发 "好的,我来..."/"让我先..." 但不调工具 →
                    # 用户看到一串重复承诺。检测 + 抑制 + 纠偏 + 兜底放弃。
                    # (这段之前只在 agent_execution.py 的 chat 里有,但
                    # in-process 路径实际跑的是 agent.py 的 chat,所以漏了。)
                    _suppress_display = False
                    _dup_abort = False
                    try:
                        from .agent_execution import (
                            _text_similarity as _sim_fn,
                            _is_meta_promise as _meta_fn,
                        )
                        _prev = str(getattr(self, "_last_iter_content", "") or "")
                        _curr = str(content or "")
                        _sim = _sim_fn(_prev, _curr) if _prev and _curr else 0.0
                        _is_meta = (not tool_calls) and _meta_fn(_curr)
                        _trip = (_curr and len(_curr) > 20
                                 and (_sim >= 0.85 or _is_meta))
                        if _trip:
                            _dup_count = int(getattr(
                                self, "_dup_iter_count", 0)) + 1
                            self._dup_iter_count = _dup_count
                            _suppress_display = True
                            logger.warning(
                                "Agent %s: 碎碎念/meta-promise "
                                "(sim=%.2f, meta=%s, dup#%d)",
                                self.id[:8], _sim, _is_meta, _dup_count)
                            # 关键: text_delta 已经流到前端并画出气泡。
                            # push 一个 retract 事件让前端擦掉最新 assistant 气泡,
                            # 否则即使 suppress 了 message,用户还是看到那条"好的我先..."。
                            try:
                                _emit(AgentEvent(time.time(), "retract_last_assistant",
                                                 {"reason": "meta_promise"}))
                            except Exception:
                                pass
                            if _dup_count == 1:
                                # 第 1 次:注入纠偏 system,让下一轮看到并改
                                self.messages.append({
                                    "role": "system",
                                    "content": (
                                        "[SYSTEM] 你刚才这一轮的回复没有实际"
                                        "动作(纯承诺 / 重复'我先 / 让我先 / "
                                        "好的马上...')。\n"
                                        "下一轮必须做下面之一,不要再重复承诺:\n"
                                        "  (a) 直接调一个具体工具(write_file / "
                                        "read_file / bash / mcp_call / "
                                        "send_message / handoff),\n"
                                        "  (b) plan_update(complete_step/"
                                        "fail_step)更新状态,\n"
                                        "  (c) 真的不知道怎么做就返回 15 字内"
                                        "的明确提问。\n"
                                        "严禁再输出'交给我吧 / 让我先 / "
                                        "好的我来 / 马上发送'开场白而不做事。"
                                    ),
                                    "_dynamic": True,
                                    "_source": "dup_guard",
                                })
                            if _dup_count >= 2:
                                # 第 2 次:放弃这一轮,避免继续烧 token
                                logger.error(
                                    "Agent %s: 连续 2 次碎碎念 → 终止本轮",
                                    self.id[:8])
                                _dup_abort = True
                                final_content = (
                                    "⚠️ 我好像卡在重复承诺里了(连续 2 轮只说"
                                    "\"我来...\" 没调工具)。已自动停止。请把任务"
                                    "拆成一小步再提问,或检查是否有工具被拒。"
                                )
                        else:
                            # 重置计数,否则新对话一开始就被卡住
                            self._dup_iter_count = 0
                        # 记下本轮 content 供下一轮比对
                        self._last_iter_content = _curr
                    except Exception as _dup_err:
                        logger.debug("dup-guard failed: %s", _dup_err)

                    if content and not _suppress_display:
                        final_content = content
                        evt = AgentEvent(time.time(), "message",
                                         {"role": "assistant",
                                          "content": content})
                        self._log(evt.kind, evt.data)
                        _emit(evt)
                    if _dup_abort:
                        # 把兜底消息发给 UI 作为本轮最终回复
                        try:
                            _emit(AgentEvent(time.time(), "message",
                                             {"role": "assistant",
                                              "content": final_content}))
                        except Exception:
                            pass
                        break

                    # ── Surface truncation / filter issues ──────────────
                    # Distinct from "empty tool_calls" — these tell the user
                    # WHY the turn ended even when there's no visible error.
                    if stop_reason == "length":
                        try:
                            _emit(AgentEvent(time.time(), "llm_truncated",
                                             {"reason": "length",
                                              "model": _eff_model,
                                              "hint": "Output hit max_tokens. "
                                                      "Increase max_tokens or "
                                                      "ask the model to be "
                                                      "more concise."}))
                        except Exception:
                            pass
                    elif stop_reason == "content_filter":
                        try:
                            _emit(AgentEvent(time.time(), "llm_filtered",
                                             {"reason": "content_filter",
                                              "model": _eff_model}))
                        except Exception:
                            pass

                    # NOTE: text-to-tool-call extraction (for models that
                    # output JSON as text instead of tool_calls) is handled
                    # in llm.py's _normalize_response_tool_calls(), so
                    # tool_calls here is already normalised.

                    if not tool_calls:
                        # ── Narrator-stall nudge ──────────────────────────
                        # The model replied with a promise ("Let me fix it:")
                        # but didn't call any tool.  If it still has tools
                        # available AND we haven't nudged yet AND we have
                        # budget for another iteration, inject a correction
                        # instead of breaking.
                        #
                        # Gate: skip nudge when stop_reason is "length" —
                        # that's truncation, not a stall (the model WANTED
                        # to keep going; injecting "call the tool" would
                        # just waste tokens on a truncated continuation).
                        # Nudge condition: tools available, under max nudge cap,
                        # haven't run out of iterations, not truncated, and EITHER
                        #   (a) text looks like a narrator stall ("让我先..."), OR
                        #   (b) content is essentially empty (no concrete output)
                        #       which is the most common DeepSeek stall mode —
                        #       "thinking" succeeded but output got dropped.
                        # The "essentially empty" branch covers what
                        # `_looks_like_narrator_stall` misses (it requires a
                        # specific colon-ending pattern).
                        _content_stripped = (content or "").strip()
                        _is_empty_response = len(_content_stripped) < 20
                        _is_stall = _looks_like_narrator_stall(content)
                        if (_effective_tools
                                and _nudge_count < _MAX_NUDGES_PER_TURN
                                and iteration < max_iters - 1
                                and stop_reason != "length"
                                and stop_reason != "content_filter"
                                and os.environ.get(
                                    "TUDOU_NUDGE_WEAK_MODELS", "1") != "0"
                                and (_is_stall or _is_empty_response)):
                            # Persist the stall reply so the next LLM call
                            # sees its own words — makes the correction feel
                            # like a direct continuation instead of a reset.
                            self.messages.append({
                                "role": "assistant",
                                "content": content,
                                "_source": "llm",
                            })
                            self._log("message",
                                      {"role": "assistant",
                                       "content": content,
                                       "source": "llm"})
                            # Inject a user-role correction.  User-role (not
                            # system) because mid-turn system insertions
                            # confuse some providers; user-role corrections
                            # are the pattern OpenAI's Cookbook recommends
                            # for self-repair loops.
                            nudge = (
                                "[system nudge] 你上一条消息以 \"让我…：\" / "
                                "\"Let me …:\" 结尾，但没有调用任何工具。"
                                "请立即调用相应工具完成你承诺的动作 —— "
                                "不要重复宣告意图。Call the tool now; "
                                "do not re-narrate."
                            )
                            self.messages.append({
                                "role": "user",
                                "content": nudge,
                                "_source": "system_nudge",
                            })
                            _nudge_count += 1
                            # Rebuild the outbound message list so the next
                            # iteration picks up the two new messages.
                            _msgs_to_send = _drop_orphan_tool_messages(
                                _compress_old_tool_results(
                                    _summarize_old_history(
                                        _hoist_skill_guides(
                                            _strip_old_images(
                                                self._inject_dynamic_context(
                                                    self.messages,
                                                    current_query=_user_text))),
                                        self)))
                            # Log for observability
                            try:
                                _emit(AgentEvent(time.time(), "nudge",
                                                 {"reason": "narrator_stall",
                                                  "iteration": iteration}))
                            except Exception:
                                pass
                            continue

                        # Final response — ensure we always emit something
                        if not content and final_content:
                            # LLM returned empty final response but we had
                            # intermediate content — re-emit the last known content
                            evt = AgentEvent(time.time(), "message",
                                             {"role": "assistant",
                                              "content": final_content})
                            self._log(evt.kind, evt.data)
                            _emit(evt)
                        self.messages.append({"role": "assistant",
                                              "content": content or final_content,
                                              "_source": "llm"})
                        break

                    assistant_msg: dict = {"role": "assistant",
                                           "content": content,
                                           "_source": "llm"}
                    assistant_msg["tool_calls"] = tool_calls
                    if _reasoning_content:
                        assistant_msg["reasoning_content"] = _reasoning_content
                    self.messages.append(assistant_msg)

                    # Check if all tool calls are parallel-safe
                    all_parallel_safe = all(
                        tc.get("function", {}).get("name", "unknown") in PARALLEL_SAFE_TOOLS
                        for tc in tool_calls
                    )

                    # ── Per-tool budget tracking (2026-04-28 / 2026-04-30) ──
                    # No hard kills on count alone — long research / coding
                    # tasks legitimately need many tool calls. Real loop
                    # guard is signature dedup (same-args repeats).
                    #
                    # Counters here drive:
                    #   • Soft warnings at the per-tool soft cap (one
                    #     time per tool, advising the agent to wrap up).
                    #   • Total-count milestones (every 10 calls) — a
                    #     short system message so the LLM SEES its own
                    #     pace and self-regulates.
                    #   • _AUTH_THRESHOLD — synchronously requests
                    #     user approval to extend the budget.
                    #   • Telemetry / log lines so admins can see what
                    #     a turn actually used.
                    # _capped_tools is no longer populated for total / per-
                    # tool counts; it's left as an empty placeholder for
                    # downstream dispatch logic that still references it.
                    _capped_tools: dict[str, str] = {}
                    for tc in tool_calls:
                        _nm = tc.get("function", {}).get("name", "?")
                        if _nm in _UNCOUNTED_TOOLS:
                            continue
                        _per_tool_count[_nm] = _per_tool_count.get(_nm, 0) + 1
                    _total = sum(_per_tool_count.values())
                    # Total-count milestone — give the LLM visibility on
                    # its own pace. Once per 10-call boundary; cumulative
                    # across the turn. Cheap (~30 tokens each).
                    _milestone = (_total // 10) * 10
                    if _milestone >= 10 and _milestone not in _milestone_announced:
                        _milestone_announced.add(_milestone)
                        _eff_threshold = _AUTH_THRESHOLD + (
                            _budget_extensions_used * _AUTH_BUMP)
                        try:
                            self.messages.append({
                                "role": "system",
                                "content": (
                                    f"[工具用量] 本回合已调用 **{_total}** 次工具。"
                                    f"超过 {_eff_threshold} 次将向用户请求继续授权，"
                                    f"如能基于现有结果收尾请优先 submit_deliverable / "
                                    f"在 reply 中说明结论。"
                                ),
                            })
                        except Exception:
                            pass
                        logger.info(
                            "Agent %s: tool-call milestone total=%d breakdown=%s",
                            self.id[:8], _total, dict(_per_tool_count),
                        )

                    # ── Budget authorization check ──
                    # If user already denied this turn, block all new tool
                    # calls outright; LLM gets a clear "wrap up" message.
                    if _budget_denied:
                        for tc in tool_calls:
                            _cid = tc.get("id", "")
                            _nm = tc.get("function", {}).get("name", "")
                            if _nm and _nm not in _UNCOUNTED_TOOLS and _cid:
                                _capped_tools.setdefault(
                                    _nm,
                                    "用户拒绝继续工具调用，请基于已有结果给最终回复",
                                )
                    else:
                        _eff_threshold = _AUTH_THRESHOLD + (
                            _budget_extensions_used * _AUTH_BUMP)
                        if (_total >= _eff_threshold
                                and _budget_extensions_used < _AUTH_MAX_EXTENSIONS):
                            # Synchronously request user approval to extend.
                            # Pattern matches the per-tool risky-call
                            # approval flow in agent_execution.py — we use
                            # the same PendingApproval primitive so portal
                            # UI surfaces it alongside other approvals.
                            decision = self._request_budget_extension(
                                _total, _AUTH_BUMP, _budget_extensions_used,
                            )
                            if decision == "approved":
                                _budget_extensions_used += 1
                            else:
                                _budget_denied = True
                                # Block this batch's new calls too — user
                                # explicitly said no more.
                                for tc in tool_calls:
                                    _cid = tc.get("id", "")
                                    _nm = tc.get("function", {}).get("name", "")
                                    if (_nm and _nm not in _UNCOUNTED_TOOLS
                                            and _cid):
                                        _capped_tools.setdefault(
                                            _nm,
                                            "用户拒绝继续工具调用，请基于已有结果给最终回复",
                                        )
                    # Soft cap warnings — single nudge per tool when it
                    # first crosses the soft threshold. NOT a block:
                    # long research tasks may legitimately keep going.
                    # Tells the agent to either wrap up OR acknowledge
                    # in its reply why more is needed (audit trail).
                    # Real loop protection is signature dedup below.
                    for _nm, _cnt in _per_tool_count.items():
                        _soft = _PER_TOOL_SOFT_CAP.get(_nm, _DEFAULT_SOFT_CAP)
                        if _cnt >= _soft and _nm not in _budget_warned:
                            _budget_warned.add(_nm)
                            try:
                                self.messages.append({
                                    "role": "system",
                                    "content": (
                                        f"[预算提醒] 工具 `{_nm}` 已调用 {_cnt} 次"
                                        f"(参考软上限 {_soft})。\n"
                                        f"如果你已经收集够,优先收尾:基于现有内容给最终结论 / "
                                        f"submit_deliverable / 在 reply 里 @ 队友。\n"
                                        f"如果**确实**还要继续(比如长任务多模块覆盖),"
                                        f"在你下一段回复正文里**用一句话说明为什么** —— "
                                        f"这等于自我授权,系统不再拦截。但记住:"
                                        f"**不要用相同参数重复调**(同 query / file_path / url "
                                        f"超过 4 次会被签名 dedup 自动拦)。"
                                    ),
                                })
                            except Exception:
                                pass
                            logger.info(
                                "Agent %s: soft-cap budget warning for %s "
                                "(%d/%d)", self.id[:8], _nm, _cnt, _soft)

                    # Parse all tool calls first
                    parsed_calls = []  # list of (name, arguments, call_id)
                    for tc in tool_calls:
                        func_info = tc.get("function", {})
                        name = func_info.get("name", "unknown")
                        call_id = tc.get("id", f"call_{uuid.uuid4().hex[:8]}")
                        arguments = func_info.get("arguments", {})
                        if isinstance(arguments, str):
                            try:
                                arguments = json.loads(arguments)
                            except (json.JSONDecodeError, TypeError):
                                arguments = {}
                        # 双重保护：解析后仍然不是 dict
                        if not isinstance(arguments, dict):
                            try:
                                arguments = json.loads(str(arguments))
                            except (json.JSONDecodeError, TypeError, ValueError):
                                arguments = {"raw": str(arguments)}
                        parsed_calls.append((name, arguments, call_id))

                    # ── Block list: signature dedup + per-tool cap ──
                    # Both guards funnel into _blocked_calls so the
                    # parallel/sequential executors below can short-
                    # circuit blocked calls uniformly.
                    _blocked_calls: dict[str, str] = {}  # call_id -> error msg
                    # Apply per-tool cap blocks first.
                    if _capped_tools:
                        for _name, _args, _cid in parsed_calls:
                            if _name in _capped_tools:
                                _blocked_calls[_cid] = _capped_tools[_name]
                    # ── Signature-based repeat detection ──
                    # For tools in _DEDUP_KEY, compute a normalized signature
                    # of (tool, key-args) and bump the per-turn counter.
                    # If a signature crosses _REPEAT_HARD, REPLACE its tool
                    # call with a synthetic error result before execution —
                    # the LLM sees "you already called this with these exact
                    # args N times; vary or stop" and naturally moves on.
                    # Soft threshold injects a one-time hint.
                    # ── Per-mcp_id repeat check ──
                    # Run BEFORE the per-arg signature loop so a hammered
                    # MCP gets the right error (clearer message than the
                    # generic "same params" one). Only counts mcp_call
                    # tool calls; one bump per call regardless of args.
                    for _name, _args, _cid in parsed_calls:
                        if _name != "mcp_call":
                            continue
                        _mid = (_args.get("mcp_id") or "").strip()
                        # list_mcps and the intra-agent redirects have
                        # already filtered earlier; skip empty mcp_id.
                        if not _mid or _args.get("list_mcps"):
                            continue
                        _mcp_id_count[_mid] = _mcp_id_count.get(_mid, 0) + 1
                        _mcnt = _mcp_id_count[_mid]
                        if _mcnt > _MCP_REPEAT_HARD:
                            _blocked_calls[_cid] = (
                                f"[Loop guard] 你已对 MCP `{_mid}` 发起 {_mcnt} 次调用,"
                                f"超过单 MCP 阈值 {_MCP_REPEAT_HARD}。\n"
                                f"该 MCP 之前的返回应该已经告诉你它能/不能做什么 —— "
                                f"反复硬调说明:\n"
                                f"  • 这个 MCP 不是你想要的工具(目标功能在别处) → "
                                f"不要再调它,改用 built-in 工具或换思路\n"
                                f"  • 或参数没填对 → 仔细看上次返回的错误信息\n"
                                f"\n如果你在试图给队友发消息,**用 reply 里 @ 提及**或 "
                                f"`send_message(to_agent=<id>, ...)`,**不是** mcp_call。\n"
                                f"如果是其它意图,基于已有信息收尾 / submit_deliverable / 给结论。"
                            )
                            logger.warning(
                                "Agent %s: MCP repeat-block mcp=%s count=%d",
                                self.id[:8], _mid[:8], _mcnt,
                            )
                        elif _mcnt > _MCP_REPEAT_SOFT and _mid not in _mcp_id_warned:
                            _mcp_id_warned.add(_mid)
                            try:
                                self.messages.append({
                                    "role": "system",
                                    "content": (
                                        f"[预算提醒] 你对 MCP `{_mid}` 已经发起 {_mcnt} 次调用。"
                                        f"再来 {_MCP_REPEAT_HARD - _mcnt + 1} 次,"
                                        f"系统就会自动拦截后续对该 MCP 的调用。\n"
                                        f"如果之前的返回已经明确这个 MCP 解决不了你的问题,"
                                        f"现在就停下,换工具或收尾。"
                                    ),
                                })
                            except Exception:
                                pass

                    for _name, _args, _cid in parsed_calls:
                        _key_fn = _DEDUP_KEY.get(_name)
                        if _key_fn is None:
                            continue
                        try:
                            _sig_inner = _key_fn(_args) or ""
                        except Exception:
                            _sig_inner = ""
                        # Skip if the signature has no actual content —
                        # e.g. {tool}_KEY returned "path=" with nothing
                        # after the equals (LLM passed unknown arg name,
                        # or our key lookup is misconfigured). Counting
                        # those would falsely block every call as "same
                        # empty args".
                        _sig_value = _sig_inner.split("=", 1)[-1].strip()
                        if not _sig_inner or not _sig_value:
                            continue
                        _sig = f"{_name}::{_sig_inner}"
                        _signature_count[_sig] = _signature_count.get(_sig, 0) + 1
                        _scnt = _signature_count[_sig]
                        if _scnt > _REPEAT_HARD:
                            _blocked_calls[_cid] = (
                                f"[Loop guard] 你已用**完全相同的参数**调用 `{_name}` "
                                f"{_scnt} 次,系统判定为重复死循环,已拦截。\n"
                                f"参数签名: `{_sig_inner[:160]}`\n"
                                f"如果还需要这类信息,请**换参数**(不同 query / 不同 "
                                f"file_path / 不同 url)。如果换不了,说明已找不到 —— "
                                f"基于已有信息给结论,或明确告诉用户「需要补充 X」。"
                            )
                            logger.warning(
                                "Agent %s: signature loop blocked tool=%s sig=%r count=%d",
                                self.id[:8], _name, _sig_inner[:80], _scnt,
                            )
                        elif _scnt > _REPEAT_SOFT and _sig not in _signature_warned:
                            _signature_warned.add(_sig)
                            try:
                                self.messages.append({
                                    "role": "system",
                                    "content": (
                                        f"[Loop guard 提醒] 工具 `{_name}` 用同样的参数调用过 "
                                        f"{_scnt} 次了 (`{_sig_inner[:120]}`)。"
                                        f"再多一次同样参数会被自动拦截。请换参数,或基于已有结果收尾。"
                                    ),
                                })
                            except Exception:
                                pass
                            logger.info(
                                "Agent %s: signature soft warn tool=%s sig=%r count=%d",
                                self.id[:8], _name, _sig_inner[:80], _scnt,
                            )

                    # Execute in parallel if all tools are safe, otherwise sequential
                    if all_parallel_safe and len(parsed_calls) > 1:
                        def _execute_single_tool(name_args_id):
                            name, arguments, call_id = name_args_id
                            # Loop-guard short-circuit: blocked signature
                            # returns synthetic error without hitting the tool.
                            if call_id in _blocked_calls:
                                return name, _blocked_calls[call_id], call_id
                            # Inject caller agent ID
                            if name in ("team_create", "send_message", "task_update",
                                        "mcp_call", "bash", "write_file", "edit_file",
                                        "submit_deliverable", "create_goal",
                                        "update_goal_progress", "create_milestone",
                                        "update_milestone_status"):
                                arguments["_caller_agent_id"] = self.id
                                try:
                                    from .tools import _get_current_scope
                                    _scope = _get_current_scope()
                                    if _scope.get("project_id"):
                                        arguments["_project_id"] = _scope["project_id"]
                                    if _scope.get("meeting_id"):
                                        arguments["_meeting_id"] = _scope["meeting_id"]
                                except Exception:
                                    pass
                            # Execute
                            if name == "plan_update":
                                return name, self._handle_plan_update(arguments), call_id
                            elif name == "request_web_login":
                                return name, self._handle_web_login_request(
                                    arguments, on_event=on_event), call_id
                            elif name == "handoff_request":
                                return name, self._handle_handoff_request(
                                    arguments, on_event=on_event), call_id
                            else:
                                return name, self._execute_tool_with_policy(
                                    name, arguments, on_event=on_event), call_id

                        with concurrent.futures.ThreadPoolExecutor(
                            max_workers=MAX_PARALLEL_WORKERS
                        ) as executor:
                            futures = [
                                executor.submit(_execute_single_tool, (name, arguments, call_id))
                                for name, arguments, call_id in parsed_calls
                            ]
                            results = []
                            for future in concurrent.futures.as_completed(futures):
                                try:
                                    name, result, call_id = future.result()
                                    result = self._handle_large_result(name, result)
                                    results.append((name, result, call_id))
                                except Exception as e:
                                    logger.error(f"Parallel tool execution error: {e}")
                                    results.append(("unknown", f"Error: {e}", f"call_{uuid.uuid4().hex[:8]}"))
                    else:
                        # Sequential execution
                        results = []
                        for name, arguments, call_id in parsed_calls:
                            if _is_aborted():
                                break

                            evt = AgentEvent(time.time(), "tool_call",
                                             {"name": name,
                                              "arguments": _truncate_dict(arguments)})
                            self._log(evt.kind, evt.data)
                            _emit(evt)

                            # Loop-guard short-circuit: blocked signature
                            # returns the synthetic error without hitting
                            # the actual tool handler.
                            if call_id in _blocked_calls:
                                result = _blocked_calls[call_id]
                                result = self._handle_large_result(name, result)
                                results.append((name, result, call_id))
                                continue

                            # Inject caller agent ID for tools that need agent context
                            if name in ("team_create", "send_message", "task_update",
                                        "mcp_call", "bash", "write_file", "edit_file"):
                                arguments["_caller_agent_id"] = self.id

                            # Handle plan_update internally (needs agent context)
                            if name == "plan_update":
                                result = self._handle_plan_update(arguments)
                                _emit(AgentEvent(time.time(), "plan_update",
                                                 {"plan": self.get_current_plan()}))
                            elif name == "request_web_login":
                                result = self._handle_web_login_request(
                                    arguments, on_event=on_event)
                            elif name == "handoff_request":
                                result = self._handle_handoff_request(
                                    arguments, on_event=on_event)
                            else:
                                result = self._execute_tool_with_policy(
                                    name, arguments, on_event=on_event)

                            # Handle large results
                            result = self._handle_large_result(name, result)

                            results.append((name, result, call_id))

                    # Process and emit all results
                    for name, result, call_id in results:
                        # Ensure result is always a string for safe operations
                        result_str = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)

                        evt = AgentEvent(time.time(), "tool_result",
                                         {"name": name, "result": result_str[:1000]})
                        self._log(evt.kind, evt.data)
                        _emit(evt)

                        # === 记录 Agent 自身操作到记忆 ===
                        self._record_tool_action(name, result_str)

                        # P0-B: Auto-spill large tool results to an
                        # artifact file. Replaces the in-message content
                        # with a compact ref + 300-char preview so the
                        # LLM can still reason on the outcome without
                        # paying for 5-10k tokens of raw HTML / CSV /
                        # log output every turn. Agent can call
                        # read_file(path) if it needs the full text.
                        result_str = self._maybe_spill_tool_result(
                            name, result_str, call_id,
                        )

                        # Check and inject budget pressure note
                        budget_note = llm.get_budget_pressure_note(iteration, max_iters)
                        if budget_note:
                            result_content = result_str + "\n\n" + budget_note
                        else:
                            result_content = result_str

                        self.messages.append({
                            "role": "tool",
                            "content": result_content,
                            "tool_call_id": call_id,
                        })

                        # --- agent_state shadow (phase-1 grey rollout) ---
                        try:
                            _shadow = getattr(self, "_shadow", None)
                            if _shadow is not None:
                                _shadow.record_tool_result(name, result_str)
                        except Exception:
                            pass

                os.chdir(old_cwd)
                self.status = AgentStatus.IDLE

                # --- src integration: track cost & history ---
                if final_content:
                    in_tokens = len(_user_text.split())
                    out_tokens = len(final_content.split())
                    self.total_input_tokens += in_tokens
                    self.total_output_tokens += out_tokens
                    apply_cost_hook(self.cost_tracker,
                                    f"chat:{self.id[:6]}", in_tokens + out_tokens)
                    self.history_log.add("chat_done",
                                         f"[LLM] in={in_tokens} out={out_tokens} total_cost={self.cost_tracker.total_units}")
                    # Track in QueryEngine if initialized
                    if self._query_engine is not None:
                        self._query_engine.total_usage = UsageSummary(
                            input_tokens=self.total_input_tokens,
                            output_tokens=self.total_output_tokens,
                        )
                    # Auto-compact transcript if it's getting large
                    if len(self.transcript.entries) > self.profile.max_context_messages:
                        self.compact_memory()

                    # --- Enhancement module: auto-learn from interaction ---
                    if self.enhancer and self.enhancer.enabled:
                        try:
                            learn_result = self.enhancer.learn_from_interaction(
                                user_message=_user_text,
                                agent_response=final_content[:500],
                                outcome="success",
                            )
                            # 将自我学习的成果也写入 L3 记忆 (向量化)
                            if learn_result:
                                self._sync_enhancement_to_memory(learn_result)
                        except Exception:
                            pass

                    # --- Execution Analyzer: auto-analysis after chat ---
                    try:
                        analysis = analyze_and_grow(
                            self,
                            task_id=f"chat_{int(self._chat_start_time)}",
                            start_time=self._chat_start_time,
                        )
                        if analysis and self._active_skill_ids:
                            registry = get_prompt_pack_registry()
                            tools_used = [e.data.get("tool", "") for e in self.events[-50:]
                                          if e.kind == "tool_call"]
                            for sid in self._active_skill_ids:
                                applied = len(tools_used) > 0
                                registry.mark_skill_applied(
                                    sid, applied=applied,
                                    task_completed=analysis.task_completed)
                    except Exception:
                        pass  # auto-analysis is optional

                    # ── RolePresetV2 Post-hook: QualityGate + SOP evaluate + KPI ──
                    try:
                        v2_version = getattr(self.profile, "role_preset_version", 1)
                        if v2_version == 2 and final_content:
                            _tools_used_list = [
                                e.data.get("name", "") for e in self.events[-50:]
                                if e.kind == "tool_call"
                            ]
                            final_content = self._run_quality_gate_with_retry(
                                final_content, _user_text, _tools_used_list,
                                _emit=_emit,
                            )
                            # SOP post-hook: evaluate exit and advance
                            if getattr(self, "_active_sop_instance", None):
                                try:
                                    from .role_sop import get_sop_manager
                                    sop_mgr = get_sop_manager()
                                    inst = self._active_sop_instance
                                    status = sop_mgr.evaluate_exit(inst, final_content)
                                    self._log("sop_stage_eval", {
                                        "sop_id": inst.sop_id,
                                        "stage_id": inst.current_stage,
                                        "status": status,
                                        "instance_id": inst.instance_id,
                                    })
                                except Exception as _sop_post_err:
                                    logger.debug("SOP post-hook skipped: %s", _sop_post_err)
                            try:
                                self._record_kpis_and_experience(
                                    final_content, _user_text, _tools_used_list,
                                )
                            except Exception as _kpi_err:
                                logger.debug("KPI recording skipped: %s", _kpi_err)
                    except Exception as _v2_err:
                        logger.debug("RolePresetV2 post-hook skipped: %s", _v2_err)

                    # --- Three-layer memory: post-response write-back ---
                    self._memory_write_back(_user_text, final_content)

                    # --- Update state machine phase ---
                    self._update_agent_phase()

            except Exception as e:
                # Per-agent error isolation: log the error but recover to IDLE
                # so this agent remains usable and doesn't block the system.
                # Include the full traceback in the log so post-incident
                # debugging doesn't need a repro — chat errors are
                # user-impacting and rare enough that the extra log noise
                # is worth it.
                import traceback as _tb
                _exc_tb = _tb.format_exc()
                evt = AgentEvent(time.time(), "error", {
                    "error": str(e),
                    "traceback": _exc_tb[-2000:],  # tail to avoid 50KB events
                })
                self._log(evt.kind, evt.data)
                _emit(evt)
                logger.error("Agent %s (%s) chat error: %s\n%s",
                              self.name, self.id, e, _exc_tb)
                try:
                    os.chdir(old_cwd)
                except Exception:
                    pass
                final_content = f"Error: {e}"
                # Recover to IDLE — the error is recorded in events/history,
                # but the agent should not stay in ERROR permanently.
                self.status = AgentStatus.IDLE
                # --- agent_state shadow (phase-1 grey rollout) ---
                try:
                    _shadow = getattr(self, "_shadow", None)
                    if _shadow is not None:
                        _shadow.record_error(e)
                except Exception:
                    pass

            # --- agent_state shadow (phase-1 grey rollout) ---
            # Final assistant turn — closes the current shadow task.
            try:
                _shadow = getattr(self, "_shadow", None)
                if _shadow is not None:
                    _shadow.record_assistant(final_content or "")
            except Exception:
                pass

            # ── 新 A.8: attach memory_recall refs to the final assistant
            # message so the UI can render a 🧠 badge + per-ref delete
            # buttons. _turn_memory_refs was accumulated by
            # _tool_memory_recall across this turn's tool calls.
            #
            # IMPORTANT (user-requested): only surface the badge when the
            # final assistant reply actually USED the memory — not just
            # "agent called memory_recall but the hits were irrelevant".
            # We use a cheap bigram-Jaccard overlap test between each
            # ref's content_preview and the final assistant text; only
            # refs above ``_MEMORY_USED_SIM_THRESHOLD`` get kept. The
            # button is the user's "delete this wrong memory" escape
            # hatch — we want it pointing at memories that genuinely
            # contributed to what's on screen.
            try:
                bucket = getattr(self, "_turn_memory_refs", None) or []
                if bucket:
                    used_refs = self._filter_memory_refs_by_usage(
                        bucket, final_content or "")
                    if used_refs:
                        for i in range(len(self.messages) - 1, -1, -1):
                            m = self.messages[i]
                            if m.get("role") != "assistant":
                                continue
                            if m.get("tool_calls"):
                                continue
                            m["memory_refs"] = list(used_refs)
                            break
                        try:
                            self._log("memory_refs", {"refs": list(used_refs)})
                        except Exception:
                            pass
            except Exception as _mref_err:
                logger.debug("attach memory_refs skipped: %s", _mref_err)
            # Clear the bucket for the next turn regardless of outcome.
            try:
                self._turn_memory_refs = []
            except Exception:
                pass

            # ── Persist chat turn immediately ──────────────────────
            # Was: _auto_save_check() with 60s throttle — if user closes
            # browser or uvicorn restarts within 60s of a chat, the entire
            # turn (messages + events) was lost from disk. Observed: 小专
            # had 0 messages persisted because every save attempt was
            # throttled. Now we always flush after a turn finishes;
            # _auto_save_check throttle only protects mid-turn saves.
            try:
                from .hub import get_hub as _get_hub
                _hub = _get_hub()
                if _hub is not None:
                    self.save_memory()
                    _hub._save_agent_workspace(self)
                    _hub._save_agents()  # also flush SQLite + JSON
                    self._last_save_time = time.time()
            except Exception as _save_err:
                logger.debug("post-turn save failed: %s", _save_err)
            # Clear streaming buffer — the turn's complete reply now
            # lives in self.messages / final_content. Leaving the
            # buffer set would make pollers incorrectly believe the
            # agent is still typing.
            self._streaming_buffer = ""
            return final_content

    def _chat_async_via_langgraph(
        self, user_message, source: str = "admin"
    ) -> ChatTask:
        """LangGraph dispatch for chat_async (opt-in path).

        Returns a ChatTask just like the V1 path so callers / portal
        UI don't need to know which engine handled the turn. The graph
        runs in a worker thread; ChatTask status updates as the graph
        progresses through assistant/tool nodes; final assistant text
        becomes the task's result.

        Hand-rolls the minimum to bridge:
          - resolve agent's (provider, model) via existing V1 resolver
            (so LLMRouter / coding / multimodal logic still applies)
          - run ``app.graph.agent_chat_graph.run_chat`` in a thread
          - mirror final messages into ``self.messages`` (so chat
            history / memory / wiki paths see the turn)
          - emit a single ``message`` event for the assistant reply

        Failure inside the graph is re-raised so the outer ``chat_async``
        wrapper can fall back to V1 — caller never gets a half-broken
        ChatTask.
        """
        import threading
        from .chat_task import get_chat_task_manager, ChatTaskStatus

        # Resolve LLM via existing V1 path (LLMRouter + multimodal +
        # registry checks). One source of truth for provider selection.
        provider, model = self._resolve_effective_provider_model(user_message)
        if not provider or not model:
            raise RuntimeError(
                "agent has no LLM bound — configure provider/model first"
            )

        # Compose user content the same way V1 does (string or
        # multimodal list passes through).
        user_content = user_message

        mgr = get_chat_task_manager()
        task = mgr.create_task(self.id, str(user_content)[:200])
        task.set_status(ChatTaskStatus.THINKING,
                        "🚀 LangGraph dispatch", 5)

        # Snapshot agent state the graph needs.
        msgs_before = list(self.messages)

        def _run_graph() -> None:
            try:
                from .graph.agent_chat_graph import build_chat_graph
                task.set_status(ChatTaskStatus.STREAMING,
                                "graph: running", 30)
                # Build initial messages: existing history + new user msg.
                initial_msgs = msgs_before + [{
                    "role": "user",
                    "content": user_content,
                    "source": source,
                }]
                graph = build_chat_graph()
                # Pass `_agent_ref` through state so graph nodes can use
                # the real Agent's system_prompt / tool dispatch / sandbox /
                # approval logic — not the mock-style standalone path.
                # LangGraph's recursion_limit counts super-steps, NOT
                # tool-call iterations. Each (assistant → tools) round
                # is ~2 super-steps, so default 25 caps us at ~12 tool
                # rounds — too low for real work. Set to 4× our
                # max_iterations cap so the *agent's* iter limit always
                # bites first (with a clearer error than LangGraph's).
                _max_iter = int(getattr(self, "max_turns", 20) or 20)
                final = graph.invoke(
                    {
                        "messages": initial_msgs,
                        "agent_id": self.id,
                        "role": self.role,
                        "name": self.name,
                        "language": getattr(self.profile, "language", "auto") or "auto",
                        "provider": provider,
                        "model": model,
                        "iteration": 0,
                        "max_iterations": _max_iter,
                        "last_finish_reason": "",
                        "_agent_ref": self,    # ← bridge into V1 business logic
                    },
                    config={"recursion_limit": max(50, _max_iter * 4)},
                )

                # Mirror final messages back into agent.messages so the
                # rest of the platform (memory / wiki / portal history)
                # sees the turn.
                final_msgs = final.get("messages") or []
                # The graph appends to history via additive reducer —
                # find what's NEW relative to msgs_before + the user msg.
                # Simple approach: take everything past len(msgs_before).
                new_tail = final_msgs[len(msgs_before):]
                # Cleanup: don't persist orphan tool messages or empty
                # content into agent.messages — otherwise next turn the
                # same orphan ships to the LLM and trips a 400.
                self.messages = cleanup_message_history(
                    list(self.messages) + new_tail,
                    log_label=f"agent.{self.id[:8]}.mirror",
                )

                # Last assistant text becomes the chat reply.
                final_text = ""
                for m in reversed(final_msgs):
                    if (m.get("role") == "assistant"
                            and not m.get("tool_calls")):
                        c = m.get("content")
                        if isinstance(c, str) and c.strip():
                            final_text = c
                            break

                task.result = final_text
                task.set_status(ChatTaskStatus.COMPLETED,
                                "graph: done", 100)
            except Exception as e:
                logger.exception(
                    "Agent %s: LangGraph chat failed: %s", self.id[:8], e,
                )
                task.set_status(ChatTaskStatus.FAILED,
                                f"graph error: {e}", 100)

        threading.Thread(target=_run_graph, daemon=True,
                          name=f"langgraph-{self.id[:8]}").start()
        return task

    def chat_async(self, user_message, source: str = "admin") -> ChatTask:
        """Submit a chat as a background task. Returns immediately.

        source: "admin" for messages from portal UI, "agent:{agent_name}" for inter-agent,
                "system" for system messages

        If another chat task is already running for this agent, the new message
        is appended to a per-agent pending queue and will be executed
        sequentially after the current task (and any already-queued tasks)
        finish. We NEVER abort the running task just because a new message
        arrived — that would destroy in-flight work.

        ── LangGraph dispatch DISABLED (commented out 2026-04-26) ─────
        Decision: see docs/PHASE2B notes — LangGraph path was producing a
        cascade of bugs (orphan tool messages, empty tool_calls=[],
        recursion runaway, missing event stream, stale token context),
        each one requiring re-implementation of features V1 already has.
        V1 is the only chat path now. Code below + ``_chat_async_via_langgraph``
        + ``app/graph/`` are kept as-is so we can revive if we ever
        commit to migrating V1 → LangGraph properly. To re-enable:
        uncomment the block below + opt-in via ``TUDOU_USE_LANGGRAPH=1``.
        """
        # ── LangGraph dispatch — DISABLED ──
        # _use_graph = (
        #     os.environ.get("TUDOU_USE_LANGGRAPH", "0") == "1"
        #     or getattr(self.profile, "use_langgraph", False)
        # )
        # if _use_graph:
        #     try:
        #         return self._chat_async_via_langgraph(user_message, source=source)
        #     except Exception as _ge:
        #         logger.warning(
        #             "Agent %s: LangGraph path failed (%s) — falling back to V1",
        #             self.id[:8], _ge,
        #         )

        from .agent_types import AgentStatus as _AgentStatus
        mgr = get_chat_task_manager()
        # Detect any in-flight task for this agent
        active_states = (ChatTaskStatus.THINKING,
                         ChatTaskStatus.STREAMING,
                         ChatTaskStatus.TOOL_EXEC,
                         ChatTaskStatus.QUEUED,
                         ChatTaskStatus.WAITING_APPROVAL)
        # Invariant: while a chat loop is running, self.status != IDLE.
        # So if the agent is IDLE *and* some ChatTask still claims an
        # active state, that task is a ghost — a previous turn crashed,
        # the server restarted mid-turn, or a WebSocket dropped without
        # the terminal-state transition landing. Sweep the ghosts so
        # new messages don't queue behind them forever.
        agent_is_idle = (getattr(self, "status", None) == _AgentStatus.IDLE)
        has_active = False
        for existing_task in mgr.get_agent_tasks(self.id):
            if existing_task.status not in active_states:
                continue
            if agent_is_idle:
                try:
                    existing_task.error = (
                        "ghost task: agent is IDLE but this task claimed "
                        "an active state. Marked FAILED at new-message arrival.")
                    existing_task.set_status(
                        ChatTaskStatus.FAILED,
                        phase="stale (agent IDLE)",
                    )
                    logger.warning(
                        "chat_async: swept ghost task %s for agent %s "
                        "(was %s, agent is IDLE)",
                        existing_task.id, self.id[:8],
                        existing_task.status.value)
                except Exception as e:
                    logger.debug("ghost sweep failed for %s: %s",
                                 existing_task.id, e)
                continue
            has_active = True
            break
        task = mgr.create_task(self.id, user_message)
        task.set_status(ChatTaskStatus.QUEUED, "Queued", 0)

        # Ensure the per-agent pending-message queue exists
        if not hasattr(self, "_pending_chat_queue") or self._pending_chat_queue is None:
            self._pending_chat_queue = []
        if not hasattr(self, "_pending_chat_lock") or self._pending_chat_lock is None:
            self._pending_chat_lock = threading.Lock()

        if has_active:
            with self._pending_chat_lock:
                self._pending_chat_queue.append((task, user_message, source))
                queue_depth = len(self._pending_chat_queue)
            logger.info(
                "Agent %s busy — queued chat task %s (queue depth=%d)",
                self.id[:8], task.id, queue_depth)
            try:
                task.push_event({
                    "type": "queued",
                    "content": f"⏳ 排队中 ({queue_depth}) — 等上一轮对话结束",
                    "queue_position": queue_depth,
                })
            except Exception:
                pass
            return task

        def _run(task=task, user_message=user_message, source=source):
            # Capture turn start time for the "recent file" envelope fallback.
            # We use this to filter for files modified DURING this turn so
            # we don't surface old artifacts as fresh produce.
            _turn_started_at = time.time()
            try:
                # Show which provider/model is being used — use dynamic
                # routing so multimodal/auto_route overrides are reflected.
                _eff_prov, _eff_mdl = self._resolve_effective_provider_model(
                    user_message=user_message,
                )
                # Keep provider/model resolved for later log output, but
                # the user-facing progress bar only shows a clean label.
                # The raw "mlx-community/Qwen3.5-…-4bit" model name
                # clutters the UI and the user can already see it in
                # the chat-header dropdown.
                _mdl_name = _eff_mdl or self.model or "default"
                _prov_name = _eff_prov or self.provider or "default"
                try:
                    reg = llm.get_registry()
                    entry = reg.get(_eff_prov or self.provider)
                    if entry:
                        _prov_name = f"{entry.name} ({entry.kind})"
                except Exception:
                    pass
                task.set_status(ChatTaskStatus.THINKING, "发言中…", 10)
                task.push_event({"type": "thinking", "content": "发言中…"})

                _tool_count = [0]  # track tool iterations for progress

                def _on_event(evt: AgentEvent):
                    """Bridge agent events into ChatTask events."""
                    if evt.kind == "text_delta":
                        task.set_status(ChatTaskStatus.STREAMING,
                                        "Generating response...", 80)
                        task.push_event({"type": "text_delta",
                                         "content": evt.data.get("content", "")})
                    elif evt.kind == "message" and evt.data.get("role") == "assistant":
                        task.set_status(ChatTaskStatus.STREAMING,
                                        "Generating response...", 85)
                        task.push_event({"type": "text",
                                         "content": evt.data.get("content", "")})
                    elif evt.kind == "tool_call":
                        _tool_count[0] += 1
                        name = evt.data.get("name", "")
                        # Progress: 20% base + increments per tool (up to 70%)
                        prog = min(70, 20 + _tool_count[0] * 15)
                        task.set_status(ChatTaskStatus.TOOL_EXEC,
                                        f"{name}", prog)
                        task.push_event({
                            "type": "tool_call",
                            "name": name,
                            "args": json.dumps(
                                evt.data.get("arguments", {}),
                                ensure_ascii=False)[:200],
                        })
                    elif evt.kind == "tool_result":
                        prog = min(75, 25 + _tool_count[0] * 15)
                        task.set_status(ChatTaskStatus.THINKING,
                                        "Analyzing...", prog)
                        task.push_event({
                            "type": "tool_result",
                            "content": evt.data.get("result", "")[:500],
                        })
                        task.push_event({"type": "thinking",
                                         "content": "Thinking..."})
                    elif evt.kind == "approval":
                        status = evt.data.get("status", "")
                        if status == "pending":
                            task.set_status(ChatTaskStatus.WAITING_APPROVAL,
                                            "Waiting for approval...", -1)
                            task.push_event({
                                "type": "approval_request",
                                "tool": evt.data.get("tool", ""),
                                "reason": evt.data.get("reason", ""),
                                "arguments": evt.data.get("arguments", {}),
                                "agent_id": self.id,
                                "agent_name": self.name,
                                "approval_id": evt.data.get("approval_id", ""),
                            })
                        elif status in ("approved", "denied"):
                            task.push_event({
                                "type": "approval_" + status,
                                "tool": evt.data.get("tool", ""),
                            })
                    elif evt.kind == "login_request":
                        task.set_status(ChatTaskStatus.WAITING_APPROVAL,
                                        "Waiting for login credentials...", -1)
                        task.push_event({
                            "type": "login_request",
                            "request_id": evt.data.get("request_id", ""),
                            "url": evt.data.get("url", ""),
                            "site_name": evt.data.get("site_name", ""),
                            "login_url": evt.data.get("login_url", ""),
                            "reason": evt.data.get("reason", ""),
                            "agent_id": self.id,
                            "agent_name": self.name,
                        })
                    elif evt.kind == "plan_update":
                        task.push_event({
                            "type": "plan_update",
                            "plan": evt.data.get("plan"),
                        })
                    elif evt.kind == "error":
                        task.push_event({"type": "error",
                                         "content": evt.data.get("error", "")})
                    elif evt.kind == "retract_last_assistant":
                        # Tell UI to remove the just-rendered assistant bubble
                        # (text_delta already painted it before meta-promise
                        # detection kicked in).
                        task.push_event({
                            "type": "retract_last_assistant",
                            "reason": evt.data.get("reason", ""),
                        })

                result = self.chat(user_message, on_event=_on_event,
                                   abort_check=lambda: task.aborted, source=source)
                task.result = result
                if task.aborted:
                    # Already set to ABORTED by abort()
                    pass
                else:
                    # All answers now come from the LLM. Memory is only
                    # injected as background context — no more short-circuit.
                    # --- agent_state shadow (phase-2 envelope injection) ---
                    # Push any artifacts produced during this turn to the
                    # frontend BEFORE the "done" event so the FileCard
                    # widgets attach to the just-finished assistant bubble.
                    # Wrapped in try/except — never break the live path.
                    try:
                        refs: list = []
                        _shadow = getattr(self, "_shadow", None)
                        if _shadow is not None:
                            # Rescan workspace BEFORE building envelope.
                            # Picks up files produced by bash scripts (e.g.
                            # `python build_pptx.py` writes a .pptx that
                            # artifact extractor doesn't observe directly —
                            # without rescan the FileCard never appears).
                            try:
                                added = _shadow.rescan_deliverable_dir()
                                if added:
                                    logger.info(
                                        "Shadow rescan: ingested %d new "
                                        "artifact(s) from workspace", added)
                            except Exception as _scan_err:
                                logger.debug(
                                    "deliverable rescan skipped: %s", _scan_err)
                            refs = _shadow.build_envelope_refs() or []
                        # Fallback runs ALWAYS (whether or not shadow exists)
                        # when refs is empty: scan workspace mtimes to find
                        # files produced this turn.
                        if not refs:
                            try:
                                refs = _build_recent_file_refs(self, _turn_started_at)
                            except Exception as _rfb_err:
                                logger.debug(
                                    "recent-file fallback failed: %s", _rfb_err)
                                refs = []
                        # Diagnostic: see whether the fallback is actually
                        # finding files. Helps debug "task says PPTX created
                        # but no FileCard appears".
                        try:
                            logger.info(
                                "FILECARD agent=%s turn_started_at=%.0f "
                                "shadow=%s refs_count=%d",
                                self.id[:8], _turn_started_at,
                                "yes" if _shadow is not None else "no",
                                len(refs) if refs else 0,
                            )
                        except Exception:
                            pass
                        if refs:
                            task.push_event({
                                "type": "artifact_refs",
                                "refs": refs,
                            })
                    except Exception:
                        pass
                    task.set_status(ChatTaskStatus.COMPLETED, "Done", 100)
                    task.push_event({"type": "done", "source": "llm"})
            except Exception as e:
                if task.aborted:
                    pass  # Abort may cause exceptions, ignore
                else:
                    task.error = str(e)
                    task.set_status(ChatTaskStatus.FAILED, f"Error: {e}", -1)
                    task.push_event({"type": "error", "content": str(e)})
                    task.push_event({"type": "done"})
            finally:
                # Persist chat history so messages survive a restart.
                # We append to self.messages during chat() but nothing upstream
                # of that call flushes to disk, so we do it here at the end of
                # every chat task (success, failure, or abort).
                try:
                    from .hub import get_hub as _get_hub
                    _hub = _get_hub()
                    if _hub is not None:
                        try:
                            _hub._save_agent_workspace(self)
                        except Exception:
                            pass
                        # Also bump the aggregate JSON/SQLite dump so the
                        # sidebar-state load path sees fresh messages.
                        try:
                            _hub._save_agents()
                        except Exception:
                            pass
                except Exception as _persist_err:
                    logger.debug("post-chat persist failed: %s", _persist_err)

                # Drain pending chat queue: if the user typed more messages
                # while we were busy, merge them into ONE follow-up turn
                # (soft-queue + merge — 等效于 Claude Code 的"下一轮一起想"语义)。
                # 环境变量 TUDOU_MERGE_PENDING=0 可退回到逐条运行。
                try:
                    drained = []
                    lock = getattr(self, "_pending_chat_lock", None)
                    if lock is not None:
                        with lock:
                            q = getattr(self, "_pending_chat_queue", None) or []
                            if q:
                                drained = list(q)
                                q.clear()

                    if drained:
                        import os as _os_pm
                        _merge_enabled = _os_pm.environ.get(
                            "TUDOU_MERGE_PENDING", "1"
                        ).strip().lower() not in ("0", "false", "no")
                        # 任一条是多模态 list/dict —— 不合并，避免破坏结构
                        _has_multimodal = any(
                            isinstance(m, (list, dict))
                            for _t, m, _s in drained
                        )

                        _runner = _run  # closure capture

                        if (len(drained) == 1 or _has_multimodal
                                or not _merge_enabled):
                            # ── 单条 / 多模态 / 禁用合并：串行运行 ──
                            first_task, first_msg, first_src = drained[0]
                            rest = drained[1:]
                            if rest and lock is not None:
                                with lock:
                                    # 把剩余的放回队首，保留到达顺序
                                    self._pending_chat_queue[:0] = rest
                                    for _i, (_t, _, _) in enumerate(
                                            self._pending_chat_queue):
                                        try:
                                            _t.push_event({
                                                "type": "queued",
                                                "content": f"⏳ 排队中 ({_i+1})",
                                                "queue_position": _i + 1,
                                            })
                                        except Exception:
                                            pass
                            logger.info(
                                "Agent %s draining pending chat task %s",
                                self.id[:8], first_task.id)
                            threading.Thread(
                                target=lambda: _runner(
                                    first_task, first_msg, first_src),
                                daemon=True,
                            ).start()
                        else:
                            # ── N ≥ 2 纯文本消息：合并为一个追加轮 ──
                            primary_task, _first_msg, primary_src = drained[0]
                            _parts = [
                                "（以下内容在你上一轮回复过程中陆续到达，"
                                "请结合刚才的输出一起考虑；"
                                "如需修正或补充请明确说明。）"
                            ]
                            for _idx, (_t, _m, _s) in enumerate(
                                    drained, start=1):
                                _parts.append(
                                    f"【追加 {_idx}】{str(_m or '').strip()}")
                            merged_text = "\n\n".join(_parts)

                            # 把被合并的 task 标记为已完成，避免 UI 永远卡在排队
                            for merged_task, _m, _s in drained[1:]:
                                try:
                                    merged_task.push_event({
                                        "type": "text",
                                        "content": ("（与同时到达的其他消息"
                                                    "合并处理，统一回复见关联"
                                                    f"任务 {primary_task.id[:8]}）"),
                                    })
                                    merged_task.set_status(
                                        ChatTaskStatus.COMPLETED,
                                        "已合并", 100)
                                    merged_task.push_event({
                                        "type": "done",
                                        "source": "merged",
                                        "merged_into": primary_task.id,
                                    })
                                except Exception:
                                    pass

                            logger.info(
                                "Agent %s merging %d pending msgs into "
                                "task %s",
                                self.id[:8], len(drained), primary_task.id)

                            # 更新 primary_task 显示文本为合并后内容
                            try:
                                primary_task.user_message = merged_text[:500]
                            except Exception:
                                pass

                            threading.Thread(
                                target=lambda: _runner(
                                    primary_task, merged_text, primary_src),
                                daemon=True,
                            ).start()
                except Exception as _drain_err:
                    logger.debug("pending chat drain failed: %s", _drain_err)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return task

    # ---- Enhancement module management ----

    def enable_enhancement(self, domain) -> dict:
        """Enable an enhancement domain for this agent.

        `domain` may be a single string (legacy) or a list of up to 8
        preset ids to merge into a composite enhancer.
        """
        if isinstance(domain, (list, tuple)):
            doms = [str(d).strip() for d in domain if str(d).strip()][:8]
            if not doms:
                self.enhancer = build_enhancer("custom")
            elif len(doms) == 1:
                self.enhancer = build_enhancer(doms[0])
            else:
                self.enhancer = build_multi_enhancer(doms)
            label = "+".join(doms) if doms else "custom"
        else:
            self.enhancer = build_enhancer(str(domain))
            label = str(domain)
        # Rebuild system prompt to include new knowledge
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = self._build_system_prompt()
        logger.info("Enhancement enabled for agent %s: domain=%s", self.id, label)
        return self.enhancer.get_stats()

    def disable_enhancement(self):
        """Disable the enhancement module."""
        self.enhancer = None
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = self._build_system_prompt()
        logger.info("Enhancement disabled for agent %s", self.id)

    def get_enhancement_info(self) -> dict | None:
        """Get detailed enhancement module info."""
        if not self.enhancer:
            return None
        return {
            **self.enhancer.get_stats(),
            "knowledge_entries": [e.to_dict() for e in self.enhancer.knowledge.entries.values()],
            "reasoning_patterns": [p.to_dict() for p in self.enhancer.reasoning.patterns.values()],
            "memory_nodes": [n.to_dict() for n in self.enhancer.memory.nodes.values()],
            "tool_chains": [tc.to_dict() for tc in self.enhancer.tool_chains.values()],
        }

    # ---- Tool budget extension (long-task user authorization) ----

    def _request_budget_extension(self, total_so_far: int,
                                   bump: int, extensions_used: int) -> str:
        """Synchronously request user approval to continue tool calls
        past the per-turn budget threshold. Reuses the existing
        ``PendingApproval`` primitive so the request surfaces in the
        portal's approval feed alongside risky-tool approvals.

        Returns ``"approved"`` or ``"denied"`` (timeout treated as
        denied — same as risky-tool approval).

        Logs:
          * approval event (pending → approved/denied) so the portal
            can render a card with an "Approve / Deny" button
          * audit entry for the operator's record
          * system message into self.messages so the LLM's next turn
            knows the outcome
        """
        try:
            from .auth import get_auth
            from .agent_types import AgentStatus, AgentEvent
        except Exception as e:
            logger.warning("budget extension: import failed: %s", e)
            return "denied"
        auth = get_auth()
        policy = getattr(auth, "tool_policy", None)
        if policy is None:
            return "denied"

        prior_status = self.status
        self.status = AgentStatus.WAITING_APPROVAL

        reason = (
            f"Agent 已调用 {total_so_far} 次工具，请求继续 +{bump} 次预算 "
            f"(已使用 {extensions_used} 次扩展)"
        )
        approval = policy.request_approval(
            tool_name="__budget_extension__",
            arguments={
                "calls_so_far": total_so_far,
                "extensions_used": extensions_used,
                "bump": bump,
            },
            agent_id=self.id,
            agent_name=self.name,
            reason=reason,
        )
        # Emit approval event so the portal sidebar surfaces a pending
        # card. The frontend's existing approvals UI handles rendering;
        # __budget_extension__ has no special wiring — it shows up in
        # the standard list with the reason text.
        evt = AgentEvent(time.time(), "approval", {
            "tool": "__budget_extension__",
            "status": "pending",
            "reason": reason,
            "agent_name": self.name,
            "approval_id": approval.approval_id,
            "calls_so_far": total_so_far,
        })
        self._log(evt.kind, evt.data)
        try:
            auth.audit("budget_extension_requested",
                       actor=self.name, target=self.id,
                       detail=reason)
        except Exception:
            pass

        decision = policy.wait_for_approval(approval)
        self.status = prior_status

        if decision == "approved":
            try:
                self.messages.append({
                    "role": "system",
                    "content": (
                        f"[预算授权] 用户已授权继续。"
                        f"当前 {total_so_far} 次 + 新增 {bump} 次预算。"
                        f"请尽快收尾。"
                    ),
                })
            except Exception:
                pass
            self._log("approval", {
                "tool": "__budget_extension__",
                "status": "approved",
                "approval_id": approval.approval_id,
            })
            try:
                auth.audit("budget_extension_approved",
                           actor=approval.decided_by or "?",
                           target=self.id, detail=reason)
            except Exception:
                pass
            return "approved"
        else:
            try:
                self.messages.append({
                    "role": "system",
                    "content": (
                        "[预算被拒] 用户拒绝继续工具调用。"
                        "请基于已有内容给最终回复，不要再调任何 tool。"
                    ),
                })
            except Exception:
                pass
            self._log("approval", {
                "tool": "__budget_extension__",
                "status": "denied",
                "approval_id": approval.approval_id,
            })
            try:
                auth.audit("budget_extension_denied",
                           actor=approval.decided_by or "timeout",
                           target=self.id, detail=reason, success=False)
            except Exception:
                pass
            return "denied"

    # ---- Think button: on-demand self-summary ----

    def think_now(self, turns_window: int = 15) -> dict:
        """Summarize the last N turns of this agent's conversation.

        The old ``Think`` button opened a panel and toggled a scheduler
        loop that almost nobody enabled, and whose output went to a
        file nobody read. This replacement is a single on-demand call:

          1. Collect the last ``turns_window`` user+assistant messages.
          2. Ask the agent's own LLM to produce a short recap + extract
             any reusable rules worth persisting to the experience lib.
          3. Append the summary as an assistant-kind event so the
             portal renders it as a chat bubble prefixed with 【自我总结】.
          4. If the LLM emits structured ``<experience>`` blocks, write
             them through ``save_experience`` — that's the one real
             self-improvement channel that already works.

        Returns ``{"ok": True, "summary": str, "experiences_saved": int}``
        or ``{"ok": False, "error": str}``.
        """
        import json as _json
        try:
            msgs = list(self.messages or [])
        except Exception:
            msgs = []
        # Skip system messages; pick recent user/assistant pairs.
        convo = [m for m in msgs
                 if isinstance(m, dict)
                 and m.get("role") in ("user", "assistant")
                 and str(m.get("content") or "").strip()]
        if not convo:
            return {"ok": False, "error": "no_conversation_yet"}
        window = convo[-int(max(1, turns_window)):]

        # Compact transcript — bound each turn's content.
        def _fmt(m):
            role = "用户" if m.get("role") == "user" else "我"
            content = str(m.get("content") or "")
            if len(content) > 800:
                content = content[:800] + "…(截断)"
            return f"[{role}]: {content}"

        transcript = "\n\n".join(_fmt(m) for m in window)

        # Build the self-summary prompt.
        prompt = (
            "请对最近的对话做一次【自我总结】。\n\n"
            "要求：\n"
            "1. 用中文，简短 3-5 段，每段 1-2 句。\n"
            "2. 按以下结构输出：\n"
            "   - 我刚才在做什么：一句话概括话题。\n"
            "   - 遇到的关键问题或卡点（如果有）。\n"
            "   - 有用的规则/教训：具体可复用的做法。写不出来就说『暂无』，不要编。\n"
            "   - 下一步建议（如果有）。\n"
            "3. 如果在【有用的规则/教训】里提炼出了具体可复用的行动规则，"
            "并且你判断值得沉淀进经验库，请在结尾追加 1 个或多个 JSON 块，"
            "用 ```experience ... ``` 包围，格式：\n"
            '   {"scene": "…场景描述…", "knowledge": "…核心知识…", '
            '"rule_do": "…应该做…", "rule_dont": "…不应该做…", '
            '"priority": 3}\n'
            "   没有值得沉淀的就不要加 JSON 块。禁止编造。\n\n"
            "--- 最近对话（旧→新）---\n"
            f"{transcript}\n"
            "--- 对话结束 ---"
        )

        # Run through the agent's own LLM (non-streaming).
        try:
            from . import llm as _llm
            _prov, _mdl = self._resolve_effective_provider_model()
            resp = _llm.chat_no_stream(
                messages=[
                    {"role": "system",
                     "content": "你是一个严谨的自我复盘助手。输出必须忠实于对话内容，不编造。"},
                    {"role": "user", "content": prompt},
                ],
                model=_mdl, provider=_prov,
            )
            # chat_no_stream returns {"message": {"content": ..., "reasoning_content": ...}, ...}
            # 老代码 resp.get("content") 读的是顶层 → 永远空 → empty_llm_output。
            # 正确读取: message.content;若为空再回退到 reasoning_content
            # (DeepSeek thinking-mode 有时只在 reasoning 里出文字)。
            if isinstance(resp, dict):
                _m = resp.get("message") or {}
                raw = str(_m.get("content") or "").strip()
                if not raw:
                    raw = str(_m.get("reasoning_content") or "").strip()
                # 极端兜底：部分旧 wrapper 可能直接把 content 放顶层
                if not raw:
                    raw = str(resp.get("content") or "").strip()
            else:
                raw = str(resp or "").strip()
        except Exception as e:
            logger.error("think_now LLM call failed: %s", e, exc_info=True)
            return {"ok": False, "error": f"llm_failed: {e}"}

        raw = (raw or "").strip()
        if not raw:
            # Dump provider/model + response shape to help diagnose future cases.
            try:
                _shape = list((resp or {}).keys()) if isinstance(resp, dict) else type(resp).__name__
                _msg_shape = list(((resp or {}).get("message") or {}).keys()) if isinstance(resp, dict) else []
                logger.warning(
                    "think_now empty_llm_output agent=%s provider=%s model=%s "
                    "resp_keys=%s message_keys=%s",
                    self.id[:8], _prov, _mdl, _shape, _msg_shape)
            except Exception:
                pass
            return {"ok": False, "error": "empty_llm_output"}

        # Extract optional ```experience ...``` JSON blocks. Strip them
        # from the displayed summary so the chat bubble stays clean.
        import re as _re
        exp_pattern = _re.compile(
            r"```experience\s*(.+?)```", _re.DOTALL | _re.IGNORECASE)
        raw_exps = exp_pattern.findall(raw)
        display_summary = exp_pattern.sub("", raw).strip()

        # Attempt to persist each experience via the library.
        saved_count = 0
        if raw_exps:
            try:
                from .experience_library import SelfImprovementEngine
                if not self.self_improvement:
                    self.self_improvement = SelfImprovementEngine(
                        agent=self, role=self.role)
                lib = self.self_improvement.library
                for block in raw_exps:
                    block = block.strip()
                    try:
                        parsed = _json.loads(block)
                    except Exception:
                        continue
                    if not isinstance(parsed, dict):
                        continue
                    if not str(parsed.get("scene") or "").strip():
                        continue
                    try:
                        from .experience_library import Experience
                        # Priority: accept numeric (1-3) or string (high/medium/low)
                        raw_pri = parsed.get("priority")
                        if isinstance(raw_pri, (int, float)):
                            pri_str = {3: "high", 2: "medium",
                                       1: "low"}.get(int(raw_pri), "medium")
                        else:
                            pri_str = str(raw_pri or "medium").lower().strip()
                            if pri_str not in ("high", "medium", "low"):
                                pri_str = "medium"
                        # Experience fields: scene / core_knowledge /
                        # action_rules[] / taboo_rules[]. Map from the
                        # simpler LLM-emitted JSON shape.
                        rule_do = str(parsed.get("rule_do") or "").strip()
                        rule_dont = str(parsed.get("rule_dont") or "").strip()
                        action_rules = [rule_do] if rule_do else []
                        taboo_rules = [rule_dont] if rule_dont else []
                        exp = Experience(
                            role=self.role,
                            exp_type="retrospective",
                            source=f"think_now:agent={self.id}",
                            scene=str(parsed.get("scene") or "").strip(),
                            core_knowledge=str(
                                parsed.get("knowledge") or "").strip(),
                            action_rules=action_rules,
                            taboo_rules=taboo_rules,
                            priority=pri_str,
                        )
                        lib.add_experience(self.role, exp)
                        saved_count += 1
                    except Exception as e:
                        logger.debug("think_now: skipping malformed exp: %s", e)
            except Exception as e:
                logger.debug("think_now: experience persistence failed: %s", e)

        # Surface the summary as an assistant-kind chat event.
        display_text = f"【自我总结】\n\n{display_summary}"
        if saved_count:
            display_text += (
                f"\n\n— 已沉淀 **{saved_count}** 条经验到你的经验库。"
            )
        try:
            self._log("message", {
                "role": "assistant",
                "content": display_text,
                "source": "think_now",
            })
            # Also append to in-memory messages so subsequent LLM calls
            # include the summary as context (aligns with existing
            # self-improvement injection path).
            self.messages.append({
                "role": "assistant",
                "content": display_text,
            })
        except Exception as e:
            logger.debug("think_now: failed to append summary event: %s", e)

        return {
            "ok": True,
            "summary": display_summary,
            "experiences_saved": saved_count,
            "turns_analyzed": len(window),
        }

    # ---- Self-Improvement (Experience Library) ----

    def enable_self_improvement(self, auto_retro: bool = True,
                                 auto_learn_interval: int = 0,
                                 import_experience: bool = True,
                                 import_limit: int = 50) -> dict:
        """Enable self-improvement for this agent."""
        from .experience_library import SelfImprovementEngine
        if not self.self_improvement:
            self.self_improvement = SelfImprovementEngine(
                agent=self, role=self.role)
        self.self_improvement.enable(
            auto_retro=auto_retro,
            auto_learn_interval=auto_learn_interval,
            import_experience=import_experience,
            import_limit=import_limit,
        )
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = self._build_system_prompt()
        logger.info("Self-improvement enabled for agent %s", self.id)
        return self.self_improvement.get_stats()

    def disable_self_improvement(self):
        """Disable self-improvement."""
        if self.self_improvement:
            self.self_improvement.disable()
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = self._build_system_prompt()
        logger.info("Self-improvement disabled for agent %s", self.id)

    def trigger_retrospective(self, task_summary: str = "",
                               context: str = "") -> dict:
        """Trigger a retrospective analysis."""
        from .experience_library import SelfImprovementEngine
        if not self.self_improvement:
            # Create the engine so this one-shot call can run, but DO
            # NOT flip the background opt-in switch. tick_growth gates
            # on ``self_improvement.enabled``; we don't want a single
            # on-demand retrospective to permanently enable background
            # growth ticks that keep spawning tasks the user never
            # asked for.
            self.self_improvement = SelfImprovementEngine(
                agent=self, role=self.role)

        # Build prompt and run through LLM
        prompt = self.self_improvement.build_retrospective_prompt(
            task_summary=task_summary, context=context)

        # Use agent's own LLM to perform retrospective
        try:
            from . import llm
            _prov, _mdl = self._resolve_effective_provider_model()
            resp = llm.chat_no_stream(
                messages=[
                    {"role": "system", "content": "你是一个经验复盘助手。请严格按JSON格式输出。"},
                    {"role": "user", "content": prompt},
                ],
                model=_mdl, provider=_prov,
            )
            raw = resp.get("content", "") if isinstance(resp, dict) else str(resp)
            result = self.self_improvement.process_retrospective_output(
                raw, task_summary=task_summary)
            return result.to_dict()
        except Exception as e:
            logger.error(f"Retrospective failed for agent {self.id}: {e}")
            return {"error": str(e)}

    def trigger_active_learning(self, learning_goal: str = "",
                                 knowledge_gap: str = "") -> dict:
        """Trigger active learning. Lower priority than tasks/projects.

        Rejects empty / placeholder goals — these produce noise in the
        learning plan board and never converge to real experiences. Callers
        (growth tick, portal button, etc.) MUST pass a concrete goal.
        """
        goal = (learning_goal or "").strip()
        if not goal or goal in ("(未设定)", "未设定", "未设定目标"):
            return {
                "status": "rejected",
                "error": "learning_goal required: provide a specific study objective",
                "learning_goal": "",
            }

        from .experience_library import SelfImprovementEngine
        if not self.self_improvement:
            self.self_improvement = SelfImprovementEngine(
                agent=self, role=self.role)
            self.self_improvement.enable()

        # Priority check: if agent has pending tasks/projects, queue instead of executing
        if self.self_improvement.should_pause_for_tasks():
            try:
                queued = self.self_improvement.queue_learning(goal, knowledge_gap)
            except ValueError as ve:
                return {"status": "rejected", "error": str(ve), "learning_goal": goal}
            return {
                "status": "queued",
                "message": f"Agent 有未完成的任务/项目，学习计划已排队。任务完成后将自动执行。",
                "queued_task": queued,
                "learning_goal": goal,
            }

        prompt = self.self_improvement.build_learning_prompt(
            learning_goal=learning_goal, knowledge_gap=knowledge_gap)

        try:
            from . import llm
            _prov, _mdl = self._resolve_effective_provider_model()
            resp = llm.chat_no_stream(
                messages=[
                    {"role": "system", "content": "你是一个主动学习助手。请严格按JSON格式输出。"},
                    {"role": "user", "content": prompt},
                ],
                model=_mdl, provider=_prov,
            )
            raw = resp.get("content", "") if isinstance(resp, dict) else str(resp)
            result = self.self_improvement.process_learning_output(raw)
            return result.to_dict()
        except Exception as e:
            logger.error(f"Active learning failed for agent {self.id}: {e}")
            return {"error": str(e)}

    # ---- Execution Plan: tool handler ----

    def _handle_handoff_request(self, arguments: dict, on_event=None) -> str:
        """Handle handoff_request: synchronous task transfer to a peer agent with
        a visible 3-state handshake (sent → acked → completed/failed).

        Unlike send_message (fire-and-forget), this blocks until the receiver
        produces a result or fails, and emits AgentEvent("handoff_*") at each
        state transition so the UI can render a badge.
        """
        import threading as _threading
        import uuid as _uuid

        to_agent_ref = (arguments.get("to_agent") or "").strip()
        task_text = (arguments.get("task") or "").strip()
        expected_output = (arguments.get("expected_output") or "").strip()
        context_text = (arguments.get("context") or "").strip()
        try:
            timeout_seconds = int(arguments.get("timeout_seconds") or 600)
        except (TypeError, ValueError):
            timeout_seconds = 600
        # Clamp to a sane range — don't let an LLM set timeout=0 or days
        timeout_seconds = max(10, min(timeout_seconds, 3600))

        if not to_agent_ref or not task_text:
            return json.dumps({
                "ok": False,
                "error": "handoff_request requires both 'to_agent' and 'task'.",
            }, ensure_ascii=False)

        # Resolve target agent (by ID or name, mirroring _tool_send_message)
        try:
            from .hub import get_hub as _get_hub
            hub = _get_hub()
        except Exception as e:
            return json.dumps({
                "ok": False, "error": f"hub unavailable: {e}",
            }, ensure_ascii=False)

        target = hub.get_agent(to_agent_ref)
        if target is None:
            for a in hub.agents.values():
                if a.name.lower() == to_agent_ref.lower():
                    target = a
                    break
        if target is None:
            available = [f"{a.name} ({a.id})" for a in hub.agents.values()]
            return json.dumps({
                "ok": False,
                "error": (
                    f"Agent '{to_agent_ref}' not found. "
                    f"Available: {', '.join(available) or 'none'}"
                ),
            }, ensure_ascii=False)

        if target.id == self.id:
            return json.dumps({
                "ok": False,
                "error": (
                    "Cannot hand off to yourself. If you are reporting status, "
                    "just write it in your response — do NOT call handoff_request."
                ),
            }, ensure_ascii=False)

        handoff_id = _uuid.uuid4().hex[:10]
        from_name = f"{self.role}-{self.name}" if self.name else self.id
        to_name = f"{target.role}-{target.name}" if target.name else target.id

        # ── Emit "sent" event — UI shows ⏳ pending ──
        if on_event:
            on_event(AgentEvent(time.time(), "handoff_sent", {
                "handoff_id": handoff_id,
                "from_agent_id": self.id,
                "from_agent_name": from_name,
                "to_agent_id": target.id,
                "to_agent_name": to_name,
                "task": task_text[:300],
                "expected_output": expected_output[:200],
            }))
        self._log("handoff_sent", {
            "handoff_id": handoff_id,
            "to_agent_id": target.id,
            "to_agent_name": to_name,
            "task_preview": task_text[:200],
        })

        # Audit: cross-agent structured handoff
        try:
            from .auth import get_auth as _get_auth
            _auth = _get_auth()
            if _auth is not None:
                _auth.audit(
                    action="agent_handoff",
                    actor=self.id or "system",
                    target=target.id,
                    detail=f"[handoff:{handoff_id}] {task_text[:300]}",
                )
        except Exception:
            pass

        # Build the prompt B will see
        prompt_parts = [f"## Delegated Task\n{task_text}"]
        if expected_output:
            prompt_parts.append(f"## Expected Output\n{expected_output}")
        if context_text:
            prompt_parts.append(f"## Context\n{context_text}")
        prompt_parts.append(
            f"_(Handoff {handoff_id} from {from_name}. "
            f"Return a complete answer — your reply is the handoff result.)_"
        )
        prompt = "\n\n".join(prompt_parts)

        # ── Emit "acked" event right before B starts working.
        #    The ack is SYSTEM-GENERATED — not B's free-form reply.
        #    UI flips the badge ⏳ → ✅ the moment we hand control to B.
        if on_event:
            on_event(AgentEvent(time.time(), "handoff_acked", {
                "handoff_id": handoff_id,
                "to_agent_id": target.id,
                "to_agent_name": to_name,
            }))
        self._log("handoff_acked", {
            "handoff_id": handoff_id, "to_agent_id": target.id,
        })

        # ── Execute on B with a timeout guard ──
        result_box: dict = {"result": "", "error": ""}

        def _run():
            try:
                result_box["result"] = target.chat(prompt) or ""
            except Exception as exc:
                result_box["error"] = f"{type(exc).__name__}: {exc}"

        t = _threading.Thread(target=_run, daemon=True,
                              name=f"handoff-{handoff_id}")
        t.start()
        t.join(timeout=timeout_seconds)

        if t.is_alive():
            # Timed out — thread keeps running but we stop waiting
            if on_event:
                on_event(AgentEvent(time.time(), "handoff_failed", {
                    "handoff_id": handoff_id,
                    "to_agent_name": to_name,
                    "error": f"Timed out after {timeout_seconds}s",
                }))
            self._log("handoff_failed", {
                "handoff_id": handoff_id, "error": "timeout",
            })
            return json.dumps({
                "ok": False,
                "handoff_id": handoff_id,
                "state": "timeout",
                "to_agent": to_name,
                "error": (
                    f"Handoff to {to_name} timed out after {timeout_seconds}s. "
                    "The receiver may still be working; check back later."
                ),
            }, ensure_ascii=False)

        if result_box["error"]:
            if on_event:
                on_event(AgentEvent(time.time(), "handoff_failed", {
                    "handoff_id": handoff_id,
                    "to_agent_name": to_name,
                    "error": result_box["error"],
                }))
            self._log("handoff_failed", {
                "handoff_id": handoff_id, "error": result_box["error"],
            })
            return json.dumps({
                "ok": False,
                "handoff_id": handoff_id,
                "state": "failed",
                "to_agent": to_name,
                "error": result_box["error"],
            }, ensure_ascii=False)

        result_text = result_box["result"]
        if on_event:
            on_event(AgentEvent(time.time(), "handoff_completed", {
                "handoff_id": handoff_id,
                "to_agent_id": target.id,
                "to_agent_name": to_name,
                "result_preview": result_text[:300],
            }))
        self._log("handoff_completed", {
            "handoff_id": handoff_id,
            "to_agent_id": target.id,
            "result_preview": result_text[:200],
        })

        return json.dumps({
            "ok": True,
            "handoff_id": handoff_id,
            "state": "completed",
            "to_agent": to_name,
            "result": result_text,
        }, ensure_ascii=False)

    def _handle_web_login_request(self, arguments: dict, on_event=None) -> str:
        """Handle request_web_login: pause agent, show login form, wait for credentials."""
        from .auth import create_login_request, wait_for_login
        url = arguments.get("url", "")
        site_name = arguments.get("site_name", "")
        reason = arguments.get("reason", "")
        login_url = arguments.get("login_url", "")
        if not url:
            return json.dumps({"error": "url is required"})

        # ── Session-level dedup: if this domain was already attempted
        #    (succeeded or failed/timed out), don't block again. ──
        guard = self._get_login_guard()
        if guard.already_attempted(url):
            if guard.was_authenticated(url):
                return json.dumps({
                    "ok": True, "login_method": "cached",
                    "note": f"Already authenticated for {site_name or url}. Proceed with your task.",
                })
            else:
                return json.dumps({
                    "error": (
                        f"Login for {site_name or url} was already attempted but the user "
                        "did not provide credentials. Do NOT retry — skip this login-required "
                        "task, inform the user, and move on to other work."
                    ),
                })

        req = create_login_request(
            agent_id=self.id, agent_name=self.name,
            url=url, site_name=site_name, reason=reason, login_url=login_url,
        )

        # Emit SSE event so chat UI shows login form
        prev_status = self.status
        self.status = AgentStatus.WAITING_APPROVAL
        evt = AgentEvent(time.time(), "login_request", {
            "request_id": req.request_id,
            "url": url,
            "site_name": site_name,
            "login_url": login_url or url,
            "reason": reason,
            "agent_id": self.id,
            "agent_name": self.name,
        })
        self._log(evt.kind, evt.data)
        if on_event:
            on_event(evt)

        # Block until user submits credentials or timeout
        credentials = wait_for_login(req, timeout=300)
        self.status = prev_status if prev_status != AgentStatus.WAITING_APPROVAL else AgentStatus.BUSY

        if not credentials:
            guard.record_attempt(url, False)
            return json.dumps({
                "error": (
                    "Login request expired — no credentials provided within 5 minutes. "
                    "Do NOT retry this login. Skip this task and move on to other work."
                ),
            })

        # ── __BROWSER_SESSION__ signal: user logged in via iframe/new tab,
        #    agent should capture cookies from its own browser instance. ──
        guard.record_attempt(url, True)
        if credentials.get("cookies") == "__BROWSER_SESSION__":
            return json.dumps({
                "ok": True,
                "site_name": site_name,
                "url": url,
                "login_method": "browser_session",
                "note": (
                    "User has completed login in their browser. "
                    "The browser session is now authenticated. "
                    "Use browser_get_cookies or continue browsing the target URL directly — "
                    "the session cookies are already active in the browser context."
                ),
            }, ensure_ascii=False)

        # ── Sensitive-data masking: store real values in runtime vault,
        #    return placeholder variables to LLM context so passwords/tokens
        #    never appear in conversation history or logs. ──
        vault_prefix = f"CRED_{req.request_id[:8]}"
        result = {"ok": True, "site_name": site_name, "url": url, "login_method": "credentials"}
        _cred_fields = [("username", False), ("password", True),
                        ("cookies", True), ("token", True)]
        for field_name, is_secret in _cred_fields:
            val = credentials.get(field_name, "")
            if not val:
                continue
            if is_secret:
                # Store in vault, give LLM a placeholder
                key = f"{vault_prefix}_{field_name.upper()}"
                self._credential_vault[key] = val
                result[field_name] = "{{" + key + "}}"
            else:
                # Non-secret (username) can go to LLM directly
                result[field_name] = val
        result["note"] = (
            "Credentials received. Secret fields are masked as {{CRED_xxx}} placeholders. "
            "Pass them as-is when calling tools — they will be auto-substituted at execution time. "
            "Do NOT attempt to decode or log the placeholders."
        )
        return json.dumps(result, ensure_ascii=False)

    def _substitute_credentials(self, arguments: dict) -> dict:
        """Replace {{CRED_xxx}} placeholders in tool arguments with real values
        from the runtime credential vault. This ensures sensitive data never
        appears in LLM conversation history but is available at execution time."""
        if not self._credential_vault:
            return arguments
        import re
        _CRED_RE = re.compile(r"\{\{(CRED_[A-Za-z0-9_]+)\}\}")
        substituted = {}
        for k, v in arguments.items():
            if isinstance(v, str) and "{{CRED_" in v:
                def _repl(m):
                    return self._credential_vault.get(m.group(1), m.group(0))
                substituted[k] = _CRED_RE.sub(_repl, v)
            else:
                substituted[k] = v
        return substituted

    def _handle_plan_update(self, arguments: dict) -> str:
        """Handle the plan_update tool call internally."""
        # 防御：arguments 可能是 str（LLM 返回未解析的 JSON）
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (json.JSONDecodeError, TypeError):
                return json.dumps({"error": "Invalid arguments format"})
        action = arguments.get("action", "")
        if action == "create_plan":
            # steps 可能是 JSON 字符串而非 list
            # Some models (Qwen) use "plan" instead of "steps"
            steps_raw = arguments.get("steps") or arguments.get("plan") or []
            if isinstance(steps_raw, str):
                try:
                    steps_raw = json.loads(steps_raw)
                except (json.JSONDecodeError, TypeError):
                    steps_raw = []
            if not isinstance(steps_raw, list):
                steps_raw = []
            # 确保每个 step 是 dict
            steps_clean = []
            missing_acceptance: list[str] = []
            for s in steps_raw:
                if isinstance(s, str):
                    try:
                        s = json.loads(s)
                    except (json.JSONDecodeError, TypeError):
                        s = {"title": s}
                if isinstance(s, dict):
                    # P1/L2 — track steps that forgot the acceptance
                    # criterion so we can nudge the LLM (not hard-fail —
                    # we create the plan either way, but the LLM sees
                    # a warning and can patch via add_step/replan).
                    _acc = str(s.get("acceptance") or "").strip()
                    if not _acc:
                        missing_acceptance.append(str(s.get("title", ""))[:40])
                    steps_clean.append(s)
            plan = self.create_execution_plan(
                task_summary=arguments.get("task_summary", ""),
                steps=steps_clean,
            )
            step_ids = [{"id": s.id, "title": s.title,
                         "acceptance": s.acceptance} for s in plan.steps]
            payload: dict = {"ok": True, "plan_id": plan.id, "steps": step_ids}
            if missing_acceptance:
                payload["warning"] = (
                    f"{len(missing_acceptance)} step(s) have no `acceptance` "
                    f"criterion: {missing_acceptance}. Vague steps are the #1 "
                    f"source of fake completions. Call plan_update(action='replan', "
                    f"steps=[...]) to patch them, or explicitly accept the risk."
                )
            return json.dumps(payload, ensure_ascii=False)
        elif action in ("start_step", "complete_step", "fail_step"):
            # Map action → status (needed to drive update_plan_step and
            # to detect "already in that state" so repeated calls don't
            # loop on their own idempotency).
            _status_for = {
                "start_step": "in_progress",
                "complete_step": "completed",
                "fail_step": "failed",
            }
            status = _status_for[action]
            step_id = arguments.get("step_id", "")
            summary = str(arguments.get("result_summary", "") or "").strip()

            # Informative-error path: when the caller's step_id doesn't
            # match, we previously returned bare {ok:false, step:null}.
            # The LLM saw this and had nothing to correct with, so it
            # retried the same call forever (observed: 10+ identical
            # plan_update complete_step calls at 15-20s intervals).
            # Now we diagnose and tell the LLM what's wrong.
            plan = self._current_plan
            if plan is None:
                return json.dumps({
                    "ok": False,
                    "error": (
                        "No active plan. Call "
                        "plan_update(action='create_plan', steps=[...]) "
                        "first before start_step / complete_step / fail_step. "
                        "If you don't need a formal plan for this task, "
                        "skip plan_update entirely and just do the work."
                    ),
                }, ensure_ascii=False)

            # P1/L2 — complete_step / fail_step require a non-trivial
            # result_summary. Empty / too-short summaries are how agents
            # fake completion ("done", "完成"). We reject and require the
            # LLM to re-call with a proper summary that references the
            # step's acceptance criterion.
            if action in ("complete_step", "fail_step"):
                matched_for_summary = next(
                    (s for s in plan.steps if s.id == step_id), None,
                )
                if len(summary) < 8:  # "completed" / "done" / "OK." all < 8
                    acc_hint = ""
                    if matched_for_summary and matched_for_summary.acceptance:
                        acc_hint = (f" The step's acceptance was: "
                                    f"{matched_for_summary.acceptance!r}. "
                                    f"Your result_summary should cite specific "
                                    f"evidence that this was met (file path, "
                                    f"count, id).")
                    return json.dumps({
                        "ok": False,
                        "error": (
                            f"result_summary is too short ({len(summary)} chars). "
                            f"Describe what you actually produced — file paths, "
                            f"counts, identifiers — not just 'done'.{acc_hint}"
                        ),
                    }, ensure_ascii=False)

            # Idempotent success: if the step is already in the target
            # state, return ok:true so the LLM moves on instead of retrying.
            matched = None
            for s in plan.steps:
                if s.id == step_id:
                    matched = s
                    break
            if matched is not None and matched.status.value == status:
                return json.dumps({
                    "ok": True,
                    "note": f"step '{step_id}' was already {status} — no-op",
                    "step": matched.to_dict(),
                }, ensure_ascii=False)

            # ── B-guard: reject start_step when another step is already
            # in_progress. Observed failure: agent loops on start_step of
            # the same conceptual work ("检索记忆和知识库") — each iteration
            # it re-calls start_step thinking "let me start again".
            # Force it to walk the state machine: complete/fail current first.
            if action == "start_step":
                busy = [s for s in plan.steps
                        if s.status.value == "in_progress" and s.id != step_id]
                if busy:
                    bs = busy[0]
                    return json.dumps({
                        "ok": False,
                        "error": (
                            f"Cannot start step '{step_id}' — step "
                            f"'{bs.id}' ({bs.title!r}) is already in_progress. "
                            f"Finish or fail it first:\n"
                            f"  - plan_update(action='complete_step', "
                            f"step_id='{bs.id}', result_summary='...')\n"
                            f"  - or plan_update(action='fail_step', "
                            f"step_id='{bs.id}', reason='...')\n"
                            f"Never run two steps in parallel."
                        ),
                        "busy_step": bs.to_dict(),
                    }, ensure_ascii=False)

            step = self.update_plan_step(step_id, status, summary)
            if step is not None:
                # ── Block 2 Review loop: run verifier on complete_step ──
                # Only fires on complete_step (not fail_step/start_step);
                # complete_step with a declared verify means "I think I'm
                # done — please check". Verifier failure rolls the step
                # back to FAILED with the verifier's reason in
                # result_summary, so the next plan_state snapshot the LLM
                # sees has the concrete failure info and can replan.
                verify_dict = None
                if action == "complete_step" and step.verify:
                    try:
                        verify_dict = self._run_step_verifier(step)
                    except Exception as _verr:
                        logger.warning(
                            "verifier hook crashed for step %s: %s",
                            step.id, _verr,
                        )
                if verify_dict is not None and not verify_dict.get("ok", False):
                    # Roll back: step → FAILED, splice verifier reason
                    # into result_summary for plan-in-context visibility.
                    step.status = StepStatus.FAILED
                    step.completed_at = time.time()
                    vs = verify_dict.get("summary", "") or "verifier rejected"
                    step.result_summary = (
                        f"{summary}\n[verifier:{verify_dict.get('verifier_kind','')}] "
                        f"{vs}"
                    )[:2000]
                    # If the plan's overall status was bumped to completed
                    # by update_plan_step (it checks all-done condition),
                    # reset it — at least one step is now FAILED.
                    plan.status = "active"
                    return json.dumps({
                        "ok": False,
                        "error": (
                            f"Step completion REJECTED by verifier "
                            f"({verify_dict.get('verifier_kind')}): {vs}"
                        ),
                        "verify": verify_dict,
                        "step": step.to_dict(),
                        "hint": (
                            "Fix the underlying issue and call "
                            "complete_step again, OR call fail_step + "
                            "replan if this step is unrecoverable."
                        ),
                    }, ensure_ascii=False)
                resp: dict = {"ok": True, "step": step.to_dict()}
                if verify_dict is not None:
                    resp["verify"] = verify_dict
                return json.dumps(resp, ensure_ascii=False)

            # update_plan_step returned None — step_id didn't match any
            # step in the plan. Surface the available IDs so the LLM can
            # retry with the right one OR conclude the plan is empty and
            # stop retrying.
            available = [{"id": s.id, "title": s.title,
                          "status": s.status.value if hasattr(s.status, "value") else str(s.status)}
                         for s in plan.steps]
            return json.dumps({
                "ok": False,
                "error": (
                    f"step_id {step_id!r} not found in current plan. "
                    f"Use one of the IDs below (exact string match). "
                    f"If the plan is empty or the wrong one, call "
                    f"plan_update(action='create_plan', ...) to replace it, "
                    f"or skip plan_update for this task."
                ),
                "available_steps": available,
            }, ensure_ascii=False)
        elif action == "add_step":
            step = self.add_plan_step(
                title=arguments.get("title", ""),
                detail=arguments.get("detail", ""))
            return json.dumps({"ok": step is not None,
                               "step": step.to_dict() if step else None},
                              ensure_ascii=False)
        elif action == "replan":
            plan = self._current_plan
            if not plan:
                return json.dumps({"ok": False, "error": "No active plan to replan"},
                                  ensure_ascii=False)
            # Keep completed/in_progress steps, remove pending
            plan.steps = [s for s in plan.steps
                          if s.status in (StepStatus.COMPLETED, StepStatus.IN_PROGRESS)]
            new_steps = arguments.get("steps", [])
            if isinstance(new_steps, str):
                try:
                    new_steps = json.loads(new_steps)
                except (json.JSONDecodeError, TypeError):
                    new_steps = []
            if not isinstance(new_steps, list):
                new_steps = []
            for s_data in new_steps:
                if isinstance(s_data, str):
                    try:
                        s_data = json.loads(s_data)
                    except (json.JSONDecodeError, TypeError):
                        s_data = {"title": s_data}
                if isinstance(s_data, dict):
                    step = plan.add_step(
                        title=s_data.get("title", ""),
                        detail=s_data.get("detail", ""),
                    )
                    if "depends_on" in s_data:
                        step.depends_on = s_data["depends_on"]
            plan.status = "active"
            task_summary = arguments.get("task_summary", "")
            if task_summary:
                plan.task_summary = task_summary
            kept = len(plan.steps) - len(new_steps)
            result_text = f"Replanned: kept {kept} completed steps, added {len(new_steps)} new steps"
            step_ids = [{"id": s.id, "title": s.title} for s in plan.steps]
            return json.dumps({"ok": True, "message": result_text,
                               "steps": step_ids}, ensure_ascii=False)
        else:
            return json.dumps({"error": f"Unknown action: {action}"},
                              ensure_ascii=False)

    # ---- Stale step detection (emergency fix for "IDLE + in_progress" bug) ──

    def _detect_stale_plan_steps(self, threshold_s: float = 180.0,
                                   emit_frames: bool = True) -> list[dict]:
        """Find plan steps that are in_progress but the agent isn't actually
        working on them.

        Both signals require the same threshold_s window — instant
        flagging on IDLE+in_progress was generating false positives:
        agent normally dips to IDLE briefly between assistant turns
        while the LLM is finishing the previous message and hasn't
        dispatched plan_update(complete_step) yet.

        Signals (need `since_activity > threshold_s`):
        1. **agent.status == IDLE** AND step.status == IN_PROGRESS
           → the LLM finished talking but never updated the plan state.
        2. **agent.status == BUSY** AND step.status == IN_PROGRESS
           → tool_call / LLM hung mid-execution.

        Per user rule (a): this method does NOT mutate step state. It
        only detects and emits a warning frame to ProgressBus so the UI
        can surface the issue to a human, who picks mark_failed / skip
        / resume via dedicated API endpoints.

        Returns a list of dicts describing each stale step.
        """
        plan = self._current_plan
        if plan is None or not plan.steps:
            return []

        # Figure out agent's current status. Imported lazily to avoid
        # circular-import pain (agent_types is itself imported from agent).
        try:
            from .agent_types import AgentStatus as _AS
            agent_idle = (self.status == _AS.IDLE)
        except Exception:
            agent_idle = str(getattr(self.status, "value", self.status)) == "idle"

        # Most recent "activity" timestamp — ANY of these count as the
        # agent doing something right now, so we should NOT flag stale:
        #   tool_call      — just fired a tool
        #   tool_result    — tool returned
        #   message        — LLM emitted a chunk
        #   plan_update    — state machine transition
        # Pure status pings / heartbeats don't count.
        ACTIVITY_KINDS = {"tool_call", "tool_result", "message",
                          "plan_update", "plan_step_update"}
        latest_activity_ts = 0.0
        try:
            events = getattr(self, "events", None) or []
            if events:
                # Scan back further (60) — long tools may have started 30s
                # ago but we want the emit timestamp, not the latest heartbeat
                for evt in reversed(events[-60:]):
                    kind = getattr(evt, "kind", "")
                    if kind not in ACTIVITY_KINDS:
                        continue
                    t = getattr(evt, "timestamp", 0.0) or 0.0
                    if t > latest_activity_ts:
                        latest_activity_ts = t
                        break  # first match wins (already in reverse order)
        except Exception:
            pass

        now = time.time()
        stale: list[dict] = []
        for s in plan.steps:
            if s.status != StepStatus.IN_PROGRESS:
                continue
            started = s.started_at or 0.0
            # "since_activity" is the ONE truth for user-visible "X 秒无活动".
            # Prefer latest activity ts; fall back to step.started_at if no
            # activity events yet (rare edge case).
            anchor = latest_activity_ts if latest_activity_ts > 0 else started
            since_activity = (now - anchor) if anchor > 0 else 0.0

            reason = ""
            if since_activity > threshold_s:
                # Both IDLE and BUSY cases need the window to elapse before
                # flagging — otherwise brief IDLE pauses between assistant
                # messages trigger false positives.
                if agent_idle:
                    reason = (f"agent 闲置 {int(since_activity)}s 但 step "
                              f"仍为 in_progress (阈值 {int(threshold_s)}s)")
                else:
                    reason = (f"无工具活动 {int(since_activity)}s "
                              f"(阈值 {int(threshold_s)}s)")

            if not reason:
                continue

            stale_info = {
                "step_id": s.id,
                "title": s.title,
                "started_at": started,
                # Report "since_activity" — the number users can act on —
                # not "age" (age only tells you how long the step has been
                # running, regardless of activity). This is what the UI
                # shows as "此步骤已 X 无活动".
                "stale_s": since_activity,
                "reason": reason,
            }
            stale.append(stale_info)

            if emit_frames:
                try:
                    from .progress_bus import emit_step_stale
                    emit_step_stale(
                        plan_id=plan.id, step_id=s.id, agent_id=self.id,
                        step_title=s.title, stale_s=since_activity,
                        reason=reason,
                    )
                except Exception as _e:
                    logger.debug("stale-step frame emit failed: %s", _e)

        return stale

    # ---- Manual-resolution helpers (called by /plan/step/* API endpoints) ──

    def mark_step_failed(self, step_id: str, reason: str = "") -> Optional["ExecutionStep"]:
        """Human-initiated failure. Moves step → FAILED with reason."""
        plan = self._current_plan
        if plan is None:
            return None
        for s in plan.steps:
            if s.id == step_id:
                s.status = StepStatus.FAILED
                s.completed_at = time.time()
                s.result_summary = (
                    (s.result_summary or "") +
                    f"\n[manually marked FAILED] {reason}"[:2000]
                ).strip()
                # Plan can't be completed if a step is failed
                plan.status = "active"
                return s
        return None

    def mark_step_skipped(self, step_id: str, reason: str = "") -> Optional["ExecutionStep"]:
        """Human-initiated skip. Moves step → SKIPPED; allows plan to
        progress past it without the agent's output."""
        plan = self._current_plan
        if plan is None:
            return None
        for s in plan.steps:
            if s.id == step_id:
                s.status = StepStatus.SKIPPED
                s.completed_at = time.time()
                s.result_summary = (
                    (s.result_summary or "") +
                    f"\n[manually SKIPPED] {reason}"[:2000]
                ).strip()
                # Check all-done state now that this one's out of the way
                if all(x.status in (StepStatus.COMPLETED, StepStatus.SKIPPED)
                       for x in plan.steps):
                    plan.status = "completed"
                    plan.completed_at = time.time()
                return s
        return None

    def resume_step(self, step_id: str) -> Optional["ExecutionStep"]:
        """Human clicks '继续': reset the stale detector so the warning
        disappears on the next poll.

        Two moves:
          1. step.started_at = now() — refreshes age
          2. emit a synthetic `plan_step_update` event — refreshes the
             `latest_activity_ts` used by _detect_stale_plan_steps, so
             even with the new since-activity logic the warning clears.

        Does NOT change status — caller should ensure agent is chatting /
        running so it can actually pick up where it left off.
        """
        plan = self._current_plan
        if plan is None:
            return None
        for s in plan.steps:
            if s.id == step_id:
                if s.status == StepStatus.IN_PROGRESS:
                    s.started_at = time.time()
                    # Inject an activity event so the stale detector's
                    # latest_activity_ts moves to now on the next poll.
                    try:
                        self._log("plan_step_update", {
                            "step_id": s.id,
                            "action": "resume",
                            "source": "human",
                        })
                    except Exception:
                        pass
                return s
        return None

    # ---- Block 2 Review loop: verifier helper ──────────────────────

    def _run_step_verifier(self, step: "ExecutionStep") -> Optional[dict]:
        """Run the verifier declared on ``step`` and return its dict result.

        Returns None when the step has no verify config (caller skips
        the check). Otherwise always returns a dict (even on crash —
        VerifyResult itself is crash-safe).

        Also emits a verify_result frame to ProgressBus so the UI
        timeline sees it even before the plan_state digest updates.
        """
        cfg_dict = step.verify or {}
        if not cfg_dict:
            return None
        from .verifier import VerifyConfig, VerifyContext, run_verify
        cfg = VerifyConfig.from_dict(cfg_dict)
        if cfg is None:
            return {
                "ok": False,
                "summary": "verify config invalid",
                "error": f"Invalid verify config on step: {cfg_dict!r}",
                "verifier_kind": "(invalid)",
            }
        # Inject LLM callable for llm_judge verifier. Bridge via this
        # agent's provider / model so the judge uses the SAME model the
        # caller is using (keeps cost + behavior consistent).
        def _llm_call(messages, _options):
            try:
                from . import llm as _llm
                prov, mdl = self._resolve_effective_provider_model()
                return _llm.chat_no_stream(
                    messages, tools=None,
                    provider=prov, model=mdl,
                    temperature=self._effective_temperature()
                    if hasattr(self, "_effective_temperature") else None,
                )
            except Exception as _llm_err:
                logger.debug("verifier llm_call failed: %s", _llm_err)
                return {"message": {"content": ""}}
        ctx = VerifyContext(
            workspace_dir=self.get_active_shared_workspace() or self.working_dir or "",
            step_started_at=step.started_at,
            acceptance=step.acceptance,
            result_summary=step.result_summary,
            agent_id=self.id,
            plan_id=self._current_plan.id if self._current_plan else "",
            step_id=step.id,
            llm_call=_llm_call,
        )
        result = run_verify(cfg, ctx)
        # Emit progress bus frame for UI timeline — best-effort.
        try:
            from .progress_bus import get_bus, ProgressFrame
            plan_id = self._current_plan.id if self._current_plan else ""
            get_bus().publish(ProgressFrame(
                kind="verify_result",
                channel=f"plan:{plan_id}" if plan_id else f"agent:{self.id}",
                plan_id=plan_id,
                step_id=step.id,
                agent_id=self.id,
                data={
                    "ok": result.ok,
                    "summary": result.summary,
                    "verifier_kind": result.verifier_kind,
                    "duration_s": round(result.duration_s, 2),
                    "required": cfg.required,
                    "details_preview": (str(result.details) or "")[:500],
                },
            ))
        except Exception as _e:
            logger.debug("verify_result frame emit failed: %s", _e)
        return result.to_dict()

    # ---- Execution Plan management ----

    def create_execution_plan(self, task_summary: str,
                               steps: list[dict] | None = None) -> ExecutionPlan:
        """Create a new execution plan for the current task."""
        plan = ExecutionPlan(task_summary=task_summary)
        if steps:
            _valid_purposes = {"tool-heavy", "multimodal", "reasoning",
                               "analysis", "coding", "default"}
            for s in steps:
                step = plan.add_step(
                    title=s.get("title", ""),
                    detail=s.get("detail", ""),
                    acceptance=s.get("acceptance", ""),
                    verify=s.get("verify"),
                )
                if "depends_on" in s:
                    step.depends_on = s["depends_on"]
                # Capture the LLM's routing hint (new in Phase 1-B). The
                # primary LLM fills this when it can see the model-scores
                # table in its system prompt; otherwise it stays "" and
                # the per-iteration resolver falls back to keyword detection.
                _purpose = str(s.get("llm_purpose") or "").strip()
                if _purpose in _valid_purposes:
                    step.llm_purpose = _purpose
                _rationale = str(s.get("llm_rationale") or "").strip()
                if _rationale:
                    step.llm_rationale = _rationale
        # Record the message anchor BEFORE swapping _current_plan so the
        # fold-on-completion logic knows which messages belong to this
        # plan's lifecycle (everything from this index onward, until the
        # plan is marked completed).
        try:
            plan.msg_anchor_idx = len(self.messages)
        except Exception:
            plan.msg_anchor_idx = -1
        self._current_plan = plan
        self.execution_plans.append(plan)
        # Emit event so UI can update
        self._log("plan_created", {
            "plan_id": plan.id,
            "task": task_summary[:100],
            "steps": len(plan.steps),
        })
        # 写入 L3 记忆：里程碑/步骤持久化
        self._write_plan_to_memory(plan)
        # 更新状态机
        self._update_agent_phase()
        return plan

    def _auto_advance_plan(self, tool_name: str = "") -> None:
        """Soft-fallback auto-advance — fires ONLY when the LLM hasn't
        started managing the plan state machine on its own yet.

        Once the LLM calls plan_update(start_step|complete_step|...) even
        once, a flag flips and this helper no-ops for the rest of the
        turn — ensuring we don't fight the LLM. The flag resets at turn
        start (see agent_execution chat loop entry).

        Semantics:
          - If no step is in_progress AND at least one is pending AND
            the LLM hasn't touched plan_update yet this turn → start
            the earliest pending step.
          - Called with tool_name='plan_update' marks the LLM as
            managing state itself and disables auto-advance until
            next turn.
        """
        if tool_name == "plan_update":
            # LLM is explicitly driving the state machine — stand down.
            self._llm_manages_plan_this_turn = True
            return
        if getattr(self, "_llm_manages_plan_this_turn", False):
            return
        plan = self._current_plan
        if plan is None or not plan.steps or plan.status != "active":
            return
        try:
            from .agent_types import StepStatus as _SS
        except Exception:
            return
        in_prog = next((s for s in plan.steps
                        if s.status == _SS.IN_PROGRESS), None)
        if in_prog is not None:
            return
        pending = [s for s in plan.steps if s.status == _SS.PENDING]
        if not pending:
            return
        target = pending[0]
        self.update_plan_step(target.id, "in_progress")
        logger.info(
            "Agent %s: auto-started plan step '%s' (id=%s) on tool=%s",
            self.id[:8], target.title[:50], target.id, tool_name or "-")

    def _fold_completed_plan_into_recap(self, plan: "ExecutionPlan") -> None:
        """Compress a just-completed plan's messages into one user-role recap.

        Triggered the moment the plan transitions to ``status="completed"``
        (all steps are COMPLETED or SKIPPED). Replaces messages from
        ``plan.msg_anchor_idx`` onward with a single user message:

            [完成的 plan: <task_summary>]
            完成于: <ts>
            步骤:
              ✓ <title> — <result_summary>
              ✓ ...
            交付: <artifact paths if any>

        Token win: a multi-step plan with N tool rounds (~30 messages,
        ~10K chars) collapses to one ~500-char recap, freeing 95% of the
        history budget for the next plan/turn. Solves "agent talks about
        old task in new conversation" — old messages physically aren't
        in self.messages anymore.

        Skipped if:
          - feature flag ``TUDOU_AGENT_FOLD_PLAN`` set to "0" (operator
            opt-out for debugging).
          - plan.msg_anchor_idx is invalid (-1 or out of bounds).
          - fewer than 2 messages would be folded (no real win).

        Never raises — fold is best-effort; if anything goes sideways
        the original messages stay put.
        """
        if os.environ.get("TUDOU_AGENT_FOLD_PLAN", "1") in ("0", "false", "False"):
            return
        if plan is None or plan.status != "completed":
            return
        anchor = int(getattr(plan, "msg_anchor_idx", -1) or -1)
        if anchor < 0 or anchor >= len(self.messages):
            return
        in_window = self.messages[anchor:]
        if len(in_window) < 2:
            return
        try:
            from .agent_types import StepStatus
        except Exception:
            return

        # Pull artifact paths out of tool results (best-effort regex)
        artifacts: list[str] = []
        try:
            for m in in_window:
                if m.get("role") != "tool":
                    continue
                c = m.get("content") or ""
                if not isinstance(c, str):
                    continue
                # match common path-looking strings; we're conservative
                for path in re.findall(r"([\w/.\-]+\.[a-zA-Z]{2,5})", c[:500]):
                    if path not in artifacts and len(path) <= 200:
                        artifacts.append(path)
                if len(artifacts) >= 8:
                    break
        except Exception:
            artifacts = []

        # Build step lines
        sym_for: dict = {
            StepStatus.COMPLETED: "✓",
            StepStatus.SKIPPED:   "—",
            StepStatus.FAILED:    "✗",
            StepStatus.PENDING:   "·",
            StepStatus.IN_PROGRESS: "·",
        }
        step_lines = []
        for s in plan.steps:
            sym = sym_for.get(s.status, "·")
            title = (s.title or "")[:60]
            rs = (s.result_summary or "")[:120]
            line = f"  {sym} {title}"
            if rs:
                line += f" — {rs}"
            step_lines.append(line)

        # Find final assistant text (skip the empty-content tool-call ones)
        final_text = ""
        for m in reversed(in_window):
            if m.get("role") == "assistant" and not m.get("tool_calls"):
                c = m.get("content")
                if isinstance(c, str) and c.strip():
                    final_text = c.strip()[:400]
                    break

        ts = time.strftime("%Y-%m-%d %H:%M:%S",
                            time.localtime(plan.completed_at or time.time()))
        recap_parts = [
            f"[已完成 plan] {(plan.task_summary or '')[:120]}",
            f"完成于: {ts}",
            f"步骤 ({len(plan.steps)}):",
            *step_lines,
        ]
        if artifacts:
            recap_parts.append("交付: " + ", ".join(artifacts[:8]))
        if final_text:
            recap_parts.append(f"摘要: {final_text}")
        recap = {
            "role": "user",
            "content": "\n".join(recap_parts),
            "_source": "plan_recap",
            "_plan_id": plan.id,
        }

        before = len(self.messages)
        self.messages = self.messages[:anchor] + [recap]
        try:
            self._log("plan_folded", {
                "plan_id": plan.id,
                "anchor_idx": anchor,
                "msgs_folded": len(in_window),
                "msgs_after": len(self.messages),
            })
            logger.info(
                "Agent %s: plan %s folded — %d msgs → recap (anchor=%d, "
                "before=%d, after=%d)",
                self.id[:8], plan.id, len(in_window), anchor,
                before, len(self.messages),
            )
        except Exception:
            pass

    def _auto_complete_in_progress_on_turn_end(self) -> None:
        """Called when a chat turn ends. If any step is still in_progress
        and the LLM never explicitly called complete_step/fail_step,
        mark it completed with an auto-generated summary — otherwise
        the plan stays permanently 'in progress' after the turn."""
        plan = self._current_plan
        if plan is None or not plan.steps:
            return
        try:
            from .agent_types import StepStatus as _SS
        except Exception:
            return
        stuck = [s for s in plan.steps if s.status == _SS.IN_PROGRESS]
        for s in stuck:
            self.update_plan_step(
                s.id, "completed",
                result_summary="(auto) turn ended without explicit complete_step",
            )
            logger.info(
                "Agent %s: auto-completed '%s' on turn end",
                self.id[:8], s.title[:50])

    def update_plan_step(self, step_id: str, status: str,
                          result_summary: str = "") -> ExecutionStep | None:
        """Update a step's status in the current plan."""
        plan = self._current_plan
        if not plan:
            return None
        if status == "in_progress":
            step = plan.start_step(step_id)
        elif status == "completed":
            step = plan.complete_step(step_id, result_summary)
            # NOTE: Previously we auto-chained start_step on the next
            # pending here. That fought with LLM's explicit start_step
            # calls — LLM calls complete_step(s1) then start_step(s2);
            # our auto-chain had already started s2 under the hood, so
            # the second start_step either duplicated the step or flipped
            # it to a confused state (observed "same title two ids" +
            # step marked skipped). Simplest fix: LLM that calls
            # complete_step clearly knows the state machine — trust it
            # and don't pre-empt.
        elif status == "failed":
            step = plan.fail_step(step_id, result_summary)
        else:
            return None
        if step:
            self._log("plan_step_updated", {
                "plan_id": plan.id,
                "step_id": step.id,
                "title": step.title,
                "status": status,
            })
            # 步骤完成时写入 L3 记忆
            if status == "completed" and plan:
                self._write_step_completion_to_memory(plan, step)
            # 更新状态机
            self._update_agent_phase()
            # ── Plan-level completion hook: fold history into recap ──
            # When this step's completion just made plan.status == "completed"
            # (all steps done), compress this plan's messages into a single
            # user-role recap. This is the "Done.on_enter fold" we agreed on
            # — applied at V1's plan-completion moment instead of a V2 phase.
            if (status == "completed"
                    and plan is not None
                    and getattr(plan, "status", "") == "completed"):
                try:
                    self._fold_completed_plan_into_recap(plan)
                except Exception as _fe:
                    logger.debug("plan recap fold skipped: %s", _fe)
        return step

    def add_plan_step(self, title: str, detail: str = "") -> ExecutionStep | None:
        """Add a new step to the current plan (during execution)."""
        if not self._current_plan:
            return None
        step = self._current_plan.add_step(title=title, detail=detail)
        self._log("plan_step_added", {
            "plan_id": self._current_plan.id,
            "step_id": step.id,
            "title": title,
        })
        return step

    def get_current_plan(self) -> dict | None:
        """Get the current execution plan for UI display."""
        if self._current_plan:
            return self._current_plan.to_dict()
        return None

    def format_plan_state_for_llm(self) -> str:
        """P0/L1 — render the current plan as a compact block for the LLM.

        Injected into the conversation right before each LLM call so the
        model always sees "which step am I on, what does done look like,
        what's blocking what". Returns empty string when there's no plan
        or the plan has no steps — caller omits the injection entirely
        in that case.

        Format (stable; referenced by SKILLs):

            <plan_state>
            task: 生成云厂商技术服务能力报告 PPT
            current: [3] 创建 PPTX 报告  (IN_PROGRESS, 2m ago)
              acceptance: 产出 *.pptx 文件 ≥ 5 slides，落 $AGENT_WORKSPACE
            done:
              [1] 搜索 2025 云厂商数据 — "AWS 31%, Azure 25%, GCP 11%"
              [2] 搜索图表资源      — "找到 3 张 statista 图"
            pending:
              [4] 发送邮件           blocked_by=[3]
            rules:
              - 只做 current 这一步承诺的事；别跳到 pending
              - 标完成前在 result_summary 里引用 acceptance 是否满足
            </plan_state>

        Keep it short: bullets truncated to ~80 chars each; no more than
        8 done/pending rows shown (older done rows collapsed to counts).
        """
        plan = self._current_plan
        if plan is None or not plan.steps:
            return ""

        def _truncate(s: str, n: int = 80) -> str:
            s = (s or "").replace("\n", " ").strip()
            return s if len(s) <= n else s[: n - 1] + "…"

        def _fmt_step_line(s: ExecutionStep, *, show_acceptance: bool = False) -> str:
            bits = [f"[{s.order}] {_truncate(s.title, 60)}"]
            if s.result_summary:
                bits.append(f'— "{_truncate(s.result_summary, 60)}"')
            line = "  " + " ".join(bits)
            if show_acceptance and s.acceptance:
                line += f"\n    acceptance: {_truncate(s.acceptance, 120)}"
            return line

        # Partition steps
        current_steps = [s for s in plan.steps if s.status == StepStatus.IN_PROGRESS]
        done_steps = [s for s in plan.steps
                      if s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED)]
        pending_steps = [s for s in plan.steps if s.status == StepStatus.PENDING]
        failed_steps = [s for s in plan.steps if s.status == StepStatus.FAILED]

        lines: list[str] = ["<plan_state>"]
        if plan.task_summary:
            lines.append(f"task: {_truncate(plan.task_summary, 140)}")

        # CURRENT — highest priority, always show full detail + acceptance
        if current_steps:
            lines.append("current:")
            for s in current_steps:
                age = ""
                if s.started_at > 0:
                    elapsed = time.time() - s.started_at
                    if elapsed < 60:
                        age = f", {int(elapsed)}s ago"
                    elif elapsed < 3600:
                        age = f", {int(elapsed/60)}m ago"
                    else:
                        age = f", {int(elapsed/3600)}h ago"
                lines.append(f"  [{s.order}] {_truncate(s.title, 60)}  (IN_PROGRESS{age})")
                if s.acceptance:
                    lines.append(f"    acceptance: {_truncate(s.acceptance, 140)}")
                if s.detail and not s.acceptance:
                    # Fall back to detail when acceptance is empty (legacy plans).
                    lines.append(f"    detail: {_truncate(s.detail, 140)}")
        else:
            # No in-progress step — tell the model so it starts the
            # next one rather than faking a completion.
            if pending_steps:
                nxt = pending_steps[0]
                lines.append("current: (none — next step to start)")
                lines.append(f"  [{nxt.order}] {_truncate(nxt.title, 60)}")
                if nxt.acceptance:
                    lines.append(f"    acceptance: {_truncate(nxt.acceptance, 140)}")
            elif not failed_steps and done_steps and \
                    len(done_steps) == len(plan.steps):
                lines.append("current: (all steps done — summarize and stop)")

        # DONE — compact (last 5, show summary if present)
        if done_steps:
            lines.append("done:")
            for s in done_steps[-5:]:
                lines.append(_fmt_step_line(s))
            if len(done_steps) > 5:
                lines.append(f"  … (+ {len(done_steps) - 5} earlier)")

        # PENDING — show up to 5, highlight dependencies
        if pending_steps:
            lines.append("pending:")
            for s in pending_steps[:5]:
                dep_suffix = ""
                if s.depends_on:
                    dep_ids = [d[:8] for d in s.depends_on]
                    dep_suffix = f"  blocked_by={dep_ids}"
                lines.append(f"  [{s.order}] {_truncate(s.title, 60)}{dep_suffix}")
            if len(pending_steps) > 5:
                lines.append(f"  … (+ {len(pending_steps) - 5} more)")

        # FAILED — always show, can't collapse
        if failed_steps:
            lines.append("failed:")
            for s in failed_steps:
                bits = f"[{s.order}] {_truncate(s.title, 60)}"
                if s.result_summary:
                    bits += f' — error: "{_truncate(s.result_summary, 60)}"'
                lines.append("  " + bits)

        # Rules — minimal, only if we actually have non-trivial plan.
        if current_steps or pending_steps:
            lines.append("rules:")
            lines.append("  - 只做 current 这一步承诺的事；不要跳到 pending")
            lines.append("  - complete_step 前 result_summary 要引用 acceptance 是否满足")
            lines.append("  - acceptance 未满足就 fail_step，不要硬标完成")

        lines.append("</plan_state>")
        return "\n".join(lines)

    def get_execution_plans(self, limit: int = 10) -> list[dict]:
        """Get recent execution plans."""
        return [p.to_dict() for p in self.execution_plans[-limit:]]

    def delegate(self, task: str, from_agent: str = "hub", child_agent: "Agent | None" = None) -> str:
        """
        Enhanced delegate() with depth tracking, isolation, and parent-child relationship.

        Args:
            task: Task description/prompt
            from_agent: Name of delegating agent
            child_agent: Optional pre-created Agent instance. If None, creates a new sub-agent.

        Returns:
            Result string from the delegated task

        Raises:
            RuntimeError: If delegation depth exceeds max_delegate_depth
        """
        # Check delegation depth limit
        if self._delegate_depth >= self._max_delegate_depth:
            error_msg = (
                f"Delegation depth limit reached (current: {self._delegate_depth}, "
                f"max: {self._max_delegate_depth}). Cannot spawn new sub-agent."
            )
            self._log("delegation_error", {
                "error": "depth_limit_exceeded",
                "current_depth": self._delegate_depth,
                "max_depth": self._max_delegate_depth,
            })
            logger.error(error_msg)
            return f"ERROR: {error_msg}"

        # ── Admin-configurable fork policy (auth.tool_policy.fork_policy) ──
        # Resolve prospective child_role for role-edge check.
        # If caller passed a pre-built child_agent, use its role; otherwise the
        # new sub-agent will inherit self.role (see child_agent creation below).
        _prospective_child_role = child_agent.role if child_agent is not None else self.role
        _policy = None
        try:
            from .auth import get_auth as _get_auth
            _policy = _get_auth().tool_policy
            cost_last_hour = 0.0
            try:
                cost_last_hour = float(getattr(self.cost_tracker, "cost_last_hour", lambda: 0.0)())
            except Exception:
                pass
            ok, reason = _policy.check_fork_allowed(
                parent_id=self.id, parent_role=self.role,
                parent_depth=self._delegate_depth,
                cost_last_hour_usd=cost_last_hour,
                child_role=_prospective_child_role,
            )
            if not ok:
                self._log("delegation_error", {"error": "fork_policy_blocked", "reason": reason})
                logger.warning("Fork policy blocked: %s", reason)
                return f"ERROR: fork policy denied: {reason}"
            _policy.register_fork_start(self.id)
        except Exception as _e:
            logger.debug("fork policy check skipped: %s", _e)
            _policy = None

        # Create or use provided child agent
        if child_agent is None:
            child_agent = Agent(
                name=f"{self.name}_child_{uuid.uuid4().hex[:6]}",
                role=self.role,
                model=self.model,
                provider=self.provider,
                # Inherit working directory; shared workspace is now
                # derived from active context, not stored — child will
                # resolve its own at runtime based on whatever context
                # it ends up in.
                working_dir=self.working_dir,
                shared_workspace="",
                system_prompt=self.system_prompt,
                profile=AgentProfile.from_dict(self.profile.to_dict()),
                node_id=self.node_id,
                parent_id=self.id,  # Track parent relationship
                authorized_workspaces=list(self.authorized_workspaces),
            )

        # Set child's depth to parent's depth + 1
        child_agent._delegate_depth = self._delegate_depth + 1
        child_agent._max_delegate_depth = self._max_delegate_depth

        # Inherit cancellation event from parent for interrupt signaling
        child_agent._cancellation_event = self._cancellation_event

        # Track active child
        with self._active_children_lock:
            self._active_children.append((child_agent.id, child_agent))

        try:
            # Build prompt with delegation metadata
            prompt = f"[Delegated task from {from_agent} | depth={child_agent._delegate_depth}/{self._max_delegate_depth}]\n{task}"

            # Log delegation event
            self._log("inter_agent_message", {
                "from_agent": from_agent,
                "to_agent": child_agent.id,
                "content": task[:500],
                "msg_type": "delegation",
                "depth": child_agent._delegate_depth,
            })

            logger.info(
                "DELEGATE: parent=%s child=%s task_len=%d depth=%d/%d",
                self.id, child_agent.id, len(task),
                child_agent._delegate_depth, self._max_delegate_depth
            )

            # Execute delegated task with isolation (separate message history)
            result = child_agent.chat(prompt)

            return result

        except Exception as e:
            error_msg = f"Delegation to {child_agent.id} failed: {str(e)}"
            self._log("delegation_error", {
                "error": str(e),
                "child_agent": child_agent.id,
                "depth": child_agent._delegate_depth,
            })
            logger.error(error_msg)
            return f"ERROR: {error_msg}"

        finally:
            # Remove child from active list when done
            with self._active_children_lock:
                self._active_children = [
                    (aid, ag) for aid, ag in self._active_children
                    if aid != child_agent.id
                ]
            # Decrement fork policy counter
            try:
                if _policy is not None:
                    _policy.register_fork_end(self.id)
            except Exception:
                pass

    def delegate_parallel(self, tasks: list[dict], max_workers: int = 4) -> list[dict]:
        """
        Spawn multiple sub-agents in parallel to handle a list of tasks.

        This is a Hermes-style parallel delegation pattern that:
        - Creates isolated sub-agent instances (each with separate message history)
        - Executes tasks concurrently via ThreadPoolExecutor
        - Shares: working_dir, tool access, LLM config, parent context
        - Respects: delegation depth limits, cancellation signals

        Args:
            tasks: List of task dicts, each containing:
                - "task" (str, required): Task description
                - "agent_id" (str, optional): Custom sub-agent ID (for tracking)
                - "context" (str, optional): Extra context to inject into task
            max_workers: Max parallel sub-agents (capped at 4 for safety)

        Returns:
            List of result dicts:
                [{
                    "agent_id": str,
                    "task": str,
                    "status": "success" | "failed" | "cancelled",
                    "result": str,
                    "error": str (if failed),
                    "duration": float,
                }]

        Example:
            results = agent.delegate_parallel([
                {"task": "Review code in file A for security", "agent_id": "reviewer_a"},
                {"task": "Review code in file B for performance", "agent_id": "reviewer_b"},
                {"task": "Write unit tests", "context": "Use pytest framework"},
            ])
        """
        if self._delegate_depth >= self._max_delegate_depth:
            error_msg = (
                f"Cannot delegate_parallel: depth limit reached "
                f"(current: {self._delegate_depth}, max: {self._max_delegate_depth})"
            )
            self._log("parallel_delegation_error", {"error": "depth_limit_exceeded"})
            return [{
                "status": "failed",
                "error": error_msg,
                "task": t.get("task", ""),
                "agent_id": t.get("agent_id", "unknown"),
                "result": "",
                "duration": 0.0,
            } for t in tasks]

        # Cap max_workers at 4 for safety
        max_workers = min(max_workers, 4)

        self._log("parallel_delegation_start", {
            "task_count": len(tasks),
            "max_workers": max_workers,
            "depth": self._delegate_depth,
        })

        logger.info(
            "PARALLEL_DELEGATE: parent=%s tasks=%d workers=%d depth=%d/%d",
            self.id, len(tasks), max_workers,
            self._delegate_depth, self._max_delegate_depth
        )

        results = []
        start_time = time.time()

        # Pre-flight fork policy check (one allowance per parallel task)
        try:
            from .auth import get_auth as _get_auth_pp
            _pp_policy = _get_auth_pp().tool_policy
        except Exception:
            _pp_policy = None

        def _execute_task(task_spec: dict) -> dict:
            """Execute a single task in a sub-agent (runs in thread)."""
            task_text = task_spec.get("task", "")
            agent_id = task_spec.get("agent_id", f"sub_{uuid.uuid4().hex[:6]}")
            context = task_spec.get("context", "")
            task_start = time.time()

            # Per-task fork policy check
            # parallel sub-agents inherit self.role, so child_role = self.role
            if _pp_policy is not None:
                try:
                    ok, reason = _pp_policy.check_fork_allowed(
                        parent_id=self.id, parent_role=self.role,
                        parent_depth=self._delegate_depth,
                        child_role=task_spec.get("role") or self.role,
                    )
                    if not ok:
                        return {"agent_id": agent_id, "task": task_text,
                                "status": "blocked", "result": "",
                                "error": f"fork policy: {reason}",
                                "duration": time.time() - task_start}
                    _pp_policy.register_fork_start(self.id)
                except Exception:
                    pass

            try:
                # Check cancellation signal
                if self._cancellation_event.is_set():
                    return {
                        "agent_id": agent_id,
                        "task": task_text,
                        "status": "cancelled",
                        "result": "",
                        "error": "Cancelled by parent agent",
                        "duration": time.time() - task_start,
                    }

                # Create isolated sub-agent. shared_workspace not
                # passed — sub-agent will derive its own from active
                # context when it runs (parallel workers always run in
                # solo unless explicitly attached to a project).
                sub_agent = Agent(
                    name=f"{self.name}_parallel_{agent_id}",
                    role=self.role,
                    model=self.model,
                    provider=self.provider,
                    working_dir=self.working_dir,
                    shared_workspace="",
                    system_prompt=self.system_prompt,
                    profile=AgentProfile.from_dict(self.profile.to_dict()),
                    node_id=self.node_id,
                    parent_id=self.id,
                    authorized_workspaces=list(self.authorized_workspaces),
                )

                # Set depth and inherit cancellation event
                sub_agent._delegate_depth = self._delegate_depth + 1
                sub_agent._max_delegate_depth = self._max_delegate_depth
                sub_agent._cancellation_event = self._cancellation_event

                # Track as active child
                with self._active_children_lock:
                    self._active_children.append((sub_agent.id, sub_agent))

                try:
                    # Build task prompt with context
                    full_task = task_text
                    if context:
                        full_task = f"{task_text}\n\n[Additional Context]\n{context}"

                    prompt = f"[Parallel delegated task | agent={agent_id} | depth={sub_agent._delegate_depth}/{self._max_delegate_depth}]\n{full_task}"

                    # Execute in isolation (separate message history)
                    result = sub_agent.chat(prompt)

                    return {
                        "agent_id": agent_id,
                        "task": task_text,
                        "status": "success",
                        "result": result,
                        "error": "",
                        "duration": time.time() - task_start,
                    }

                finally:
                    # Clean up from active children list
                    with self._active_children_lock:
                        self._active_children = [
                            (aid, ag) for aid, ag in self._active_children
                            if aid != sub_agent.id
                        ]
                    if _pp_policy is not None:
                        try:
                            _pp_policy.register_fork_end(self.id)
                        except Exception:
                            pass

            except Exception as e:
                if _pp_policy is not None:
                    try:
                        _pp_policy.register_fork_end(self.id)
                    except Exception:
                        pass
                return {
                    "agent_id": agent_id,
                    "task": task_text,
                    "status": "failed",
                    "result": "",
                    "error": str(e),
                    "duration": time.time() - task_start,
                }

        # Execute tasks in parallel using ThreadPoolExecutor
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_execute_task, task): task for task in tasks}

            for future in concurrent.futures.as_completed(futures):
                try:
                    result = future.result(timeout=300)  # 5 min timeout per task
                    results.append(result)
                except Exception as e:
                    task = futures[future]
                    results.append({
                        "agent_id": task.get("agent_id", "unknown"),
                        "task": task.get("task", ""),
                        "status": "failed",
                        "result": "",
                        "error": str(e),
                        "duration": time.time() - task_start,
                    })

        total_duration = time.time() - start_time

        self._log("parallel_delegation_complete", {
            "task_count": len(tasks),
            "success": sum(1 for r in results if r["status"] == "success"),
            "failed": sum(1 for r in results if r["status"] == "failed"),
            "cancelled": sum(1 for r in results if r["status"] == "cancelled"),
            "duration": total_duration,
        })

        logger.info(
            "PARALLEL_DELEGATE COMPLETE: parent=%s success=%d failed=%d duration=%.2fs",
            self.id,
            sum(1 for r in results if r["status"] == "success"),
            sum(1 for r in results if r["status"] == "failed"),
            total_duration
        )

        return results

    def cancel_children(self) -> dict:
        """
        Signal all active child agents to stop execution.

        Sets a threading.Event that child agents check in their chat loops
        (if abort_check callback is used). Returns summary of cancellation.

        Returns:
            {"cancelled_count": int, "agent_ids": list[str]}
        """
        self._cancellation_event.set()

        with self._active_children_lock:
            agent_ids = [aid for aid, _ in self._active_children]
            child_count = len(self._active_children)

        self._log("children_cancelled", {
            "count": child_count,
            "agent_ids": agent_ids,
        })

        logger.info(
            "CANCEL_CHILDREN: parent=%s cancelled=%d agents=%s",
            self.id, child_count, agent_ids
        )

        return {
            "cancelled_count": child_count,
            "agent_ids": agent_ids,
        }

    def clear(self):
        """Wipe agent's SOLO chat state — direct-chat messages + events
        + any active plan. Project / meeting buckets are untouched
        because they belong to those surfaces, not this one.

        Scope (2026-04-29):
          * solo bucket in ``_messages_by_context``  → cleared
          * project:{*} / meeting:{*} buckets        → preserved
          * ``self.events``                          → cleared
            (this list backs the direct-agent chat page; project chats
             read from ``project.chat_history`` instead, so they aren't
             affected)
          * active plan                              → marked SKIPPED

        Used by:
          - "Clear Chat" button on the direct agent chat page
            → POST /api/portal/agent/{id}/clear
          - The persistent ``/new`` slash command (skip_history=true)

        For surgical per-project / per-meeting wipes, use:
          - DELETE /api/portal/projects/{id}/chat
          - DELETE /api/portal/meetings/{id}/messages

        Why we also abandon ``_current_plan``: the watchdog
        (``Hub._maybe_wake_stuck_agent``) reads the active plan to
        decide when to nudge an idle agent. If the user clears chat
        but the plan remains "active" with open steps, the agent gets
        re-woken for a task whose context the user just deleted.
        """
        with self._lock:
            # Clear ONLY the solo bucket. self.messages currently
            # points at whatever context was active; rebind it to the
            # (now-empty) solo bucket so subsequent writes from
            # admin-side go to the right place.
            try:
                self._messages_by_context["solo"] = []
                self._active_context_id = "solo"
                self.messages = self._messages_by_context["solo"]
            except Exception:
                # Defensive: if the per-context fields aren't present
                # (very old agent state), fall back to legacy clear.
                self.messages.clear()
            self.events.clear()
            # Abandon any in-flight plan so the watchdog doesn't resurrect
            # the cleared task. Mark "completed" with a synthetic note so
            # the plan still appears in execution_plans history if anyone
            # reads it (audit trail), but no step stays open.
            plan = getattr(self, "_current_plan", None)
            if plan is not None and getattr(plan, "status", "") == "active":
                from .agent_types import StepStatus
                _now = time.time()
                for s in (plan.steps or []):
                    if s.status in (StepStatus.PENDING,
                                    StepStatus.IN_PROGRESS):
                        s.status = StepStatus.SKIPPED
                        s.completed_at = _now
                        s.result_summary = (
                            (s.result_summary or "")
                            + "\n[abandoned: chat cleared]"
                        ).strip()
                plan.status = "completed"
                plan.completed_at = _now
            self._current_plan = None
            # Reset watchdog wake counts so future plans get fresh quota.
            try:
                self._stuck_wake_counts = {}
            except Exception:
                pass
            self._log("status", {"action": "cleared"})

    def delete_message_at(self, ts: float, role: str,
                          content_prefix: str = "") -> dict:
        """Delete a single chat bubble by timestamp + role match.

        The chat UI fetches its history from ``self.events`` (one event
        per bubble) and the LLM history lives in
        ``_messages_by_context[active_ctx]``. Both must be removed so
        the bubble disappears AND the next turn doesn't replay it back
        into the model.

        Match strategy:
          • ``events``: find the entry where ``kind == "message"`` and
            ``timestamp ≈ ts`` (±0.5s) and ``data.role == role``. If a
            ``content_prefix`` is given, it must also match the first
            chars (case-sensitive) — disambiguates duplicate-content
            bubbles posted in the same second.
          • ``messages``: in the active context bucket, remove the
            FIRST entry with the same role + content prefix. We don't
            try to keep events and messages in lock-step indices —
            that's brittle — content+role is enough because identical
            consecutive messages are rare and removing one of them is
            the user's intent anyway.

        Returns ``{"event_removed": bool, "message_removed": bool}``.
        Caller (the HTTP handler) decides whether to consider the call
        a success — usually success = at least one of them was found.
        """
        out = {"event_removed": False, "message_removed": False}
        with self._lock:
            # 1) Drop the event the chat UI is rendering.
            for i, ev in enumerate(self.events):
                if ev.kind != "message":
                    continue
                if abs(ev.timestamp - ts) > 0.5:
                    continue
                if (ev.data or {}).get("role") != role:
                    continue
                if content_prefix:
                    ec = str((ev.data or {}).get("content", ""))
                    if not ec.startswith(content_prefix[:200]):
                        continue
                self.events.pop(i)
                out["event_removed"] = True
                break

            # 2) Drop the matching LLM message in the active bucket.
            # Use the active context's bucket (the user is looking at
            # whichever context is foregrounded). Match by role +
            # content prefix; if multiple match we drop the first.
            try:
                ctx = self._active_context_id or "solo"
                bucket = self._messages_by_context.get(ctx)
                if bucket is None:
                    bucket = self.messages  # fallback
                for j, msg in enumerate(bucket):
                    if msg.get("role") != role:
                        continue
                    mc = msg.get("content", "")
                    if not isinstance(mc, str):
                        # Multimodal content: skip prefix check, match by role.
                        # Better than no match for image-only user turns.
                        bucket.pop(j)
                        out["message_removed"] = True
                        break
                    if content_prefix and not mc.startswith(
                            content_prefix[:200]):
                        continue
                    bucket.pop(j)
                    out["message_removed"] = True
                    break
            except Exception:
                # Defensive: don't let bucket weirdness break event delete.
                pass

            if out["event_removed"] or out["message_removed"]:
                self._log("status", {"action": "delete_message",
                                     "role": role,
                                     "ts": ts})
        return out

    def update_profile(self, **kwargs):
        """Update profile fields dynamically."""
        for k, v in kwargs.items():
            if hasattr(self.profile, k):
                setattr(self.profile, k, v)

    # ---- task management ----

    def add_task(self, title: str, description: str = "",
                 priority: int = 0, parent_id: str = "",
                 assigned_by: str = "user",
                 source: str = "admin",
                 source_agent_id: str = "",
                 deadline: float = 0.0,
                 tags: list[str] | None = None,
                 recurrence: str = "once",
                 recurrence_spec: str = "") -> AgentTask:
        task = AgentTask(
            title=title, description=description,
            priority=priority, parent_id=parent_id,
            assigned_by=assigned_by,
            source=source,
            source_agent_id=source_agent_id,
            deadline=deadline,
            tags=tags or [],
            recurrence=recurrence or "once",
            recurrence_spec=recurrence_spec or "",
        )
        # Compute next_run_at for recurring tasks
        if task.recurrence != "once":
            try:
                from .scheduler import compute_next_run
                task.next_run_at = compute_next_run(
                    task.recurrence, task.recurrence_spec, time.time()) or 0.0
            except Exception:
                task.next_run_at = 0.0
        self.tasks.append(task)
        self._log("task", {"action": "created", "task_id": task.id,
                           "title": title, "source": source,
                           "deadline": task.deadline_str or "none"})
        logger.info("TASK added: agent=%s task=%s title='%s' source=%s deadline=%s",
                    self.name, task.id, title, source, task.deadline_str or "none")
        # Inject task notification into agent context so it knows about the task
        self._notify_new_task(task)
        return task

    def _notify_new_task(self, task: AgentTask):
        """Inject a system-level notification so the agent is aware of the new task."""
        if task.notified:
            return
        deadline_info = f" | Deadline: {task.deadline_str}" if task.deadline else ""
        priority_label = {0: "Normal", 1: "High", 2: "Urgent"}.get(task.priority, "Normal")
        source_label = f"{task.source}"
        if task.source_agent_id:
            source_label += f" (agent: {task.source_agent_id})"
        notification = (
            f"[TASK ASSIGNED] ID: {task.id}\n"
            f"  Title: {task.title}\n"
            f"  Description: {task.description or 'N/A'}\n"
            f"  Priority: {priority_label}{deadline_info}\n"
            f"  Source: {source_label}\n"
            f"  Status: {task.status.value}\n"
            f"  Please acknowledge and work on this task."
        )
        # Add as a system message so the agent sees it in its next turn
        self.messages.append({"role": "user", "content": notification})
        task.notified = True
        self._log("task", {"action": "notified", "task_id": task.id})
        logger.info("TASK notified: agent=%s task=%s", self.name, task.id)

    def get_pending_tasks_summary(self) -> str:
        """Generate a summary of pending tasks for injection into agent context."""
        pending = [t for t in self.tasks if t.status in (TaskStatus.TODO, TaskStatus.IN_PROGRESS)]
        if not pending:
            return ""
        lines = ["[PENDING TASKS]"]
        for t in pending:
            dl = f" (Due: {t.deadline_str})" if t.deadline else ""
            overdue = " ⚠️OVERDUE" if t.is_overdue else ""
            pri = {0: "", 1: " [HIGH]", 2: " [URGENT]"}.get(t.priority, "")
            lines.append(f"  - [{t.status.value}]{pri} {t.title}{dl}{overdue}")
        return "\n".join(lines)

    # ── Self-growth closed loop ───────────────────────────────────────────
    def enqueue_growth_task(self, learning_goal: str = "",
                            knowledge_gap: str = "",
                            title: str = "") -> AgentTask:
        """Create a low-priority self-growth task.

        Growth tasks have priority -1 (background). They only run when the
        agent has no pending normal/high/urgent tasks AND is not busy
        chatting. They use the agent's learning_provider/learning_model
        (cheap/local) instead of the main provider/model.
        """
        task_title = title or (
            f"自我成长: {learning_goal}" if learning_goal else "自我成长 — 经验积累"
        )
        task = AgentTask(
            title=task_title,
            description=knowledge_gap or learning_goal or "Background self-improvement run.",
            priority=-1,
            assigned_by="system",
            source="system",
            tags=["growth"],
            provider=self.learning_provider or "",
            model=self.learning_model or "",
            learning_goal=learning_goal or "",
            knowledge_gap=knowledge_gap or "",
        )
        self.tasks.append(task)
        self._log("task", {
            "action": "growth_enqueued",
            "task_id": task.id,
            "title": task_title,
            "learning_provider": self.learning_provider or "(default)",
            "learning_model": self.learning_model or "(default)",
        })
        logger.info("GROWTH task enqueued: agent=%s task=%s",
                    self.name, task.id)
        return task

    def _has_higher_priority_pending(self) -> bool:
        """True if there are any non-growth tasks still pending or running."""
        for t in self.tasks:
            if t.status in (TaskStatus.TODO, TaskStatus.IN_PROGRESS) and t.priority >= 0:
                return True
        return False

    def _next_growth_task(self) -> AgentTask | None:
        for t in self.tasks:
            if t.status == TaskStatus.TODO and t.priority < 0:
                return t
        return None

    def _derive_growth_gap(self) -> dict | None:
        """Introspect recent activity to synthesize a new growth topic.

        Strategy (cheap, no-LLM):
        1. If there are recent failed tool calls in history_log, target the
           most frequent failing tool.
        2. Otherwise pick a topic from the agent's role/expertise that
           hasn't been studied in the last 24h (tracked via _learning_topics_seen).
        3. Throttle: only synthesize a new topic at most once every 30 minutes
           per agent to avoid spamming the learning model.
        """
        now = time.time()
        # Throttle synthesis
        last_synth = getattr(self, "_last_growth_synth", 0.0) or 0.0
        if (now - last_synth) < 1800:
            return None
        self._last_growth_synth = now

        # Scan recent history for failures
        try:
            recent = list(self.history_log.events[-40:])
        except Exception:
            recent = []
        failures = []
        for e in recent:
            title = (getattr(e, "title", "") or "").lower()
            detail = (getattr(e, "detail", "") or "")
            if "fail" in title or "error" in title or "denied" in title:
                failures.append(detail[:200])
        if failures:
            return {
                "goal": "排查最近的执行失败并提炼改进经验",
                "gap": "最近的失败样本：\n" + "\n".join("- " + f for f in failures[:5]),
            }

        # Fallback: role/expertise-based self-study
        topics_seen = getattr(self, "_growth_topics_seen", None)
        if topics_seen is None:
            topics_seen = {}
            self._growth_topics_seen = topics_seen
        candidates = []
        role = (self.role or "general").lower()
        expertise = list(getattr(self.profile, "expertise", []) or [])
        base_topics = {
            "coder": ["代码重构技巧", "常见 bug 模式", "测试策略", "性能优化"],
            "reviewer": ["代码审查清单", "安全漏洞模式", "可读性准则"],
            "cto": ["系统设计权衡", "技术选型评估", "团队协作流程"],
            "pm": ["需求拆解方法", "风险管理", "里程碑规划"],
            "general": ["沟通技巧", "问题分解方法", "学习新工具的流程"],
        }
        candidates = list(expertise) + base_topics.get(role, base_topics["general"])
        for topic in candidates:
            last_ts = topics_seen.get(topic, 0.0)
            if (now - last_ts) > 86400:  # not in last 24h
                topics_seen[topic] = now
                return {
                    "goal": f"自我学习 — {topic}",
                    "gap": f"围绕角色 '{self.role}' 深化对「{topic}」的理解。请总结关键要点并提炼可复用的经验条目。",
                }
        return None

    def tick_growth(self, min_interval: float = 60.0) -> dict | None:
        """Heartbeat hook: pick a growth task and run it if truly idle.

        Idle = no higher-priority pending tasks AND no active chat. Throttled
        by ``min_interval`` seconds between runs per agent. Returns the task's
        result dict on success, or None if nothing happened.

        Opt-in gate: does nothing when the agent's self-improvement engine
        hasn't been explicitly enabled. Previously ``_derive_growth_gap``
        could synthesize a background task even on agents the admin never
        opted-in, which surprised users (they'd see unexpected
        ``growth_done`` log lines with no knob they turned on).
        """
        si = getattr(self, "self_improvement", None)
        if si is None or not getattr(si, "enabled", False):
            return None

        now = time.time()
        if (now - self._last_growth_tick) < min_interval:
            return None
        # Don't interrupt user chat or running work
        if self.status == AgentStatus.BUSY:
            return None
        if self._has_higher_priority_pending():
            return None
        task = self._next_growth_task()
        # ── Closed-loop #1: drain SelfImprovementEngine._learning_queue into
        # growth tasks. trigger_active_learning() queues here when the agent
        # was busy at request time; we now unblock them. Skip entries whose
        # goal is empty — they'd just pollute the board. ──
        if task is None and self.self_improvement is not None:
            queue = getattr(self.self_improvement, "_learning_queue", None) or []
            while queue and task is None:
                nxt = queue.pop(0)
                _g = (nxt.get("learning_goal") or "").strip()
                if not _g:
                    continue  # drop noise
                task = self.enqueue_growth_task(
                    learning_goal=_g,
                    knowledge_gap=(nxt.get("knowledge_gap") or "").strip(),
                )
        # ── Closed-loop #2: if still nothing, synthesize a background
        # introspection task from recent failures / unreviewed experience.
        # We ONLY enqueue when derive returns a concrete goal; no more empty
        # "自我反思与经验沉淀" fallback. ──
        if task is None:
            gap = self._derive_growth_gap()
            if gap and (gap.get("goal") or "").strip():
                task = self.enqueue_growth_task(
                    learning_goal=gap["goal"].strip(),
                    knowledge_gap=(gap.get("gap") or "").strip(),
                )
        if task is None:
            return None
        self._last_growth_tick = now
        # Mark in-progress and run
        task.status = TaskStatus.IN_PROGRESS
        task.updated_at = now
        prev_task = self._current_task
        self._current_task = task  # routes provider/model via resolver
        try:
            result = self.trigger_active_learning(
                learning_goal=task.learning_goal,
                knowledge_gap=task.knowledge_gap,
            )
            task.status = TaskStatus.DONE
            task.result = (
                result.get("message", "")
                or result.get("status", "")
                or "growth tick complete"
            )[:500] if isinstance(result, dict) else str(result)[:500]
            task.updated_at = time.time()
            self._log("task", {
                "action": "growth_done",
                "task_id": task.id,
            })
            return result if isinstance(result, dict) else {"result": str(result)}
        except Exception as e:
            task.status = TaskStatus.BLOCKED
            task.result = f"growth tick failed: {e}"
            logger.error("GROWTH tick failed for agent %s: %s", self.name, e)
            return {"error": str(e)}
        finally:
            self._current_task = prev_task

    def update_task(self, task_id: str, **kwargs) -> AgentTask | None:
        for t in self.tasks:
            if t.id == task_id:
                _prev_status = t.status
                for k, v in kwargs.items():
                    if k == "status":
                        t.status = TaskStatus(v) if isinstance(v, str) else v
                    elif hasattr(t, k):
                        setattr(t, k, v)
                t.updated_at = time.time()
                self._log("task", {"action": "updated", "task_id": task_id,
                                   "changes": list(kwargs.keys())})
                # Auto-post progress to meeting if this task originated from one
                self._sync_meeting_progress(t)
                # ── Success rate counters (P1) ──
                # Bump on terminal status transitions only. DONE → success;
                # BLOCKED / CANCELLED → fail. Used by long_task.auto_assign
                # to prefer historically-reliable agents when multiple
                # agents match the same role_hint.
                if t.status != _prev_status:
                    if t.status == TaskStatus.DONE:
                        self.role_success_count = int(getattr(self, "role_success_count", 0) or 0) + 1
                        self.role_last_success_at = time.time()
                    elif t.status in (TaskStatus.BLOCKED, TaskStatus.CANCELLED):
                        self.role_fail_count = int(getattr(self, "role_fail_count", 0) or 0) + 1

                # L3 extraction trigger — fire on transition to DONE only.
                # Wrapped to never break the task update on extraction errors.
                if t.status == TaskStatus.DONE and _prev_status != TaskStatus.DONE:
                    try:
                        from .core import l3_extractor as _l3
                        llm_call = self._make_summary_llm_call()
                        if llm_call:
                            _lang = getattr(self.profile, "language", "auto") or "auto"
                            _l3.extract_on_task_done(
                                agent_id=self.id,
                                task_title=t.title or "",
                                task_description=t.description or "",
                                task_result=t.result or "",
                                recent_messages=self.messages[-12:],
                                llm_call=llm_call,
                                lang=_lang,
                            )
                    except Exception as _l3_err:  # noqa: BLE001
                        logger.debug("L3 task-done hook failed: %s", _l3_err)
                return t
        return None

    def _sync_meeting_progress(self, task: AgentTask) -> None:
        """If a task was spawned from a meeting assignment, post status back."""
        if not task.source_meeting_id or not task.source_assignment_id:
            return
        try:
            from .hub import get_hub as _get_hub
            hub = _get_hub()
            reg = getattr(hub, "meeting_registry", None) if hub else None
            if reg is None:
                return
            m = reg.get(task.source_meeting_id)
            if not m:
                return
            status_str = task.status.value if isinstance(task.status, TaskStatus) else str(task.status)
            m.post_progress(
                agent_id=self.id,
                agent_name=self.name,
                assignment_id=task.source_assignment_id,
                status=status_str,
                detail=task.result[:200] if task.result else "",
            )
            reg.save()
        except Exception as e:
            logger.debug("meeting progress sync failed: %s", e)

    def get_task(self, task_id: str) -> AgentTask | None:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None

    def list_tasks(self, status: str = "") -> list[AgentTask]:
        if status:
            return [t for t in self.tasks if t.status.value == status]
        return list(self.tasks)

    def remove_task(self, task_id: str) -> bool:
        for i, t in enumerate(self.tasks):
            if t.id == task_id:
                self.tasks.pop(i)
                self._log("task", {"action": "removed", "task_id": task_id})
                return True
        return False

    # ══════════════════════════════════════════════════════════════════
    # RolePresetV2 — QualityGate retry & KPI/Experience recording
    # (Phase C.1 + C.2)
    # ══════════════════════════════════════════════════════════════════

    def _run_quality_gate_with_retry(
        self,
        output_text: str,
        user_text: str,
        tools_used: list[str],
        _emit: Any = None,
    ) -> str:
        """Phase C.1: hard-retry ≤3 times with feedback, then soft-warning fallback.

        Returns the (possibly improved) output text. Never raises.
        """
        from .quality_gate import get_quality_gate
        rules = list(getattr(self.profile, "quality_rules", []) or [])
        if not rules:
            return output_text

        hard_retries = 3
        soft_fallback = True
        try:
            from .role_preset_registry import get_registry as _get_reg
            preset = _get_reg().get(getattr(self.profile, "role_preset_id", ""))
            if preset is not None:
                hard_retries = int(getattr(preset, "quality_hard_retries", 3) or 3)
                soft_fallback = bool(getattr(preset, "quality_soft_fallback", True))
        except Exception:
            pass

        gate = get_quality_gate()
        current = output_text
        ctx = {"tools_used": tools_used or [], "user_text": user_text or ""}
        failed_rule_counts: dict[str, int] = {}
        exhausted_rule_ids: set[str] = set()
        result = None
        # Same budgets as agent_execution._run_quality_gate_with_retry —
        # 3-per-rule + 6-total. Tunable via env so ops can dial based on
        # observed retry waste.
        import os as _os
        _MAX_PER_RULE = int(
            _os.environ.get("TUDOU_QUALITY_MAX_PER_RULE", "3") or 3)
        _MAX_TOTAL = int(
            _os.environ.get("TUDOU_QUALITY_MAX_TOTAL_FAILS", "6") or 6)

        for attempt in range(hard_retries + 1):
            result = gate.check(current, rules, context=ctx)
            self._log("quality_check", {
                "attempt": attempt,
                "passed": result.passed,
                "failing_rules": result.failing_rules,
            })
            if result.passed:
                return current
            if attempt >= hard_retries:
                break

            for rid in result.failing_rules:
                failed_rule_counts[rid] = failed_rule_counts.get(rid, 0) + 1
                if failed_rule_counts[rid] >= _MAX_PER_RULE:
                    exhausted_rule_ids.add(rid)
            if sum(failed_rule_counts.values()) >= _MAX_TOTAL:
                logger.info(
                    "Agent %s: quality-retry total-fail cap hit (%d ≥ %d)",
                    self.id[:8],
                    sum(failed_rule_counts.values()), _MAX_TOTAL)
                break

            feedback = gate.build_feedback_prompt(
                result, current, rules,
                prior_feedback_ids=exhausted_rule_ids,
            )
            self._log("quality_retry", {
                "attempt": attempt + 1,
                "feedback_len": len(feedback),
            })

            try:
                _prov, _mdl = self._resolve_effective_provider_model()
                retry_messages = [
                    {"role": "system", "content": "你需要严格按反馈改进上一轮回答，并输出完整的最终答案。"},
                    {"role": "user", "content": user_text or ""},
                    {"role": "assistant", "content": current},
                    {"role": "user", "content": feedback},
                ]
                resp = llm.chat_no_stream(
                    retry_messages, tools=None,
                    provider=_prov, model=_mdl,
                )
                new_content = (resp or {}).get("message", {}).get("content", "") or ""
                if new_content.strip():
                    current = new_content
            except Exception as e:
                logger.debug("QualityGate retry LLM call failed: %s", e)
                break

        # Exhausted retries → soft fallback. Agent turn always continues —
        # return last-best output so caller can deliver it; UI shows a
        # quality_warning banner so the user sees the failure.
        if soft_fallback and result is not None and not result.passed:
            try:
                _failing = result.failing_rules
                _total = sum(failed_rule_counts.values())
                evt = AgentEvent(time.time(), "quality_warning", {
                    "failing_rules": _failing,
                    "exhausted_rules": sorted(exhausted_rule_ids),
                    "total_fails": _total,
                    "message": (
                        f"质量检查未通过 / Quality check failed after "
                        f"{_total} retry attempts. 返回最后一版输出，"
                        f"agent 继续执行 / returning last-best output, "
                        f"agent continues."
                        + (f" 未通过规则 / failing: {', '.join(_failing)}"
                           if _failing else "")
                    ),
                })
                self._log(evt.kind, evt.data)
                if _emit is not None:
                    try:
                        _emit(evt)
                    except Exception:
                        pass
            except Exception:
                pass
        # Defensive: if retries produced empty content, fall back to original
        # so downstream chat turn always has something non-empty.
        if not (current or "").strip():
            current = output_text
        return current

    def _record_kpis_and_experience(
        self,
        output_text: str,
        user_text: str,
        tools_used: list[str],
    ) -> None:
        """Phase C.2: record KPI values to SQLite + turn failures into Experience.

        All failures are swallowed — KPI/learning is best-effort.
        """
        try:
            from .kpi_recorder import get_kpi_recorder
        except Exception as e:
            logger.debug("kpi_recorder unavailable: %s", e)
            return

        role_id = getattr(self.profile, "role_preset_id", "") or self.profile.role
        kpi_defs = list(getattr(self.profile, "kpi_definitions", []) or [])

        qc_events = [e for e in self.events[-20:] if e.kind == "quality_check"]
        retry_events = [e for e in self.events[-20:] if e.kind == "quality_retry"]
        last_qc = qc_events[-1] if qc_events else None
        first_qc = qc_events[0] if qc_events else None
        passed = bool(last_qc.data.get("passed", True)) if last_qc else True
        retries_used = len(retry_events)
        first_pass = (
            1.0 if (first_qc and first_qc.data.get("passed")) else 0.0
        ) if first_qc else 1.0

        recorder = get_kpi_recorder()
        for kpi in kpi_defs:
            try:
                if isinstance(kpi, dict):
                    kpi_name = kpi.get("key") or kpi.get("name") or ""
                else:
                    kpi_name = getattr(kpi, "key", "") or getattr(kpi, "name", "")
                if not kpi_name:
                    continue
                if kpi_name in ("first_pass_rate", "first_pass"):
                    value = first_pass
                elif kpi_name in ("retries_used", "retry_count"):
                    value = float(retries_used)
                elif kpi_name in ("summary_completeness", "completeness", "prd_completeness", "design_completeness"):
                    value = 1.0 if passed else 0.6
                elif kpi_name in ("action_extraction_rate", "action_items"):
                    value = 1.0 if (passed and ("action" in output_text.lower() or "待办" in output_text)) else 0.5
                else:
                    value = 1.0 if passed else 0.0
                recorder.record(
                    role=role_id,
                    agent_id=self.id,
                    key=kpi_name,
                    value=value,
                    meta={"retries": retries_used, "passed": passed},
                )
            except Exception as e:
                logger.debug("KPI record skipped (%s): %s", kpi, e)

        try:
            if not passed and last_qc is not None:
                from .experience_library import get_experience_library, Experience
                lib = get_experience_library()
                failing = last_qc.data.get("failing_rules", []) or []
                exp = Experience(
                    exp_type="retrospective",
                    source="quality_gate",
                    scene=f"用户请求类似：{(user_text or '')[:80]}",
                    core_knowledge=f"质量检查失败：{', '.join(failing) or '未知规则'}",
                    action_rules=[
                        f"针对规则 '{r}'，在初次输出前主动满足其要求"
                        for r in failing[:3]
                    ] or ["初次输出前对照本角色 quality_rules 逐条自检"],
                    taboo_rules=["不要在不满足硬性规则的情况下直接提交输出"],
                    priority="high",
                    tags=list(failing) + ["quality_failure"],
                )
                lib.add_experience(role=role_id, exp=exp)
                self._log("experience_added", {
                    "role": role_id,
                    "priority": "high",
                    "tags": list(failing),
                })
        except Exception as e:
            logger.debug("Experience add skipped: %s", e)


# ---------------------------------------------------------------------------
# ChatTask — background chat execution with progress tracking
# ---------------------------------------------------------------------------

class ChatTaskStatus(str, Enum):
    QUEUED = "queued"
    THINKING = "thinking"
    STREAMING = "streaming"
    TOOL_EXEC = "tool_exec"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass
class ChatTask:
    """A background chat task that runs independently of the HTTP connection."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    agent_id: str = ""
    user_message: str = ""
    status: ChatTaskStatus = ChatTaskStatus.QUEUED
    progress: int = 0           # 0-100
    phase: str = ""             # human-readable phase description
    result: str = ""            # final assistant text
    error: str = ""
    events: list = field(default_factory=list)   # SSE event dicts
    _event_cursor: int = 0      # for clients to track what they've read
    aborted: bool = False       # abort flag checked by chat loop
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def abort(self):
        """Signal the task to stop."""
        self.aborted = True
        self.set_status(ChatTaskStatus.ABORTED, "Aborted by user", -1)
        self.push_event({"type": "error", "content": "Task aborted by user"})
        self.push_event({"type": "done"})

    def push_event(self, evt: dict):
        """Thread-safe event push."""
        with self._lock:
            self.events.append(evt)
            self.updated_at = time.time()

    def get_events_since(self, cursor: int) -> tuple[list[dict], int]:
        """Return events since cursor and new cursor position."""
        with self._lock:
            new_events = self.events[cursor:]
            return new_events, len(self.events)

    def set_status(self, status: ChatTaskStatus, phase: str = "",
                   progress: int = -1):
        with self._lock:
            self.status = status
            if phase:
                self.phase = phase
            if progress >= 0:
                self.progress = min(progress, 100)
            self.updated_at = time.time()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "status": self.status.value,
            "progress": self.progress,
            "phase": self.phase,
            "result": self.result[:500] if self.result else "",
            "error": self.error,
            "event_count": len(self.events),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }



# ChatTaskManager, get_chat_task_manager — imported from app.chat_task (line 39-42).
# DO NOT duplicate here; the router must share the same singleton.


def _truncate_dict(d: dict, max_len: int = 200) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, str) and len(v) > max_len:
            out[k] = v[:max_len] + "..."
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Default organizational departments — used by the portal UI's
# department selector. Users can still enter a custom string.
# ---------------------------------------------------------------------------

DEFAULT_DEPARTMENTS: list[str] = [
    "管理层",
    "研发",
    "产品",
    "设计",
    "运营",
    "市场",
    "销售",
    "客服",
    "数据",
    "财务",
    "人事",
    "法务",
]


# ---------------------------------------------------------------------------
# Role presets — now with rich profile defaults
# ---------------------------------------------------------------------------

ROLE_PRESETS: dict[str, dict] = {
    "general": {
        "name": "Claw",
        "system_prompt": "You are a general-purpose AI programming assistant.",
        "profile": AgentProfile(
            personality="helpful",
            communication_style="technical",
            expertise=["programming", "debugging", "system administration"],
            skills=["code_writing", "code_review", "debugging", "documentation"],
        ),
    },
    "coder": {
        "name": "Coder",
        "system_prompt": (
            "You are an expert software engineer. Write clean, well-tested code. "
            "Always read existing code before making changes. Prefer small, focused edits."
        ),
        "profile": AgentProfile(
            personality="precise",
            communication_style="technical",
            expertise=["python", "javascript", "rust", "go", "algorithms", "data structures"],
            skills=["code_writing", "refactoring", "testing", "optimization"],
            auto_approve_tools=["write_file", "edit_file"],
        ),
    },
    "reviewer": {
        "name": "Reviewer",
        "system_prompt": (
            "You are a code reviewer. Read the code carefully, find bugs, suggest improvements. "
            "Be constructive and specific. Check for security issues, performance, and readability."
        ),
        "profile": AgentProfile(
            personality="strict",
            communication_style="detailed",
            expertise=["code_quality", "security", "performance", "best_practices"],
            skills=["code_review", "security_audit", "performance_analysis"],
            allowed_tools=["read_file", "search_files", "glob_files", "bash", "web_search"],
            denied_tools=["write_file", "edit_file"],
        ),
    },
    "researcher": {
        "name": "Researcher",
        "system_prompt": (
            "You are a research assistant. Search the web, read documentation, and gather "
            "information. Summarise findings clearly with sources."
        ),
        "profile": AgentProfile(
            personality="curious",
            communication_style="educational",
            expertise=["research", "documentation", "technical_writing"],
            skills=["web_research", "summarization", "comparison", "analysis"],
            allowed_tools=["read_file", "search_files", "glob_files",
                           "web_search", "web_fetch"],
            denied_tools=["write_file", "edit_file", "bash"],
        ),
    },
    "architect": {
        "name": "Architect",
        "system_prompt": (
            "You are a software architect. Design systems, plan implementations, and make "
            "high-level technical decisions. Consider trade-offs, scalability, and maintainability."
        ),
        "profile": AgentProfile(
            personality="thoughtful",
            communication_style="detailed",
            expertise=["system_design", "architecture", "scalability",
                        "microservices", "database_design"],
            skills=["system_design", "technical_planning", "trade-off_analysis",
                    "documentation"],
        ),
    },
    "devops": {
        "name": "DevOps",
        "system_prompt": (
            "You are a DevOps engineer. Help with CI/CD, Docker, deployment, monitoring, "
            "and infrastructure. Use bash commands to inspect and configure systems."
        ),
        "profile": AgentProfile(
            personality="pragmatic",
            communication_style="brief",
            expertise=["docker", "kubernetes", "ci_cd", "monitoring",
                        "linux", "networking", "cloud"],
            skills=["deployment", "monitoring", "troubleshooting",
                    "infrastructure_as_code"],
            auto_approve_tools=["bash"],
        ),
    },
}


def create_agent(
    name: str = "",
    role: str = "general",
    model: str = "",
    provider: str = "",
    working_dir: str = "",
    system_prompt: str = "",
    node_id: str = "local",
    profile_overrides: dict | None = None,
    parent_id: str = "",
    priority_level: int = 3,
    role_title: str = "",
    department: str = "",
) -> Agent:
    """Create a new agent from a role preset, with optional profile overrides.

    priority_level: 1=CXO (highest), 2=PM, 3=Team Member (default)
    role_title: e.g. "CXO", "PM", "Developer"
    """
    logger.info("create_agent: name=%s role=%s model=%s provider=%s node_id=%s priority=%s",
                name, role, model, provider, node_id, priority_level)
    preset = ROLE_PRESETS.get(role, ROLE_PRESETS["general"])
    profile = AgentProfile(
        **preset.get("profile", AgentProfile()).to_dict()
    ) if isinstance(preset.get("profile"), AgentProfile) else AgentProfile()

    # ── RolePresetV2 application ───────────────────────────────────────
    # If preset carries a _v2_preset marker, populate V2-specific fields on
    # the profile. This runs BEFORE profile_overrides so callers can still
    # override individual fields.
    v2_preset = preset.get("_v2_preset")
    if v2_preset is not None:
        try:
            profile.role_preset_id = v2_preset.role_id
            profile.role_preset_version = 2
            profile.llm_tier = v2_preset.llm_tier or ""
            profile.llm_tier_overrides = dict(v2_preset.llm_tier_overrides or {})
            profile.sop_template_id = v2_preset.sop_template_id or ""
            profile.quality_rules = [
                r.to_dict() if hasattr(r, "to_dict") else dict(r)
                for r in (v2_preset.quality_rules or [])
            ]
            profile.output_contract = dict(v2_preset.output_contract or {})
            profile.input_contract = dict(v2_preset.input_contract or {})
            profile.kpi_definitions = [
                k.to_dict() if hasattr(k, "to_dict") else dict(k)
                for k in (v2_preset.kpi_definitions or [])
            ]
            # Tool lists from V2 override profile defaults (role-level policy)
            if v2_preset.allowed_tools:
                profile.allowed_tools = list(v2_preset.allowed_tools)
            if v2_preset.denied_tools:
                profile.denied_tools = list(v2_preset.denied_tools)
            if v2_preset.auto_approve_tools:
                profile.auto_approve_tools = list(v2_preset.auto_approve_tools)
            # RAG namespaces merged into rag_collection_ids
            if v2_preset.rag_namespaces:
                existing_rag = set(profile.rag_collection_ids or [])
                for ns in v2_preset.rag_namespaces:
                    if ns not in existing_rag:
                        profile.rag_collection_ids.append(ns)
            logger.info("RolePresetV2 applied to new agent: role=%s tier=%s sop=%s",
                        v2_preset.role_id, v2_preset.llm_tier, v2_preset.sop_template_id)
        except Exception as _v2err:
            logger.warning("RolePresetV2 apply failed for role %s: %s", role, _v2err)

    # Apply overrides
    if profile_overrides:
        for k, v in profile_overrides.items():
            if hasattr(profile, k) and v is not None:
                setattr(profile, k, v)

    agent = Agent(
        name=name or preset["name"],
        role=role,
        model=model,
        provider=provider,
        working_dir=working_dir,
        system_prompt=system_prompt or preset["system_prompt"],
        profile=profile,
        node_id=node_id,
        parent_id=parent_id,
        priority_level=priority_level,
        role_title=role_title,
        department=department,
    )
    # If no working_dir provided, default to the agent's own workspace folder.
    # This ensures generated reports/files land under agents/{id}/workspace/
    # instead of leaking into the node's cwd or root.
    if not working_dir:
        try:
            ws = agent._ensure_workspace_layout()
            agent.working_dir = str(ws)
        except Exception:
            pass
    else:
        # User gave an explicit working_dir, still seed the layout (no-op if exists)
        try:
            agent._ensure_workspace_layout()
        except Exception:
            pass

    # ── RolePresetV2 Knowledge binding (Phase B.2) ──────────────────────
    # Bind few-shot skills to agent.bound_prompt_packs so they auto-inject
    # into the chat context via PromptPackRegistry.
    if v2_preset is not None and getattr(v2_preset, "few_shot_skill_ids", None):
        try:
            existing = set(agent.bound_prompt_packs or [])
            for sid in v2_preset.few_shot_skill_ids:
                if sid and sid not in existing:
                    agent.bound_prompt_packs.append(sid)
                    existing.add(sid)
            logger.info("RolePresetV2 bound %d few-shot skills to agent %s",
                        len(v2_preset.few_shot_skill_ids), agent.id[:8])
        except Exception as _sk_err:
            logger.warning("RolePresetV2 skill binding failed: %s", _sk_err)

    # ── RolePresetV2 MCP binding (Phase B.1) ────────────────────────────
    # If this role has default_mcp_bindings declared in YAML, auto-bind
    # each MCP to this agent via MCPManager.bind_mcp_to_agent. Unknown
    # MCP IDs are logged as warnings but don't block agent creation.
    if v2_preset is not None and getattr(v2_preset, "default_mcp_bindings", None):
        try:
            from .mcp.manager import get_mcp_manager as _get_mcp_manager
            mcp_mgr = _get_mcp_manager()
            globals_mcps = mcp_mgr.list_global_mcps()
            node_mcps = mcp_mgr.get_node_mcp_config(agent.node_id).available_mcps
            bound, skipped = [], []
            for mcp_id in v2_preset.default_mcp_bindings:
                if mcp_id not in globals_mcps and mcp_id not in node_mcps:
                    skipped.append(mcp_id)
                    continue
                ok = mcp_mgr.bind_mcp_to_agent(agent.node_id, agent.id, mcp_id)
                if ok:
                    bound.append(mcp_id)
                else:
                    skipped.append(mcp_id)
            # Sync bindings into agent.profile.mcp_servers so tools.py can see them
            try:
                mcp_mgr.sync_agent_mcps(agent)
            except Exception as _sync_err:
                logger.debug("sync_agent_mcps skipped: %s", _sync_err)
            if bound:
                logger.info("RolePresetV2 MCP auto-bind [%s]: %d bound (%s), %d skipped (%s)",
                            agent.id[:8], len(bound), ",".join(bound),
                            len(skipped), ",".join(skipped) if skipped else "-")
            elif skipped:
                logger.warning("RolePresetV2 MCP auto-bind [%s]: no MCPs bound; %d skipped (%s) — "
                               "check MCP install status", agent.id[:8], len(skipped), ",".join(skipped))
        except Exception as _mcp_err:
            logger.warning("RolePresetV2 MCP auto-bind failed: %s", _mcp_err)

    logger.info("create_agent OK: id=%s name=%s node=%s workspace=%s",
                agent.id, agent.name, agent.node_id, agent.working_dir)
    return agent
