from __future__ import annotations
"""
Unified LLM calling interface with dynamic provider registry.

Supports two standard chat protocols:
  - openai:  OpenAI-compatible  (POST /v1/chat/completions) — covers OpenAI, Ollama,
             vLLM, LM Studio, Unsloth, and any other OpenAI-compatible provider.
  - claude:  Anthropic Messages API

Ollama is treated as an OpenAI-compatible provider (its /v1 endpoint returns standard
tool_calls format).  Model-specific directives (e.g. Qwen3 /no_think) are handled in
_apply_model_directives(), not in protocol-specific code.

Providers are stored in ~/.tudou_claw/providers.json and can be managed at runtime
via the ProviderRegistry.  Each provider has:
    id, name, kind ("ollama"|"openai"|"claude"), base_url, api_key, enabled

Provider Fallback Chains:
  Providers can specify a list of fallback providers via the `fallback_providers` field.
  When a provider fails with a retryable error (429 rate limit, connection error, timeout),
  the system automatically tries the next provider in the chain before raising an exception.

  Example:
    registry.update("primary", fallback_providers=["secondary", "tertiary"])
    # Now when "primary" fails (429, timeout, connection error), it tries "secondary",
    # then "tertiary" before finally raising an error.

  The chat() and chat_no_stream() functions automatically use the configured fallback chain.
"""
import json
import logging
import os
import re
import threading
import time
import uuid
from collections import deque
import yaml
from pathlib import Path
from typing import Generator

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import copy

logger = logging.getLogger("tudou.llm")
token_logger = logging.getLogger("tudou.tokens")
# Per-call payload composition. Different logger so users can tune
# its level independently — set "tudou.tokens.breakdown" to DEBUG to
# see every call, INFO to only see anomalies (>15K total or >5K growth).
breakdown_logger = logging.getLogger("tudou.tokens.breakdown")


# ── Token breakdown: what's in the actual outbound payload ─────────────
# This measures `safe_messages` (post-sanitize, post-fold) — i.e. what
# the wire actually carries. Categories sum to total_chars.
_TOKEN_BREAKDOWN_HISTORY: dict = {}  # agent_id -> deque of last N totals
_BREAKDOWN_HISTORY_KEEP = 30
_BREAKDOWN_ANOMALY_TOTAL = 15000     # chars; ≈ 4K tokens
_BREAKDOWN_ANOMALY_GROWTH = 5000     # chars added vs previous call


def _measure_msg_chars(m: dict) -> int:
    """Total character count of one OpenAI-style message."""
    n = 0
    c = m.get("content")
    if isinstance(c, str):
        n += len(c)
    elif isinstance(c, list):
        for p in c:
            if isinstance(p, dict):
                n += len(p.get("text", "") or "")
                # multimodal parts: image_url etc — count the URL string
                if "image_url" in p:
                    iu = p["image_url"]
                    n += len(iu.get("url", "")) if isinstance(iu, dict) else len(str(iu))
            elif isinstance(p, str):
                n += len(p)
    for tc in (m.get("tool_calls") or []):
        if isinstance(tc, dict):
            fn = tc.get("function") or {}
            if isinstance(fn, dict):
                n += len(fn.get("name", "") or "")
                args = fn.get("arguments", "")
                n += len(args) if isinstance(args, str) else len(json.dumps(args, ensure_ascii=False, default=str))
    return n


def _log_payload_breakdown(safe_messages: list, tools: list | None,
                             agent_id: str = "", model: str = "") -> None:
    """Compute and log per-source token breakdown for one outbound payload."""
    try:
        sys_chars = 0
        user_chars = 0
        asst_text_chars = 0
        asst_tool_call_chars = 0
        tool_result_chars = 0
        n_user = n_asst_text = n_asst_tc = n_tool = 0
        total_tcs = 0

        for m in safe_messages or []:
            r = m.get("role", "")
            mc = _measure_msg_chars(m)
            if r == "system":
                sys_chars += mc
            elif r == "user":
                user_chars += mc
                n_user += 1
            elif r == "assistant":
                if m.get("tool_calls"):
                    asst_tool_call_chars += mc
                    n_asst_tc += 1
                    total_tcs += len(m.get("tool_calls") or [])
                else:
                    asst_text_chars += mc
                    n_asst_text += 1
            elif r == "tool":
                tool_result_chars += mc
                n_tool += 1

        history_chars = (user_chars + asst_text_chars
                         + asst_tool_call_chars + tool_result_chars)
        tools_chars = (
            len(json.dumps(tools, ensure_ascii=False, default=str))
            if tools else 0
        )
        total = sys_chars + history_chars + tools_chars

        # Compute delta vs previous call for this agent
        delta = 0
        if agent_id:
            history_deque = _TOKEN_BREAKDOWN_HISTORY.setdefault(agent_id, [])
            if history_deque:
                delta = total - history_deque[-1]
            history_deque.append(total)
            if len(history_deque) > _BREAKDOWN_HISTORY_KEEP:
                history_deque.pop(0)

        # Anomaly → INFO so users see it without enabling DEBUG
        anomaly = (total > _BREAKDOWN_ANOMALY_TOTAL
                   or delta > _BREAKDOWN_ANOMALY_GROWTH)
        log_method = breakdown_logger.info if anomaly else breakdown_logger.debug

        # When tools_chars is suspiciously large (≥ 25KB ≈ full registry),
        # capture the caller stack so we can pinpoint which code path is
        # passing the unfiltered tools list. Cheap — only runs on outliers.
        tool_caller_hint = ""
        if tools_chars >= 25000:
            try:
                import traceback
                # Skip 2 frames: this function + its caller (provider handler)
                stack = traceback.extract_stack(limit=10)[:-2]
                # Find the topmost frame inside app/ but outside llm.py /
                # provider modules — that's the bloat source.
                for frame in reversed(stack):
                    fn = frame.filename
                    if "/app/" in fn and "/llm" not in fn and "/v2/bridges/llm" not in fn:
                        rel = fn.split("/app/", 1)[-1]
                        tool_caller_hint = (
                            f" caller={rel}:{frame.lineno}({frame.name})"
                        )
                        break
            except Exception:
                pass

        # Compact one-liner — searchable in logs
        log_method(
            "TOKEN-BREAKDOWN agent=%s model=%s total=%d "
            "(sys=%d hist=%d tools=%d) "
            "hist_split=[user=%d/%d asst_text=%d/%d asst_tc=%d/%d(%dtcs) tool=%d/%d] "
            "delta=%+d%s",
            (agent_id or "-")[:8], model,
            total,
            sys_chars, history_chars, tools_chars,
            user_chars, n_user,
            asst_text_chars, n_asst_text,
            asst_tool_call_chars, n_asst_tc, total_tcs,
            tool_result_chars, n_tool,
            delta, tool_caller_hint,
        )
    except Exception as _e:
        # Never let diagnostics crash a real LLM call.
        breakdown_logger.debug("breakdown computation failed: %s", _e)

# HTTP timeouts for chat/completions requests.
#   CONNECT — includes TCP handshake AND the time to flush the request body
#             to the server. 45s accommodates large prompts (50KB+ of tool
#             schemas + message history + XML context blocks) over slow
#             uplinks — a common cause of the "The write operation timed
#             out" urllib3 warnings.
#   READ    — time to receive the FIRST byte of the response. Default 3 min;
#             longer hangs almost always mean cloud-model stall, and shorter
#             timeouts let the fallback-LLM path kick in quickly.
# Override via env vars TUDOU_LLM_CONNECT_TIMEOUT / TUDOU_LLM_READ_TIMEOUT.
try:
    # connect timeout default lowered 45→10s (2026-04-30): 45s was too
    # forgiving for "is the provider even reachable?" — combined with
    # the outer retry loop, a stale endpoint could burn 10+ minutes
    # before surfacing a connect failure. 10s is plenty for a healthy
    # API endpoint and fast-fails to the next retry / fallback otherwise.
    _CONNECT_TIMEOUT = float(os.environ.get("TUDOU_LLM_CONNECT_TIMEOUT", "10"))
except ValueError:
    _CONNECT_TIMEOUT = 45.0
try:
    _READ_TIMEOUT = float(os.environ.get("TUDOU_LLM_READ_TIMEOUT", "180"))
except ValueError:
    _READ_TIMEOUT = 180.0
_REQUEST_TIMEOUT: tuple[float, float] = (_CONNECT_TIMEOUT, _READ_TIMEOUT)

# Set by Hub.__init__ so token logger can route per-agent stats.
_active_hub = None

# ── Token usage accounting ──
# 全局累计 + 按 (provider, model) 累计，方便 portal 展示和导出。
_TOKEN_LOCK = threading.Lock()
_TOKEN_TOTALS: dict = {
    "total_in": 0,
    "total_out": 0,
    "calls": 0,
    "cache_read_total": 0,    # tokens served from prompt cache (cheap path)
    "cache_write_total": 0,   # tokens that wrote to prompt cache (Anthropic only;
                              #   ~125% cost for that turn but pays back later)
    "by_model": {},   # key: f"{provider}/{model}" -> {"in", "out", "calls",
                      #                                  "cache_read", "cache_write"}
}


def _extract_cache_tokens(usage: dict) -> tuple[int, int]:
    """Provider-agnostic cache-token extraction from a usage dict.

    Returns ``(cache_read, cache_write)``:

      * Anthropic              ``cache_read_input_tokens`` / ``cache_creation_input_tokens``
      * OpenAI / GPT-4o / mini ``prompt_tokens_details.cached_tokens`` (read only —
                               OpenAI's prompt cache writes are free, no separate metric)
      * DeepSeek               ``prompt_cache_hit_tokens`` (read; miss isn't a write)

    Returns ``(0, 0)`` on missing / malformed usage.
    """
    if not isinstance(usage, dict):
        return 0, 0
    # Anthropic — explicit two-sided metric.
    cr = int(usage.get("cache_read_input_tokens") or 0)
    cw = int(usage.get("cache_creation_input_tokens") or 0)
    if cr or cw:
        return cr, cw
    # DeepSeek — hit count, no separate write metric.
    cr = int(usage.get("prompt_cache_hit_tokens") or 0)
    if cr:
        return cr, 0
    # OpenAI standard (Nov 2024+) — nested under prompt_tokens_details.
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cr = int(details.get("cached_tokens") or 0)
        if cr:
            return cr, 0
    return 0, 0
# 当前线程上下文：让 agent 调用 LLM 时把 agent_id / project_id /
# meeting_id / chat_task_id 透传进来，token 统计按多维度归属。
# 所有字段都是可选：只有 agent_id 一定会有（LLM 调用必然来自某 agent）;
# project_id / meeting_id / chat_task_id 看调用场景而定。
_TOKEN_CTX = threading.local()


def set_token_context(agent_id: str = "",
                       project_id: str = "",
                       meeting_id: str = "",
                       chat_task_id: str = "") -> None:
    """供 agent / project_chat / meeting_chat 在调 LLM 之前调用,
    标记调用归属。按多层归属汇总 token: per-agent / per-task /
    per-project / per-meeting."""
    _TOKEN_CTX.agent_id = agent_id
    _TOKEN_CTX.project_id = project_id
    _TOKEN_CTX.meeting_id = meeting_id
    _TOKEN_CTX.chat_task_id = chat_task_id


def clear_token_context() -> None:
    _TOKEN_CTX.agent_id = ""
    _TOKEN_CTX.project_id = ""
    _TOKEN_CTX.meeting_id = ""
    _TOKEN_CTX.chat_task_id = ""


def get_token_totals() -> dict:
    """供 portal API 查询累计 token 使用。

    新增 ``cache_*`` 字段 + ``cache_hit_rate`` (0-1):
      cache_hit_rate = cache_read_total / total_in
    高命中率 = system prompt prefix 切得稳;低命中率 = prefix 经常变,
    每次都要重写缓存。可以据此判断是否该上"块化条件装入"那一层。
    """
    with _TOKEN_LOCK:
        total_in = _TOKEN_TOTALS["total_in"]
        cr = _TOKEN_TOTALS.get("cache_read_total", 0)
        cw = _TOKEN_TOTALS.get("cache_write_total", 0)
        return {
            "total_in": total_in,
            "total_out": _TOKEN_TOTALS["total_out"],
            "calls": _TOKEN_TOTALS["calls"],
            "cache_read_total": cr,
            "cache_write_total": cw,
            "cache_hit_rate": (cr / total_in) if total_in > 0 else 0.0,
            "by_model": dict(_TOKEN_TOTALS["by_model"]),
        }


def _log_token_usage(provider: str, model: str,
                      prompt_tokens: int = 0,
                      completion_tokens: int = 0,
                      stream: bool = False,
                      payload_kb: float = 0.0,
                      cache_read: int = 0,
                      cache_write: int = 0) -> None:
    """
    记录一次 LLM 调用的 token 用量。
    - 写到 tudou.tokens logger
    - 累加到全局计数器
    - 关联当前线程的 agent_id / project_id（若有）

    ``cache_read`` / ``cache_write`` 是 prompt-cache 度量。``prompt_tokens``
    是 provider 报告的总输入(已包含从缓存读的部分),``cache_read`` 是其
    中"从缓存读"的字段;命中率 = cache_read / total_in。
    """
    p_in = int(prompt_tokens or 0)
    p_out = int(completion_tokens or 0)
    cr = int(cache_read or 0)
    cw = int(cache_write or 0)
    agent_id = getattr(_TOKEN_CTX, "agent_id", "") or ""
    project_id = getattr(_TOKEN_CTX, "project_id", "") or ""
    meeting_id = getattr(_TOKEN_CTX, "meeting_id", "") or ""
    chat_task_id = getattr(_TOKEN_CTX, "chat_task_id", "") or ""

    # Per-call 日志降级到 DEBUG — 之前每次调用都刷屏,用户投诉"是不是没有聚合"。
    # INFO 级改为聚合 rollup(见下),每 N 次或每 M 秒打一次。
    cache_part = f" cache_r={cr} cache_w={cw}" if (cr or cw) else ""
    token_logger.debug(
        "TOKEN provider=%s model=%s in=%d out=%d total=%d "
        "stream=%s payload_kb=%.1f agent=%s project=%s meeting=%s task=%s%s",
        provider, model, p_in, p_out, p_in + p_out,
        stream, payload_kb,
        agent_id[:8], project_id[:8],
        meeting_id[:8], chat_task_id[:8], cache_part,
    )

    key = f"{provider}/{model}"
    with _TOKEN_LOCK:
        _TOKEN_TOTALS["total_in"] += p_in
        _TOKEN_TOTALS["total_out"] += p_out
        _TOKEN_TOTALS["calls"] += 1
        _TOKEN_TOTALS["cache_read_total"] = (
            _TOKEN_TOTALS.get("cache_read_total", 0) + cr
        )
        _TOKEN_TOTALS["cache_write_total"] = (
            _TOKEN_TOTALS.get("cache_write_total", 0) + cw
        )
        bucket = _TOKEN_TOTALS["by_model"].setdefault(
            key, {"in": 0, "out": 0, "calls": 0,
                  "cache_read": 0, "cache_write": 0})
        bucket["in"] += p_in
        bucket["out"] += p_out
        bucket["calls"] += 1
        bucket["cache_read"] = bucket.get("cache_read", 0) + cr
        bucket["cache_write"] = bucket.get("cache_write", 0) + cw

    # 按维度写入 per-scope 累计器。每个目标对象维护自己的 _token_stats,
    # 查询时 O(1) 读出。
    def _accumulate(target):
        if target is None:
            return
        stats = getattr(target, "_token_stats", None)
        if stats is None:
            stats = {"in": 0, "out": 0, "calls": 0,
                     "cache_read": 0, "cache_write": 0,
                     "by_model": {}}
            try:
                target._token_stats = stats
            except Exception:
                return
        stats["in"] += p_in
        stats["out"] += p_out
        stats["calls"] += 1
        stats["cache_read"] = stats.get("cache_read", 0) + cr
        stats["cache_write"] = stats.get("cache_write", 0) + cw
        bm = stats.setdefault("by_model", {})
        b = bm.setdefault(key, {"in": 0, "out": 0, "calls": 0,
                                "cache_read": 0, "cache_write": 0})
        b["in"] += p_in
        b["out"] += p_out
        b["calls"] += 1
        b["cache_read"] = b.get("cache_read", 0) + cr
        b["cache_write"] = b.get("cache_write", 0) + cw

    # 注意 _active_hub 是挂在 THIS module (app.llm) 的全局变量,由
    # Hub.__init__ 启动时写入 (see hub/_core.py:143).
    if agent_id or project_id or meeting_id or chat_task_id:
        try:
            _h = _active_hub
            if _h is not None:
                # Agent
                if agent_id and hasattr(_h, "agents"):
                    _accumulate(_h.agents.get(agent_id))
                # Project
                if project_id and hasattr(_h, "projects"):
                    _accumulate(_h.projects.get(project_id))
                # Meeting
                if meeting_id and hasattr(_h, "meetings"):
                    _accumulate(_h.meetings.get(meeting_id))
                # Chat task — looked up through the chat_task manager
                if chat_task_id:
                    try:
                        from .chat_task import get_chat_task_manager
                        mgr = get_chat_task_manager()
                        task = mgr.get(chat_task_id) if mgr else None
                        _accumulate(task)
                    except Exception:
                        pass
        except Exception:
            pass

    # ── Rollup log (INFO) ── 每 10 次调用或每 30 秒，打一次当前 scope 的累计快照。
    # 解决用户投诉"是不是没有聚合,还是很多条"。per-call 已降级到 DEBUG,这里补上
    # 周期性的聚合 INFO。只对有 agent 归属的调用做,避免无主调用也刷屏。
    if agent_id:
        try:
            _maybe_rollup_log(agent_id, project_id, meeting_id, chat_task_id)
        except Exception:
            pass


# ── Rollup helper state ──
# {(agent, project, meeting, task) -> {"calls_since":int, "last_ts":float}}
_ROLLUP_STATE: dict = {}
_ROLLUP_LOCK = threading.Lock()
_ROLLUP_CALLS_THRESHOLD = 10   # flush每累计10次调用
_ROLLUP_SECONDS_THRESHOLD = 30  # 或每30秒一次

def _maybe_rollup_log(agent_id: str, project_id: str,
                       meeting_id: str, chat_task_id: str) -> None:
    """Periodically emit aggregated TOKEN-TOTAL log per scope."""
    import time as _t
    key = (agent_id, project_id, meeting_id, chat_task_id)
    now = _t.time()
    should_flush = False
    with _ROLLUP_LOCK:
        st = _ROLLUP_STATE.setdefault(key, {"calls_since": 0, "last_ts": now})
        st["calls_since"] += 1
        if (st["calls_since"] >= _ROLLUP_CALLS_THRESHOLD
                or now - st["last_ts"] >= _ROLLUP_SECONDS_THRESHOLD):
            should_flush = True
            st["calls_since"] = 0
            st["last_ts"] = now
    if not should_flush:
        return

    # Pull current per-scope totals from the accumulator objects.
    def _read(target):
        if target is None:
            return None
        return getattr(target, "_token_stats", None)

    _h = _active_hub
    if _h is None:
        return
    parts = []
    try:
        if agent_id and hasattr(_h, "agents"):
            s = _read(_h.agents.get(agent_id))
            if s:
                parts.append("agent=%s in=%d out=%d calls=%d" % (
                    agent_id[:8], s["in"], s["out"], s["calls"]))
        if project_id and hasattr(_h, "projects"):
            s = _read(_h.projects.get(project_id))
            if s:
                parts.append("project=%s in=%d out=%d calls=%d" % (
                    project_id[:8], s["in"], s["out"], s["calls"]))
        if meeting_id and hasattr(_h, "meetings"):
            s = _read(_h.meetings.get(meeting_id))
            if s:
                parts.append("meeting=%s in=%d out=%d calls=%d" % (
                    meeting_id[:8], s["in"], s["out"], s["calls"]))
    except Exception:
        return
    if parts:
        token_logger.info("TOKEN-ROLLUP " + " | ".join(parts))

# ---------------------------------------------------------------------------
# Prompt Caching and Budget Management
# ---------------------------------------------------------------------------

from .defaults import (
    BUDGET_WARNING_THRESHOLD as _BUDGET_WARN,
    BUDGET_ATTENTION_THRESHOLD as _BUDGET_ATTN,
    OLLAMA_URL as _DEF_OLLAMA_URL,
    OPENAI_BASE_URL as _DEF_OPENAI_URL,
    UNSLOTH_BASE_URL as _DEF_UNSLOTH_URL,
    DEFAULT_PROVIDER as _DEF_PROVIDER,
    DEFAULT_MODEL as _DEF_MODEL,
    LLM_MAX_RETRIES, LLM_BACKOFF_FACTOR,
    LLM_POOL_CONNECTIONS, LLM_POOL_MAXSIZE,
)

_budget_caution_threshold = _BUDGET_WARN
_budget_warning_threshold = _BUDGET_ATTN


def _ensure_str(content) -> str:
    """Normalize message content to a plain string.

    Some code paths may store content as a list of content blocks
    (OpenAI multimodal format) or other non-string types.  APIs like
    Qwen / LM Studio reject non-string content with 400.  This helper
    ensures a plain string.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif "text" in block:
                    parts.append(block["text"])
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            elif isinstance(block, str):
                parts.append(block)
            else:
                parts.append(str(block))
        return "\n".join(parts)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    return str(content)


def _compress_description(desc: str, max_chars: int = 160) -> str:
    """Keep first sentence of a tool description, drop examples / rationale.

    Tool `description` fields in the schema accumulated long-form prose over
    time (use cases, comparisons, decision trees, example prompts). LLMs
    only need a terse "what does this do" to decide calling. Everything
    beyond the first sentence is ~75% of the schema weight.

    Heuristic:
      1. Take text up to first double-newline or \\n (break on paragraph).
      2. Take first sentence (until '.' / '。' / '!' / '？') if still long.
      3. Hard cap at max_chars, add '…' suffix if truncated.

    LLM can still fetch the full description via `get_skill_guide` tool if
    it needs more context.
    """
    if not desc:
        return ""
    s = desc.strip()
    if len(s) <= max_chars:
        return s
    # Break on paragraph / line
    for sep in ("\n\n", "\n"):
        if sep in s:
            head = s.split(sep, 1)[0].strip()
            if head and len(head) <= max_chars:
                return head
            if head:
                s = head
                break
    # Still too long — truncate on first sentence terminator
    for punct in ("。", ". ", ".", "!", "？", "?", ";"):
        idx = s.find(punct)
        if 40 <= idx <= max_chars:
            return s[:idx + len(punct.rstrip())].strip()
    # Hard truncate
    return s[:max_chars].rstrip() + "…"


def _compress_parameter_properties(props: dict, per_prop_max: int = 100) -> dict:
    """Shorten `description` on each parameter property. Keeps structural
    info (type, enum, required) intact — only prose is trimmed."""
    if not isinstance(props, dict):
        return props
    out = {}
    for k, v in props.items():
        if not isinstance(v, dict):
            out[k] = v
            continue
        cp = dict(v)
        if "description" in cp:
            cp["description"] = _compress_description(
                cp["description"], max_chars=per_prop_max)
        # Recurse into nested objects (items / properties)
        if isinstance(cp.get("items"), dict):
            nested = cp["items"]
            if "properties" in nested and isinstance(nested["properties"], dict):
                nested = dict(nested)
                nested["properties"] = _compress_parameter_properties(
                    nested["properties"], per_prop_max)
                cp["items"] = nested
            elif "description" in nested:
                ni = dict(nested)
                ni["description"] = _compress_description(
                    ni["description"], max_chars=per_prop_max)
                cp["items"] = ni
        if isinstance(cp.get("properties"), dict):
            cp["properties"] = _compress_parameter_properties(
                cp["properties"], per_prop_max)
        out[k] = cp
    return out


def _validate_tools(tools: list[dict] | None,
                     compress: bool = True) -> list[dict] | None:
    """Validate and clean tool definitions before sending to LLM.

    - Removes tools with empty/missing name, description, or parameters
    - Ensures each tool has a valid function-calling schema
    - (NEW) If `compress=True` (default), shrinks tool `description` +
      parameter-property `description` fields to first-sentence form.
      Disable via env TUDOU_TOOL_SCHEMA_FULL=1 for debugging.
    - Returns None if no valid tools remain (so empty list won't be sent)
    """
    if not tools:
        return None
    if compress:
        compress = os.environ.get("TUDOU_TOOL_SCHEMA_FULL", "0") != "1"
    valid = []
    for t in tools:
        func = t.get("function", {}) if t.get("type") == "function" else t
        if not isinstance(func, dict):
            continue
        name = func.get("name")
        desc = func.get("description")
        # Must have a name
        if not name or not isinstance(name, str) or not name.strip():
            continue
        # Build clean tool definition
        clean_func = {"name": name.strip()}
        if desc and isinstance(desc, str) and desc.strip():
            d = desc.strip()
            if compress:
                d = _compress_description(d, max_chars=160)
            clean_func["description"] = d
        params = func.get("parameters")
        if params and isinstance(params, dict):
            # Only include parameters if it has actual properties
            props = params.get("properties")
            if props and isinstance(props, dict) and len(props) > 0:
                if compress:
                    cleaned_params = dict(params)
                    cleaned_params["properties"] = _compress_parameter_properties(
                        props, per_prop_max=100)
                    clean_func["parameters"] = cleaned_params
                else:
                    clean_func["parameters"] = params
            else:
                # Empty parameters object — use minimal schema
                clean_func["parameters"] = {"type": "object", "properties": {}}
        valid.append({"type": "function", "function": clean_func})
    return valid if valid else None


# Regex: markdown code fences  ```lang\n...\n```  (non-greedy per block)
_CODE_FENCE_RE = re.compile(
    r"```(?:json|JSON|js|javascript|python|py|yaml|yml|toml|xml|html|css|"
    r"bash|sh|shell|sql|text|plaintext|txt|markdown|md|go|rust|java|c|cpp|"
    r"csharp|ruby|php|swift|kotlin|scala|r|lua|perl|haskell|typescript|ts)?"
    r"\s*\n(.*?)```",
    re.DOTALL,
)


# ─────────────────────────────────────────────────────────────────────
# LLM Provider Adapters — extracted to app/llm_providers.py
# ─────────────────────────────────────────────────────────────────────
from .llm_providers import (  # noqa: F401
    LLMProvider, ProviderStrategy,
    register_provider, resolve_strategy, _detect_provider_kind,
    OpenAIProvider, AnthropicProvider, DeepSeekProvider, GLMProvider,
    QwenProvider, VolcesProvider, OllamaProvider, LMStudioProvider,
)


def _strip_images_for_text_only(messages: list[dict]) -> tuple[list[dict], int]:
    """Walk messages; for any user content that is a list with image_url
    parts, replace it with a plain-text summary that preserves the text
    parts and notes how many images were stripped.

    Returns (new_messages, stripped_count)."""
    out: list[dict] = []
    stripped = 0
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            out.append(m)
            continue
        text_parts: list[str] = []
        img_n = 0
        has_other = False
        for p in content:
            if not isinstance(p, dict):
                continue
            ptype = p.get("type", "")
            if ptype == "text":
                text_parts.append(str(p.get("text", "")))
            elif ptype in ("image_url", "image", "input_image"):
                img_n += 1
            elif ptype in ("audio", "input_audio"):
                img_n += 1  # audio also unsupported on text-only
            else:
                has_other = True
                text_parts.append(str(p.get("text", "")))
        if img_n == 0 and not has_other:
            out.append(m)
            continue
        joined_text = "\n".join(t for t in text_parts if t).strip()
        if img_n:
            note = f"\n\n[已省略 {img_n} 张图片 — 当前 LLM 不支持视觉,如需识图请切换支持 vision 的模型]"
            joined_text = (joined_text + note).strip()
        new_m = dict(m)
        new_m["content"] = joined_text or "[image attached, stripped for text-only model]"
        out.append(new_m)
        stripped += img_n
    return out, stripped


def _sanitize_messages_for_openai(messages: list[dict],
                                    target_url: str = "",
                                    target_model: str = "") -> list[dict]:
    """Sanitize messages for OpenAI-compatible APIs (LM Studio, Qwen, vLLM, Ollama, etc.).

    1. Merge multiple system messages into ONE (many local servers reject >1)
    2. Normalize content to plain strings (non-string content causes 400)
    3. Strip non-standard fields (e.g. 'source', '_dynamic') that strict APIs reject
    4. Preserve only standard OpenAI fields per role
    5. Downgrade image_url parts to text when the target model is text-only
       (DeepSeek chat etc. — their JSON schema rejects unknown variant `image_url`)
    """
    # Pre-pass: strip images if the target provider declares supports_vision=False.
    # Single source of truth: LLMProvider.supports_vision in app/llm_providers.py.
    # Operators can opt providers in/out via llm_provider_configs/<name>.yaml.
    try:
        _strategy = resolve_strategy(target_url)
        _vision_ok = bool(getattr(_strategy, "supports_vision", True))
    except Exception:
        _vision_ok = True  # conservative: don't strip when we can't resolve
    if not _vision_ok:
        messages, _img_n = _strip_images_for_text_only(messages)
        if _img_n:
            logger.warning(
                "[sanitize] stripped %d image part(s) before sending to "
                "text-only target (url=%s model=%s) — replaced with text "
                "placeholders so the conversation stays coherent.",
                _img_n, target_url, target_model,
            )
    # Standard fields per role.
    # `reasoning_content` (assistant): DeepSeek "thinking mode" requires the
    # model's previous reasoning to be passed back on the next turn, else
    # it returns:
    #   "The reasoning_content in the thinking mode must be passed back"
    # We treat it as a first-class assistant field — other providers either
    # ignore it or error on unknown field; for safety we drop it at send
    # time for non-DeepSeek providers (see _drop_reasoning_for_non_deepseek).
    _STANDARD_FIELDS = {
        "system": {"role", "content", "name"},
        "user": {"role", "content", "name"},
        "assistant": {"role", "content", "tool_calls", "name", "refusal",
                      "reasoning_content"},
        "tool": {"role", "content", "tool_call_id"},
    }
    _DEFAULT_FIELDS = {"role", "content", "name"}

    # --- Pass 1: merge all system messages into one ---
    system_parts = []
    non_system = []
    for msg in messages:
        if msg.get("role") == "system":
            content = msg.get("content")
            text = _ensure_str(content) if not isinstance(content, str) else (content or "")
            if text.strip():
                system_parts.append(text)
        else:
            non_system.append(msg)

    merged = []
    if system_parts:
        merged.append({"role": "system", "content": "\n\n".join(system_parts)})
    merged.extend(non_system)

    # --- Pass 1.5: drop orphan tool messages ---
    # A `tool` message must follow an `assistant` with matching tool_calls.id.
    # Context compaction / history trimming can drop the assistant while
    # leaving the tool result, which makes DeepSeek/OpenAI/strict APIs 400
    # with: "Messages with role 'tool' must be a response to a preceding
    # message with 'tool_calls'". This is the LAST line of defense — every
    # OpenAI-compat call goes through here.
    seen_tc_ids: set[str] = set()
    deorphan: list[dict] = []
    orphan_count = 0
    for m in merged:
        r = m.get("role", "")
        if r == "assistant":
            for tc in (m.get("tool_calls") or []):
                if isinstance(tc, dict):
                    tcid = tc.get("id") or ""
                    if tcid:
                        seen_tc_ids.add(tcid)
            deorphan.append(m)
        elif r == "tool":
            tcid = m.get("tool_call_id") or ""
            if tcid and tcid in seen_tc_ids:
                deorphan.append(m)
            else:
                orphan_count += 1
        else:
            deorphan.append(m)
    if orphan_count:
        logger.warning(
            "[sanitize] dropped %d orphan tool message(s) — context "
            "compaction likely lost the paired assistant.tool_calls",
            orphan_count,
        )
    merged = deorphan

    # --- Pass 2: clean fields + normalize content + fix tool_calls ---
    sanitized = []
    for msg in merged:
        role = msg.get("role", "user")
        allowed = _STANDARD_FIELDS.get(role, _DEFAULT_FIELDS)

        # Build clean message with only standard fields
        clean = {}
        for key in msg:
            if key in allowed:
                clean[key] = msg[key]

        # Tool messages MUST carry a non-empty tool_call_id — strict APIs
        # (Volces Ark, DeepSeek, OpenAI) return 400 otherwise. If it's
        # missing, we drop this orphaned tool message entirely. It came
        # from a compressed/truncated history where the matching assistant
        # tool_calls were lost.
        if role == "tool":
            tcid = clean.get("tool_call_id") or ""
            if not tcid:
                continue
            clean["tool_call_id"] = tcid

        # Normalize content: preserve multimodal list content for user messages
        # (vision models expect [{type:"text",text:...},{type:"image_url",...}])
        content = clean.get("content")
        if content is not None and not isinstance(content, str):
            if isinstance(content, list) and role == "user" and any(
                isinstance(p, dict) and p.get("type") in (
                    "image_url", "image", "input_image", "audio", "input_audio"
                )
                for p in content
            ):
                # Multimodal content — preserve list format for vision APIs
                clean["content"] = content
            else:
                clean["content"] = _ensure_str(content)

        # Fix tool_calls: ensure each has id, type, and arguments as string
        if "tool_calls" in clean and clean["tool_calls"]:
            fixed_tcs = []
            for i, tc in enumerate(clean["tool_calls"]):
                if not isinstance(tc, dict):
                    continue
                ftc = {
                    "id": tc.get("id") or f"call_{i}_{id(msg) % 10000:04d}",
                    "type": tc.get("type", "function"),
                    "function": {},
                }
                func = tc.get("function", {})
                if isinstance(func, dict):
                    args = func.get("arguments", "{}")
                    # arguments MUST be a JSON string, not a dict
                    if isinstance(args, dict):
                        args = json.dumps(args, ensure_ascii=False)
                    elif not isinstance(args, str):
                        args = str(args)
                    ftc["function"] = {
                        "name": func.get("name", "unknown"),
                        "arguments": args,
                    }
                fixed_tcs.append(ftc)
            if fixed_tcs:
                clean["tool_calls"] = fixed_tcs
            else:
                # All entries were invalid → drop field entirely. DeepSeek
                # rejects with: "tool_calls: empty array. Expected min 1"
                clean.pop("tool_calls", None)
        elif "tool_calls" in clean:
            # Field present but empty list / None → strict APIs (DeepSeek
            # / OpenAI) reject. Remove it entirely; semantically equivalent
            # to "this assistant turn made no tool calls".
            clean.pop("tool_calls", None)

        # ── Cross-provider invariants ───────────────────────────────────
        # Some providers (GLM/智谱、Qwen) reject assistant messages where
        # both `content` AND `tool_calls` are present-and-non-empty.
        # OpenAI accepts both. Volces / DeepSeek accept either.
        # Safe lowest-common-denominator: when an assistant has tool_calls,
        # drop content if it's empty/whitespace; preserve it only when
        # there's real text (then OpenAI is happy + GLM tolerates short text).
        if (role == "assistant"
                and clean.get("tool_calls")
                and isinstance(clean.get("content"), str)
                and not clean["content"].strip()):
            clean.pop("content", None)

        # Tool message content must be a string (some providers reject list).
        if role == "tool" and not isinstance(clean.get("content"), str):
            clean["content"] = _ensure_str(clean.get("content"))

        # Drop empty user messages — every API rejects role:user, content:""
        if role == "user":
            _c = clean.get("content")
            if _c is None:
                continue
            if isinstance(_c, str) and not _c.strip():
                continue

        # Drop fully-empty assistant messages (no content + no tool_calls).
        # They can leak in from aborted streams / nudge rollback.
        if role == "assistant":
            _has_text = isinstance(clean.get("content"), str) and clean["content"].strip()
            _has_tools = bool(clean.get("tool_calls"))
            if not _has_text and not _has_tools:
                continue

        sanitized.append(clean)

    # --- Pass 3: enforce tool_call ↔ tool message pairing ---
    # Strict APIs (DeepSeek, Volces Ark, OpenAI) require:
    #   (a) tool messages must appear IMMEDIATELY after the assistant message
    #       that produced the matching tool_calls — any user / system /
    #       assistant-without-tool_calls in between breaks the pairing
    #   (b) every tool_call_id in a tool message must match a tool_calls[].id
    #       in the most recent assistant block
    #   (c) assistant tool_calls with no following tool message are orphaned
    #
    # Old logic only did (b) — tool was kept whenever its id had EVER been
    # seen in the history. DeepSeek rejects that: `Messages with role 'tool'
    # must be a response to a preceding message with 'tool_calls'`.
    #
    # New: track the "active" tool_call block. Cleared by any non-tool, non-
    # assistant-with-tool_calls message. Tool only survives if its id is in
    # the currently active block.
    active_ids: set = set()       # ids from the *most recent* assistant.tool_calls
    final: list = []
    for m in sanitized:
        r = m.get("role")
        if r == "assistant":
            if m.get("tool_calls"):
                # new active block — replace any previous
                active_ids = {tc.get("id") or "" for tc in m["tool_calls"]}
                active_ids.discard("")
            else:
                # plain text assistant — closes any open tool block
                active_ids = set()
            final.append(m)
        elif r == "tool":
            tcid = m.get("tool_call_id") or ""
            if tcid and tcid in active_ids:
                final.append(m)
                # NB: don't clear active_ids; multi-call blocks can have
                # several tool responses in a row for different ids
            # else: orphaned (no active assistant.tool_calls right before
            # it, or tool_call_id not in current block) — drop entirely
        else:
            # user / system / other — closes any open tool block
            active_ids = set()
            final.append(m)

    # Pass 3b: strip assistant messages whose every tool_call has no
    # follow-up tool message. We walk once more forward.
    resolved: set = set()
    for m in final:
        if m.get("role") == "tool":
            resolved.add(m.get("tool_call_id", ""))
    cleaned: list = []
    for m in final:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            kept_tcs = [tc for tc in m["tool_calls"] if tc.get("id") in resolved]
            if kept_tcs:
                mm = dict(m)
                mm["tool_calls"] = kept_tcs
                cleaned.append(mm)
            else:
                # No resolved tool_calls. If the assistant message has text
                # content, strip tool_calls and keep it; otherwise drop it.
                txt = (m.get("content") or "").strip()
                if txt:
                    mm = dict(m)
                    mm.pop("tool_calls", None)
                    cleaned.append(mm)
                # else drop entirely
        else:
            cleaned.append(m)

    # --- Pass 4: neutralize curly braces in content ---
    # Many local LLM servers (Ollama, vLLM, LM Studio, Qwen-serve) use JSON
    # parsers that misinterpret embedded '{' inside message content strings
    # as JSON object boundaries → "Value looks like object, but can't find
    # closing '}' symbol".
    #
    # Strategy:
    #   a) Markdown code fences: replace the whole fence with [code] and use
    #      fullwidth braces inside.
    #   b) Inline bare braces in non-system content: replace { } with fullwidth
    #      ｛｝.  This is safe because content is natural language / markdown
    #      rendered for the LLM, not machine-parsed JSON.  System messages are
    #      left alone because they may contain structured config the LLM needs
    #      to see with real braces.

    def _neutralize_braces(text: str, role: str) -> str:
        """Replace { } in message content to prevent LLM JSON parser confusion."""
        if not text or "{" not in text:
            return text
        # Step 1: replace code fences (``` ... ```) first
        if "```" in text:
            def _fence_replacer(m: re.Match) -> str:
                inner = m.group(1).strip()
                inner = inner.replace("{", "｛").replace("}", "｝")
                return f"[code]\n{inner}\n[/code]"
            text = _CODE_FENCE_RE.sub(_fence_replacer, text)
        # Step 2: replace remaining bare braces in non-system messages
        if role != "system" and "{" in text:
            text = text.replace("{", "｛").replace("}", "｝")
        return text

    # --- Pass 5: deduplicate near-identical messages ---
    # Scheduled tasks, retries, or buggy context assembly can produce
    # the same (or near-identical) messages repeated many times.
    # Two strategies:
    #   a) Exact consecutive same-role: drop duplicates
    #   b) Near-duplicate user messages (first 200 chars match): collapse
    #      repeated (user→assistant) cycles, keeping only the last cycle.

    def _content_fingerprint(text: str, n: int = 100) -> str:
        """Cheap fingerprint: first N chars, stripped of whitespace and digits.

        Using a short prefix (100 chars) and stripping digits ensures that
        messages like scheduled tasks which differ only in timestamps, counts,
        or sequence numbers still produce the same fingerprint.
        """
        if not text or len(text) < 40:
            return ""
        # Strip digits and common varying tokens (timestamps, counts)
        prefix = re.sub(r"[\d]+", "#", text[:n])
        return re.sub(r"\s+", " ", prefix.strip())

    # Collect fingerprints of user messages for near-dedup
    user_fps: dict[str, list[int]] = {}  # fingerprint → [indices]
    for i, m in enumerate(cleaned):
        if m.get("role") == "user" and not m.get("tool_calls"):
            fp = _content_fingerprint(m.get("content", ""))
            if fp:
                user_fps.setdefault(fp, []).append(i)

    # Mark indices to skip: for each repeated fingerprint group, keep only
    # the LAST occurrence (and its following assistant response).  Earlier
    # occurrences + their assistant replies are marked for collapse.
    skip_indices: set[int] = set()
    for fp, indices in user_fps.items():
        if len(indices) < 2:
            continue
        # Keep the last occurrence; skip all earlier ones + their next assistant
        for idx in indices[:-1]:
            skip_indices.add(idx)
            # Also skip the assistant reply that follows this user message
            nxt = idx + 1
            if nxt < len(cleaned) and cleaned[nxt].get("role") == "assistant":
                skip_indices.add(nxt)

    # Build final result with brace neutralization + dedup
    collapsed_count = 0
    deduped: list[dict] = []
    for i, m in enumerate(cleaned):
        if i in skip_indices:
            collapsed_count += 1
            continue

        # Neutralize braces in content
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, str) and content:
            safe_content = _neutralize_braces(content, role)
            if safe_content != content:
                m = dict(m)
                m["content"] = safe_content

        # Also skip exact consecutive duplicates (same role + content)
        if deduped:
            prev = deduped[-1]
            if (m.get("role") == prev.get("role")
                    and m.get("content") == prev.get("content")
                    and m.get("role") in ("user", "assistant")
                    and not m.get("tool_calls")):
                continue
        deduped.append(m)

    if collapsed_count > 0:
        logger.info("Sanitizer: collapsed %d near-duplicate messages (%d → %d)",
                     collapsed_count, len(cleaned), len(deduped))

    # ── Apply provider adapter ─────────────────────────────────────
    # Each LLM is its own self-contained adapter object. Resolution = one
    # call; per-message transform = one method call per message. Adding a
    # new LLM doesn't touch this code.
    adapter = resolve_strategy(target_url)
    # Fold first: providers with `max_tool_call_rounds` capped (e.g. GLM-
    # 4.5-air) need older tool rounds collapsed into plain text BEFORE
    # per-message field transforms run on the resulting messages.
    pre_fold_n = len(deduped)
    deduped = adapter.fold_excess_tool_rounds(deduped)
    if len(deduped) != pre_fold_n:
        logger.info(
            "Sanitizer: %s folded tool rounds (%d → %d msgs, cap=%d)",
            adapter.name, pre_fold_n, len(deduped),
            adapter.max_tool_call_rounds,
        )
    for m in deduped:
        adapter.transform_message(m)

    # ── Final structural integrity pass ─────────────────────────────────
    # Cross-provider invariants every OpenAI-compat API expects:
    #   1. Conversation must contain at least one non-system message
    #   2. Last message can be ANY role (system msg before tool result is OK)
    #      but [system, system, ..., system] alone is rejected
    #   3. Any leftover orphan tool message (passes pass 3 but ends up at
    #      position [0] after dedup) → drop
    #   4. After sanitization if no messages, raise so caller catches early
    #      rather than getting opaque 400 from provider
    final_pass = []
    for i, m in enumerate(deduped):
        # An orphan tool at the very start is invalid — every provider barfs.
        if i == 0 and m.get("role") == "tool":
            logger.warning("Sanitizer: dropped tool message at position 0 "
                           "(no preceding assistant.tool_calls)")
            continue
        final_pass.append(m)

    # Empty payload guard. Better to error fast than send empty messages.
    has_non_system = any(m.get("role") != "system" for m in final_pass)
    if not has_non_system:
        # Don't raise — some scenarios (initial ping, system probe) are OK.
        # Just log a hint and let the caller decide.
        logger.warning(
            "Sanitizer: payload has no user/assistant message after sanitization. "
            "This may cause provider 400. Original count: %d", len(messages))

    return final_pass


def apply_prompt_cache(messages: list[dict], provider_kind: str) -> list[dict]:
    """
    Apply prompt caching hints to messages for Anthropic/Claude providers.

    Only applies to anthropic/claude providers. Adds cache_control: {"type": "ephemeral"}
    to the system message and the last 3 user messages (rolling window).

    Modifies messages in-place by working on a deep copy before returning.

    Args:
        messages: List of message dicts
        provider_kind: Provider kind (e.g., "claude", "anthropic", "openai", "ollama")

    Returns:
        Modified messages list with cache control hints (or original if not applicable)
    """
    if provider_kind.lower() not in ("claude", "anthropic"):
        return messages

    # Work on a deep copy to avoid modifying original
    msgs = copy.deepcopy(messages)

    # Add cache control to system message
    for msg in msgs:
        if msg.get("role") == "system":
            if isinstance(msg.get("content"), str):
                msg["content"] = [{"type": "text", "text": msg["content"]}]
            if isinstance(msg.get("content"), list):
                for block in msg["content"]:
                    if isinstance(block, dict) and block.get("type") == "text":
                        block["cache_control"] = {"type": "ephemeral"}
            break

    # Add cache control to last 3 user messages (rolling window)
    user_messages = [i for i, msg in enumerate(msgs) if msg.get("role") == "user"]
    for idx in user_messages[-3:]:
        msg = msgs[idx]
        if isinstance(msg.get("content"), str):
            msg["content"] = [{"type": "text", "text": msg["content"]}]
        if isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if isinstance(block, dict) and block.get("type") == "text":
                    block["cache_control"] = {"type": "ephemeral"}

    return msgs


def get_budget_pressure_note(current_iteration: int, max_iterations: int) -> str | None:
    """
    Get a budget pressure note based on iteration progress.

    Args:
        current_iteration: Current iteration number (0-based)
        max_iterations: Maximum iterations allowed

    Returns:
        A budget pressure note string, or None if budget is not a concern
    """
    if max_iterations <= 0:
        return None

    # Use current_iteration + 1 to get 1-based iteration number for percentage
    iteration_pct = ((current_iteration + 1) / max_iterations) * 100

    if iteration_pct >= _budget_warning_threshold * 100:
        return f"🚨 Budget critical: {iteration_pct:.0f}% iterations used. Provide final answer NOW."
    elif iteration_pct >= _budget_caution_threshold * 100:
        return f"⚠️ Budget: {iteration_pct:.0f}% iterations used. Start wrapping up."

    return None

# ---------------------------------------------------------------------------
# Connection Pool — 长连接 + 重试 + 并发限流
# ---------------------------------------------------------------------------

class LLMConnectionPool:
    """
    Per-provider HTTP 连接池管理器 + per-model 并发队列。

    核心能力：
      1. requests.Session 长连接复用（TCP keep-alive，避免反复三次握手）
      2. 自动重试（429/500/502/503/504），指数退避
      3. Per-model 并发控制：同一个 provider+model 允许 N 个并发请求
         N 由 ProviderEntry.get_model_concurrency(model) 决定（默认=1 串行）
         不同 provider 或不同 model 之间独立控制
         队列 key = "provider_id:model"
      4. 连接池大小可配置（pool_connections / pool_maxsize）
      5. Per-provider RPM 限速（rate_limit_rpm > 0 时生效）
    """

    def __init__(self):
        self._sessions: dict[str, requests.Session] = {}
        self._lock = threading.Lock()

        # ── Per-model 并发队列 ──
        # key = "provider_id:model" → Semaphore(N)
        # N 从 ProviderEntry.get_model_concurrency(model) 获取
        self._model_locks: dict[str, threading.Semaphore] = {}
        # 记录每个 key 的当前 semaphore 容量，用于检测配置变更
        self._model_lock_size: dict[str, int] = {}

        # ── Per-provider RPM 限速 ──
        # key = provider_id → deque of timestamps (最近 60s 的请求时间)
        self._rpm_windows: dict[str, deque] = {}
        self._rpm_lock = threading.Lock()

        # 全局配置
        self.pool_connections = LLM_POOL_CONNECTIONS
        self.pool_maxsize = LLM_POOL_MAXSIZE
        self.max_retries = LLM_MAX_RETRIES
        self.backoff_factor = LLM_BACKOFF_FACTOR
        self.retry_on_status = (429, 500, 502, 503, 504)
        # 队列状态跟踪（调试用）
        self._queue_waiters: dict[str, int] = {}
        self._queue_lock = threading.Lock()

        # ── Circuit breaker ──────────────────────────────────────────
        # If a (provider_id, model) pair has failed repeatedly in a short
        # window, stop hammering it. The classic case is mlx-lm's
        # tool-parser bug: malformed XML tool calls crash the server
        # mid-stream, client sees connection-reset, retries 5x, same
        # 14k-token prompt re-processed each time. With max_iters=20 in
        # the agent loop, a single "hung" turn could run 100 retries
        # and 30+ minutes before giving up.
        #
        # Breaker semantics:
        #   OPEN   → next call raises immediately for `cooldown_s` seconds
        #   CLOSED → normal retry path
        # Transition:
        #   CLOSED → OPEN when N consecutive failures within `window_s`
        #   OPEN   → CLOSED after cooldown elapses (next call tries once)
        self._cb_fails: dict[str, list[float]] = {}   # key → [ts, ts, ...]
        self._cb_open_until: dict[str, float] = {}    # key → unix ts
        self._cb_lock = threading.Lock()
        self.cb_threshold = 2          # consecutive fails to trip
        self.cb_window_s = 120.0       # fails must land within this window
        self.cb_cooldown_s = 180.0     # how long the breaker stays open

    def get_session(self, provider_id: str) -> requests.Session:
        """获取或创建 provider 的长连接 Session。"""
        if provider_id in self._sessions:
            return self._sessions[provider_id]

        with self._lock:
            if provider_id in self._sessions:
                return self._sessions[provider_id]

            session = requests.Session()

            # ── Retry policy: DISABLE urllib3-level retry ──
            # 2026-04-30 incident: a 智谱 GLM endpoint was slow; the
            # outer streaming loop retried 6 times AND each attempt was
            # internally retried 5 times by urllib3 → 30 nested attempts,
            # each with up to (connect+read)=225s timeout. User stared
            # at the screen for nearly half an hour before failure
            # surfaced. The outer loop in chat() / stream_events()
            # already retries with proper backoff and yields error
            # events to the UI — having urllib3 retry below it is pure
            # double-counting. Set ``total=0`` so each request fails
            # fast back up to the outer retry, which has visibility.
            retry_strategy = Retry(
                total=0,
                connect=0,
                read=0,
                status=0,
                allowed_methods=["POST", "GET"],
                raise_on_status=False,
            )

            # HTTP/HTTPS 适配器 — 连接池 + 重试
            adapter = HTTPAdapter(
                pool_connections=self.pool_connections,
                pool_maxsize=self.pool_maxsize,
                max_retries=retry_strategy,
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)

            # TCP keep-alive headers
            session.headers.update({
                "Connection": "keep-alive",
            })

            self._sessions[provider_id] = session
            logger.info("Created connection pool for provider '%s' "
                        "(pool=%d, max=%d, retries=%d)",
                        provider_id, self.pool_connections,
                        self.pool_maxsize, self.max_retries)
            return session

    def _model_key(self, provider_id: str, model: str) -> str:
        """生成 provider+model 的队列 key。"""
        return f"{provider_id}:{model}"

    def _resolve_concurrency(self, provider_id: str, model: str) -> int:
        """从 ProviderRegistry 查询 provider+model 的并发数。"""
        try:
            reg = get_registry()
            provider = reg.get(provider_id)
            if provider:
                return provider.get_model_concurrency(model)
        except Exception:
            pass
        return 1  # 默认串行

    def _get_model_lock(self, key: str, provider_id: str = "",
                        model: str = "") -> threading.Semaphore:
        """
        获取或创建 model 级别的并发锁 Semaphore(N)。
        N 由 ProviderEntry.get_model_concurrency(model) 决定。
        如果配置变更（N 改了），自动重建 Semaphore。
        """
        desired = self._resolve_concurrency(provider_id, model) if provider_id else 1

        if key in self._model_locks:
            # 检查并发数是否发生变化
            if self._model_lock_size.get(key, 1) == desired:
                return self._model_locks[key]
            # 配置变更 → 需要重建（新请求用新值，旧请求继续用旧锁直到释放）
            logger.info("Concurrency changed for '%s': %d → %d",
                        key, self._model_lock_size.get(key, 1), desired)

        with self._lock:
            # double-check
            if key in self._model_locks and self._model_lock_size.get(key, 1) == desired:
                return self._model_locks[key]
            self._model_locks[key] = threading.Semaphore(desired)
            self._model_lock_size[key] = desired
            logger.info("Created concurrency queue for LLM slot '%s' "
                        "(max_concurrent=%d)", key, desired)
            return self._model_locks[key]

    def _wait_for_rpm(self, provider_id: str):
        """
        RPM (Requests Per Minute) 限速。
        如果 provider 配置了 rate_limit_rpm > 0，则在 60s 滑动窗口内
        限制请求数不超过该值。超过时 sleep 等待。
        """
        try:
            reg = get_registry()
            provider = reg.get(provider_id)
            if not provider or provider.rate_limit_rpm <= 0:
                return
            rpm_limit = provider.rate_limit_rpm
        except Exception:
            return

        with self._rpm_lock:
            if provider_id not in self._rpm_windows:
                self._rpm_windows[provider_id] = deque()
            window = self._rpm_windows[provider_id]

        # 清理 60s 之前的记录并检查是否超限
        now = time.time()
        cutoff = now - 60.0

        with self._rpm_lock:
            while window and window[0] < cutoff:
                window.popleft()
            if len(window) < rpm_limit:
                window.append(now)
                return
            # 需要等待：等到最早的请求过期
            wait_until = window[0] + 60.0

        wait_time = max(0.1, wait_until - time.time())
        logger.info("RPM limit reached for provider '%s' (%d/%d rpm), "
                     "waiting %.1fs...", provider_id, len(window),
                     rpm_limit, wait_time)
        time.sleep(wait_time)

        # 重新记录
        with self._rpm_lock:
            now = time.time()
            while window and window[0] < (now - 60.0):
                window.popleft()
            window.append(now)

    def acquire_slot(self, provider_id: str, model: str = ""):
        """
        获取 provider+model 的执行槽位。
        并发数由 ProviderEntry 配置决定（默认 1 = 串行）。
        同时遵守 RPM 限速。
        """
        key = self._model_key(provider_id, model)
        lock = self._get_model_lock(key, provider_id, model)
        with self._queue_lock:
            self._queue_waiters[key] = self._queue_waiters.get(key, 0) + 1
            waiting = self._queue_waiters[key]
        if waiting > 1:
            logger.info("Agent queued for '%s' (waiting: %d)", key, waiting)
        lock.acquire()
        with self._queue_lock:
            self._queue_waiters[key] = max(0, self._queue_waiters.get(key, 1) - 1)
        # RPM 限速检查（获取槽位后、发请求前）
        self._wait_for_rpm(provider_id)

    def release_slot(self, provider_id: str, model: str = ""):
        """释放 provider+model 的执行槽位。"""
        key = self._model_key(provider_id, model)
        lock = self._get_model_lock(key, provider_id, model)
        lock.release()

    # ── Circuit-breaker helpers ───────────────────────────────────────
    def _cb_key(self, provider_id: str, model: str) -> str:
        return f"{provider_id}:{model or ''}"

    def _cb_check(self, key: str) -> None:
        """If breaker is OPEN for this key, raise an informative error.
        Otherwise no-op."""
        now = time.time()
        with self._cb_lock:
            open_until = self._cb_open_until.get(key, 0.0)
            if open_until > now:
                remaining = int(open_until - now)
                raise RuntimeError(
                    f"LLM_CIRCUIT_OPEN: provider={key} is in cooldown "
                    f"for {remaining}s after {self.cb_threshold} "
                    f"consecutive failures. Common cause: local server "
                    f"(mlx-lm / Ollama) crashed parsing a bad tool call. "
                    f"Check server logs; restart the model if needed."
                )
            if open_until and open_until <= now:
                # Cooldown elapsed — reset so a single probe can try
                self._cb_open_until.pop(key, None)
                self._cb_fails.pop(key, None)

    def _cb_record_failure(self, key: str) -> None:
        now = time.time()
        with self._cb_lock:
            fails = self._cb_fails.setdefault(key, [])
            fails.append(now)
            # trim window
            cutoff = now - self.cb_window_s
            fails[:] = [t for t in fails if t >= cutoff]
            if len(fails) >= self.cb_threshold:
                self._cb_open_until[key] = now + self.cb_cooldown_s
                logger.error(
                    "Circuit breaker OPEN for '%s' — %d failures in "
                    "last %.0fs; next attempts will fail fast for %.0fs.",
                    key, len(fails), self.cb_window_s, self.cb_cooldown_s,
                )

    def _cb_record_success(self, key: str) -> None:
        with self._cb_lock:
            self._cb_fails.pop(key, None)
            self._cb_open_until.pop(key, None)

    def request_with_retry(self, provider_id: str, method: str, url: str,
                           model: str = "", **kwargs) -> requests.Response:
        """
        带连接池 + per-model 串行队列 + 手动退避重试 + 熔断 的 HTTP 请求。

        同一个 provider+model 排队串行，不同 provider/model 可并行。
        短期连续失败会跳闸，让后续调用立即失败，避免 mlx-lm / Ollama
        崩溃时把 14k prompt 反复重新推理。
        """
        cb_key = self._cb_key(provider_id, model)
        self._cb_check(cb_key)           # may raise if breaker is OPEN
        session = self.get_session(provider_id)
        last_exc = None

        for attempt in range(self.max_retries + 1):
            # 获取 provider+model 执行槽位（排队等待）
            self.acquire_slot(provider_id, model)
            try:
                resp = session.request(method, url, **kwargs)

                if resp.status_code == 429:
                    # 解析 Retry-After header
                    retry_after = resp.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait_time = float(retry_after)
                        except ValueError:
                            wait_time = self.backoff_factor * (2 ** attempt)
                    else:
                        wait_time = self.backoff_factor * (2 ** attempt)
                    wait_time = min(wait_time, 60)  # 最多等 60 秒

                    logger.warning(
                        "Provider '%s' returned 429 (attempt %d/%d), "
                        "retrying in %.1fs...",
                        provider_id, attempt + 1, self.max_retries + 1,
                        wait_time)
                    time.sleep(wait_time)
                    continue

                if resp.status_code in (500, 502, 503, 504):
                    wait_time = self.backoff_factor * (2 ** attempt)
                    logger.warning(
                        "Provider '%s' returned %d (attempt %d/%d), "
                        "retrying in %.1fs...",
                        provider_id, resp.status_code,
                        attempt + 1, self.max_retries + 1, wait_time)
                    time.sleep(wait_time)
                    continue

                self._cb_record_success(cb_key)
                return resp

            except requests.exceptions.ChunkedEncodingError as e:
                # Server crashed MID-response (e.g. mlx-lm tool parser
                # JSONDecodeError). Retrying sends the same 14k prompt
                # again — model will produce the same garbage → same
                # crash. Bail immediately and trip the breaker.
                last_exc = e
                logger.error(
                    "Provider '%s' dropped mid-response (server likely "
                    "crashed parsing output): %s. NOT retrying.",
                    provider_id, str(e)[:120])
                break  # breaker recorded in the outer finally
            except requests.Timeout as e:
                # Read timeouts should NOT be retried for local providers
                # (Ollama, LM Studio) — if inference took >5min, retrying
                # will just wait another 5min and fail again.
                last_exc = e
                logger.error(
                    "Provider '%s' read timeout (attempt %d/%d): %s "
                    "— not retrying (increase timeout or use a smaller model)",
                    provider_id, attempt + 1, self.max_retries + 1,
                    str(e)[:100])
                break  # breaker recorded in the outer finally  # Don't retry timeouts
            except requests.ConnectionError as e:
                last_exc = e
                # Connection-reset mid-response looks like ConnectionError
                # to some versions of requests. Same signal: don't retry.
                msg = str(e).lower()
                if ("connection aborted" in msg or "remote end closed" in msg
                        or "broken pipe" in msg):
                    logger.error(
                        "Provider '%s' connection dropped mid-response: %s. "
                        "NOT retrying (likely server-side crash).",
                        provider_id, str(e)[:120])
                    self._cb_record_failure(cb_key)
                    break
                wait_time = self.backoff_factor * (2 ** attempt)
                logger.warning(
                    "Provider '%s' connection error (attempt %d/%d): %s, "
                    "retrying in %.1fs...",
                    provider_id, attempt + 1, self.max_retries + 1,
                    str(e)[:100], wait_time)
                time.sleep(wait_time)
                continue
            except Exception as e:
                last_exc = e
                break
            finally:
                self.release_slot(provider_id, model)

        # 所有重试都失败了 — 记录熔断计数
        if last_exc:
            # Only record if we haven't already recorded above (chunk /
            # connection-drop / timeout branches already called it).
            self._cb_record_failure(cb_key)
            raise last_exc
        # 返回最后一次响应（可能是 429/5xx）
        resp.raise_for_status()
        return resp

    def close(self, provider_id: str = ""):
        """关闭连接池。"""
        with self._lock:
            if provider_id:
                s = self._sessions.pop(provider_id, None)
                if s:
                    s.close()
            else:
                for s in self._sessions.values():
                    s.close()
                self._sessions.clear()
                self._model_locks.clear()


# 全局连接池单例
_pool: LLMConnectionPool | None = None
_pool_lock = threading.Lock()


def get_connection_pool() -> LLMConnectionPool:
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = LLMConnectionPool()
    return _pool


# ---------------------------------------------------------------------------
# Legacy config (kept for backward compat — global default provider/model)
# ---------------------------------------------------------------------------

_CONFIG_CACHE: dict | None = None


def _load_config() -> dict:
    """Load config from config.yaml, with env-var overrides."""
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE

    defaults = {
        "provider": _DEF_PROVIDER,
        "model": _DEF_MODEL,
        "ollama_url": _DEF_OLLAMA_URL,
        "openai_api_key": "",
        "openai_base_url": _DEF_OPENAI_URL,
        "claude_api_key": "",
        "unsloth_base_url": _DEF_UNSLOTH_URL,
        "unsloth_api_key": "",
        # Cross-agent system prompt — prepended to every agent's static
        # system prompt. Empty string disables it.
        "global_system_prompt": "",
        "scene_prompts": [],  # list of {id, name, prompt, enabled}
    }

    config_path = Path(__file__).parent / "config.yaml"
    if config_path.exists():
        with open(config_path, "r", encoding="utf-8") as f:
            file_cfg = yaml.safe_load(f) or {}
        defaults.update({k: v for k, v in file_cfg.items()
                         if v is not None and v != ""})

    env_map = {
        "TUDOU_PROVIDER": "provider",
        "TUDOU_MODEL": "model",
        "TUDOU_OLLAMA_URL": "ollama_url",
        "TUDOU_OPENAI_API_KEY": "openai_api_key",
        "TUDOU_OPENAI_BASE_URL": "openai_base_url",
        "TUDOU_CLAUDE_API_KEY": "claude_api_key",
        "TUDOU_UNSLOTH_BASE_URL": "unsloth_base_url",
        "TUDOU_UNSLOTH_API_KEY": "unsloth_api_key",
        "TUDOU_GLOBAL_SYSTEM_PROMPT": "global_system_prompt",
    }
    for env_key, cfg_key in env_map.items():
        val = os.environ.get(env_key, "")
        if val:
            defaults[cfg_key] = val

    _CONFIG_CACHE = defaults
    return defaults


def get_config() -> dict:
    return _load_config()


def reload_config():
    global _CONFIG_CACHE
    _CONFIG_CACHE = None
    return _load_config()


def set_model(model_name: str):
    cfg = _load_config()
    cfg["model"] = model_name


def set_provider(provider_name: str):
    cfg = _load_config()
    cfg["provider"] = provider_name


def save_config():
    """Persist the current in-memory config to config.yaml."""
    cfg = _load_config()
    config_path = Path(__file__).parent / "config.yaml"
    # Only save user-configurable keys (not runtime-only values)
    saveable_keys = {
        "provider", "model", "ollama_url",
        "openai_api_key", "openai_base_url",
        "claude_api_key",
        "unsloth_base_url", "unsloth_api_key",
        "global_system_prompt",
        "scene_prompts",
    }
    # Keep falsy values for keys that are meaningfully "empty == cleared"
    # (e.g. global_system_prompt: empty string means "no global prompt").
    _allow_empty = {"global_system_prompt", "scene_prompts"}
    save_data = {
        k: v for k, v in cfg.items()
        if k in saveable_keys and (v or k in _allow_empty)
    }
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            yaml.dump(save_data, f, default_flow_style=False, allow_unicode=True)
        logger.info("Config saved to %s", config_path)
    except Exception as e:
        logger.error("Failed to save config: %s", e)


# ---------------------------------------------------------------------------
# Provider Registry
# ---------------------------------------------------------------------------

class ProviderEntry:
    """One registered LLM provider."""
    __slots__ = ("id", "name", "kind", "base_url", "api_key", "enabled",
                 "created_at", "models_cache", "models_cache_ts", "manual_models",
                 "scope", "max_concurrent", "model_concurrency", "priority",
                 "schedule_strategy", "rate_limit_rpm", "cost_per_1k_tokens",
                 "fallback_providers", "context_length", "tier_models",
                 "supports_multimodal")

    def __init__(self, *, id: str = "", name: str = "", kind: str = "openai",
                 base_url: str = "", api_key: str = "", enabled: bool = True,
                 created_at: float = 0.0, manual_models: list[str] | None = None,
                 scope: str = "local",
                 max_concurrent: int = 1,
                 model_concurrency: dict[str, int] | None = None,
                 priority: int = 10,
                 schedule_strategy: str = "serial",
                 rate_limit_rpm: int = 0,
                 cost_per_1k_tokens: float = 0.0,
                 fallback_providers: list[str] | None = None,
                 context_length: int = 0,
                 tier_models: dict[str, str] | None = None,
                 supports_multimodal: bool = False):
        self.id = id or uuid.uuid4().hex[:10]
        self.name = name or id
        self.kind = kind          # "ollama" | "openai" | "claude"
        self.base_url = base_url  # e.g. http://localhost:11434 / https://api.openai.com/v1
        self.api_key = api_key
        self.enabled = enabled
        self.created_at = created_at or time.time()
        self.models_cache: list[str] = []
        self.models_cache_ts: float = 0.0
        self.manual_models: list[str] = manual_models or []
        self.scope = scope        # "local" | "cloud" | "master_proxy"

        # ── Concurrency & Scheduling ──
        self.max_concurrent = max(1, max_concurrent)  # Provider-level max concurrency
        self.model_concurrency = model_concurrency or {}  # Per-model override: {"qwen3.5": 2}
        self.priority = priority   # Lower = higher priority (for scheduling)
        self.schedule_strategy = schedule_strategy  # "serial"|"concurrent"|"burst"
        self.rate_limit_rpm = rate_limit_rpm  # Requests per minute limit (0=unlimited)
        self.cost_per_1k_tokens = cost_per_1k_tokens  # For cost-aware scheduling

        # ── Fallback Chain ──
        self.fallback_providers = fallback_providers or []  # List of provider IDs to try on failure

        # ── Context Length ──
        self.context_length = context_length  # 0 = auto-detect from model name

        # ── V2 tier → model binding ──
        # Maps a capability tier name (e.g. "coding_strong") to the
        # concrete model this provider serves for that tier. Edited via
        # V2 Providers UI; consulted by V2 through pick_for_tier().
        self.tier_models: dict[str, str] = dict(tier_models or {})

        # ── Multimodal capability flag ──
        # True iff this provider's model(s) accept image/audio parts.
        # Consulted at V2 Intake: a task with attachments but no
        # multimodal provider fails fast with a user-visible clarification.
        self.supports_multimodal = bool(supports_multimodal)

    def get_model_concurrency(self, model: str) -> int:
        """Get concurrency limit for a specific model. Falls back to provider max."""
        return self.model_concurrency.get(model, self.max_concurrent)

    def to_dict(self, mask_key: bool = False) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "base_url": self.base_url,
            "api_key": ("********" if self.api_key else "") if mask_key else self.api_key,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "models_cache": self.models_cache,
            "manual_models": self.manual_models,
            "scope": self.scope,
            "max_concurrent": self.max_concurrent,
            "model_concurrency": self.model_concurrency,
            "priority": self.priority,
            "schedule_strategy": self.schedule_strategy,
            "rate_limit_rpm": self.rate_limit_rpm,
            "cost_per_1k_tokens": self.cost_per_1k_tokens,
            "fallback_providers": self.fallback_providers,
            "context_length": self.context_length,
            "tier_models": dict(self.tier_models),
            "supports_multimodal": self.supports_multimodal,
        }

    @staticmethod
    def from_dict(d: dict) -> "ProviderEntry":
        p = ProviderEntry(
            id=d.get("id", ""),
            name=d.get("name", ""),
            kind=d.get("kind", "openai"),
            base_url=d.get("base_url", ""),
            api_key=d.get("api_key", ""),
            enabled=d.get("enabled", True),
            created_at=d.get("created_at", 0.0),
            manual_models=d.get("manual_models", []),
            scope=d.get("scope", "local"),
            max_concurrent=d.get("max_concurrent", 1),
            model_concurrency=d.get("model_concurrency", {}),
            priority=d.get("priority", 10),
            schedule_strategy=d.get("schedule_strategy", "serial"),
            rate_limit_rpm=d.get("rate_limit_rpm", 0),
            cost_per_1k_tokens=d.get("cost_per_1k_tokens", 0.0),
            fallback_providers=d.get("fallback_providers", []),
            context_length=d.get("context_length", 0),
            tier_models=d.get("tier_models", {}),
            supports_multimodal=d.get("supports_multimodal", False),
        )
        p.models_cache = d.get("models_cache", [])
        return p


def _is_ghost_provider_dict(d: dict) -> bool:
    """A provider is a 'ghost' if it has no way to actually do anything:
    no base_url, no api_key, and no models cached/manual. These rows come
    from buggy callers that construct ``ProviderEntry()`` empty or
    persist a partial dict. Load paths skip them to keep the registry
    from filling with placeholders on every restart.
    """
    base = (d.get("base_url") or "").strip()
    key = (d.get("api_key") or "").strip()
    models = (d.get("models_cache") or []) or (d.get("manual_models") or [])
    return not base and not key and not models


class ProviderRegistry:
    """Dynamic registry of LLM providers, persisted to JSON."""

    def __init__(self, data_dir: str = ""):
        self._providers: dict[str, ProviderEntry] = {}
        self._lock = threading.Lock()
        from . import DEFAULT_DATA_DIR
        self._data_dir = data_dir or DEFAULT_DATA_DIR
        self._file = os.path.join(self._data_dir, "providers.json")
        self._load()

    # ---- Persistence ----

    def _get_db(self):
        try:
            from .database import get_database
            return get_database()
        except Exception:
            return None

    def _load(self):
        """Load providers from DB (primary) or JSON (fallback).

        Skips rows that would materialise as ghost providers —
        i.e. empty id/name/base_url/api_key. The DB's ``data`` column
        used to sometimes be NULL (upsert path bug), which caused
        ``from_dict({})`` to generate a fresh uuid and persist a
        placeholder row on every restart.
        """
        db = self._get_db()
        if db and db.count("providers") > 0:
            try:
                for d in db.load_providers():
                    # DB row_to_dict merges the `data` blob. If blob is NULL
                    # or missing an 'id'/'name' we only have the `provider_id`
                    # column — fall back to that, and skip truly empty rows.
                    if not d.get("id") and d.get("provider_id"):
                        d["id"] = d["provider_id"]
                    if not (d.get("id") or "").strip():
                        logger.warning("Skipping ghost provider row (no id): %r",
                                       {k: d.get(k) for k in ("provider_id", "name")})
                        continue
                    if _is_ghost_provider_dict(d):
                        logger.warning("Skipping ghost provider %r: no base_url/api_key/models",
                                       d.get("id"))
                        continue
                    p = ProviderEntry.from_dict(d)
                    self._providers[p.id] = p
                return
            except Exception:
                pass
        if not os.path.exists(self._file):
            self._seed_defaults()
            return
        try:
            with open(self._file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for d in data.get("providers", []):
                if not (d.get("id") or "").strip():
                    continue
                if _is_ghost_provider_dict(d):
                    logger.warning("Skipping ghost provider %r from JSON", d.get("id"))
                    continue
                p = ProviderEntry.from_dict(d)
                self._providers[p.id] = p
        except Exception:
            self._seed_defaults()

    def _save(self):
        """Persist providers to DB + JSON. Ghost entries (empty
        base_url / api_key / models) are filtered out and logged so
        they disappear on the next flush even if something upstream
        snuck one into memory.
        """
        os.makedirs(self._data_dir, exist_ok=True)
        real: list[ProviderEntry] = []
        for p in self._providers.values():
            if _is_ghost_provider_dict(p.to_dict()):
                logger.warning(
                    "Dropping ghost provider on save: id=%s name=%s",
                    p.id, p.name,
                )
                continue
            real.append(p)
        # Evict ghosts from memory too so future reads are consistent.
        if len(real) != len(self._providers):
            self._providers = {p.id: p for p in real}

        db = self._get_db()
        if db:
            try:
                for p in real:
                    db.save_provider(p.to_dict())
            except Exception:
                pass
        data = {"providers": [p.to_dict() for p in real]}
        try:
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _seed_defaults(self):
        """Create default providers from config.yaml on first run."""
        cfg = _load_config()
        defaults = [
            ProviderEntry(
                id="ollama", name="Ollama (local)",
                kind="ollama",
                base_url=cfg.get("ollama_url", "http://localhost:11434"),
                api_key="", enabled=True,
            ),
            ProviderEntry(
                id="openai", name="OpenAI",
                kind="openai",
                base_url=cfg.get("openai_base_url", "https://api.openai.com/v1"),
                api_key=cfg.get("openai_api_key", ""),
                enabled=True,
            ),
            ProviderEntry(
                id="claude", name="Claude (Anthropic)",
                kind="claude",
                base_url="https://api.anthropic.com",
                api_key=cfg.get("claude_api_key", ""),
                enabled=True,
            ),
            ProviderEntry(
                id="unsloth", name="Unsloth / vLLM (local)",
                kind="openai",
                base_url=cfg.get("unsloth_base_url", "http://localhost:8888/v1"),
                api_key=cfg.get("unsloth_api_key", ""),
                enabled=True,
            ),
        ]
        for p in defaults:
            self._providers[p.id] = p
        self._save()

    # ---- CRUD ----

    def list(self, include_disabled: bool = False) -> list[ProviderEntry]:
        with self._lock:
            providers = list(self._providers.values())
        if not include_disabled:
            providers = [p for p in providers if p.enabled]
        return providers

    def get(self, provider_id: str) -> ProviderEntry | None:
        return self._providers.get(provider_id)

    def get_fallback_chain(self, provider_id: str) -> list[ProviderEntry]:
        """
        Return the fallback chain for a provider.

        Returns [primary, fallback1, fallback2, ...] where:
        - primary is the requested provider
        - fallback1, fallback2... are the configured fallback providers (if enabled)

        Returns empty list if primary provider not found.
        """
        primary = self.get(provider_id)
        if not primary:
            return []

        chain = [primary]
        for fb_id in (primary.fallback_providers or []):
            fb = self.get(fb_id)
            if fb and fb.enabled:
                chain.append(fb)

        return chain

    def add(self, *, name: str, kind: str, base_url: str,
            api_key: str = "", enabled: bool = True, manual_models: list[str] | None = None,
            scope: str = "local", max_concurrent: int = 1,
            model_concurrency: dict[str, int] | None = None,
            schedule_strategy: str = "serial",
            rate_limit_rpm: int = 0,
            fallback_providers: list[str] | None = None) -> ProviderEntry:
        # Validation: every provider needs at minimum a non-empty name
        # AND one of {base_url, api_key}. Without this the registry
        # fills with ghost entries named after random uuid4 prefixes
        # (caller passed name="" → ProviderEntry auto-IDed from uuid →
        # to_dict() showed id==name and an empty base, unusable).
        _name = (name or "").strip()
        if not _name:
            raise ValueError("Provider name is required (non-empty)")
        if not (base_url or "").strip() and not (api_key or "").strip():
            raise ValueError(
                "Provider must have at least a base_url or api_key — "
                "empty placeholder providers clutter the registry")
        p = ProviderEntry(name=_name, kind=kind, base_url=base_url,
                          api_key=api_key, enabled=enabled, manual_models=manual_models,
                          scope=scope, max_concurrent=max_concurrent,
                          model_concurrency=model_concurrency,
                          schedule_strategy=schedule_strategy,
                          rate_limit_rpm=rate_limit_rpm,
                          fallback_providers=fallback_providers)
        with self._lock:
            self._providers[p.id] = p
            self._save()
        return p

    def update(self, provider_id: str, **kwargs) -> ProviderEntry | None:
        with self._lock:
            p = self._providers.get(provider_id)
            if not p:
                return None
            for k, v in kwargs.items():
                if k in ("name", "kind", "base_url", "api_key", "enabled",
                        "manual_models", "scope", "fallback_providers",
                        "tier_models", "priority", "max_concurrent",
                        "schedule_strategy", "rate_limit_rpm",
                        "cost_per_1k_tokens", "context_length",
                        "supports_multimodal"):
                    setattr(p, k, v)
            self._save()
            return p

    # ---- V2 tier resolution (legacy tier_models on ProviderEntry) ----
    # Note: the richer tier system lives in app.llm_tier_routing. This
    # method is kept so V2 can fall back to per-provider tier_models dicts
    # for anyone who configured providers before the new router existed.

    def pick_for_tier(self, tier: str) -> tuple[ProviderEntry, str] | None:
        """Find the best provider+model serving a V2 capability tier.

        Scans enabled providers; the one with the lowest ``priority`` value
        whose ``tier_models`` contains ``tier`` wins. Returns
        ``(provider_entry, model_name)`` or ``None`` if no provider is
        configured for this tier via the legacy path.
        """
        if not tier:
            return None
        candidates: list[tuple[int, ProviderEntry, str]] = []
        with self._lock:
            providers = list(self._providers.values())
        for p in providers:
            if not p.enabled:
                continue
            model = (p.tier_models or {}).get(tier)
            if model:
                candidates.append((p.priority, p, model))
        if not candidates:
            return None
        candidates.sort(key=lambda t: t[0])
        _, entry, model = candidates[0]
        return entry, model

    def provider_supports_multimodal(self, provider_id: str) -> bool:
        """True iff the provider has ``supports_multimodal=True``.

        Used by V2 Intake to decide whether to accept attachments on a
        task. Unknown / disabled provider → False (fail-closed)."""
        if not provider_id:
            return False
        p = self.get(provider_id)
        return bool(p and p.enabled and p.supports_multimodal)

    def remove(self, provider_id: str) -> bool:
        with self._lock:
            if provider_id in self._providers:
                del self._providers[provider_id]
                self._save()
                return True
        return False

    # ---- Model detection ----

    def detect_models(self, provider_id: str,
                      timeout: float = 10) -> list[str]:
        """Auto-detect available models from a provider endpoint."""
        p = self.get(provider_id)
        if not p:
            return []
        models = _detect_models_for_entry(p, timeout=timeout)
        # Merge with manual models
        models = list(set(models + p.manual_models))
        with self._lock:
            p.models_cache = models
            p.models_cache_ts = time.time()
            self._save()
        return models

    def get_all_models(self) -> dict[str, list[str]]:
        """Return {provider_id: [model_name, ...]} for all enabled providers."""
        result = {}
        for p in self.list():
            result[p.id] = list(p.models_cache) if p.models_cache else []
        return result

    def detect_all_models(self, timeout: float = 8) -> dict[str, list[str]]:
        """Detect models for all enabled providers in parallel."""
        providers = self.list()
        result: dict[str, list[str]] = {}
        threads = []

        def _detect(prov: ProviderEntry):
            try:
                models = _detect_models_for_entry(prov, timeout=timeout)
                # Merge with manual models
                models = list(set(models + prov.manual_models))
                with self._lock:
                    prov.models_cache = models
                    prov.models_cache_ts = time.time()
                result[prov.id] = models
            except Exception:
                result[prov.id] = list(prov.models_cache)

        for p in providers:
            t = threading.Thread(target=_detect, args=(p,))
            t.start()
            threads.append(t)

        for t in threads:
            t.join(timeout=timeout + 2)

        # Save all at once
        with self._lock:
            self._save()

        return result


def detect_context_length(p: ProviderEntry, model: str = "", timeout: float = 8) -> int:
    """Probe a provider endpoint for the model's actual context window size.

    Tries multiple strategies:
    1. GET /v1/models — LM Studio returns max_model_len or context_length per model
    2. Ollama GET /api/show — returns context_length in model info
    3. Fall back to 0 (meaning: use heuristic from model name)

    Result is cached on the ProviderEntry.context_length field if > 0.
    """
    if p.context_length > 0:
        return p.context_length  # User-configured, don't override

    base = p.base_url.rstrip("/")
    headers = {}
    if p.api_key:
        headers["Authorization"] = f"Bearer {p.api_key}"

    # Strategy 1: OpenAI-compatible /v1/models (LM Studio, vLLM, etc.)
    if p.kind in ("openai", "ollama"):
        root = base.rstrip("/v1").rstrip("/")
        for suffix in ("/v1/models", "/models"):
            try:
                resp = requests.get(root + suffix, headers=headers, timeout=timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    for m in data.get("data", []):
                        mid = m.get("id", "")
                        # Match the requested model or take the first one
                        if model and mid != model and model not in mid:
                            continue
                        # LM Studio: max_model_len, vLLM: max_model_len
                        for key in ("max_model_len", "context_length", "max_tokens",
                                    "context_window", "max_context_length"):
                            val = m.get(key)
                            if val and isinstance(val, (int, float)) and val > 0:
                                ctx = int(val)
                                logger.info("Auto-detected context_length=%d for model '%s' on provider '%s'",
                                           ctx, mid, p.id)
                                return ctx
            except Exception:
                continue

    # Strategy 2: Ollama /api/show
    if p.kind == "ollama" and model:
        try:
            resp = requests.post(f"{base}/api/show",
                                json={"name": model},
                                headers=headers, timeout=timeout)
            if resp.status_code == 200:
                info = resp.json()
                # Ollama returns model_info with context_length
                model_info = info.get("model_info", {})
                for key in ("context_length", "max_model_len"):
                    val = model_info.get(key)
                    if val and isinstance(val, (int, float)) and val > 0:
                        ctx = int(val)
                        logger.info("Auto-detected context_length=%d for model '%s' (Ollama)",
                                   ctx, model)
                        return ctx
                # Also check parameters string
                params = info.get("parameters", "")
                if "num_ctx" in params:
                    import re
                    match = re.search(r"num_ctx\s+(\d+)", params)
                    if match:
                        ctx = int(match.group(1))
                        logger.info("Auto-detected context_length=%d from Ollama parameters", ctx)
                        return ctx
        except Exception:
            pass

    return 0  # Unknown — caller will use model name heuristic


def _detect_models_for_entry(p: ProviderEntry, timeout: float = 10) -> list[str]:
    """Probe a provider endpoint for available models."""
    base = p.base_url.rstrip("/")
    headers = {}
    if p.api_key:
        headers["Authorization"] = f"Bearer {p.api_key}"

    if p.kind == "ollama":
        # Ollama: GET /api/tags
        try:
            resp = requests.get(f"{base}/api/tags",
                                headers=headers, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            return [m["name"] for m in data.get("models", [])]
        except Exception:
            return []

    elif p.kind == "openai":
        # OpenAI-compatible: GET /models  (or /v1/models)
        root = base.rstrip("/v1").rstrip("/")
        for suffix in ("/v1/models", "/models"):
            try:
                url = root + suffix
                resp = requests.get(url, headers=headers, timeout=timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    models_list = data.get("data", [])
                    found = sorted([m["id"] for m in models_list if m.get("id")])
                    if found:
                        return found
            except Exception:
                continue
        # Fallback: try /api/tags (some local servers use Ollama format)
        try:
            resp = requests.get(f"{root}/api/tags",
                                headers=headers, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                found = [m["name"] for m in data.get("models", [])]
                if found:
                    return found
        except Exception:
            pass
        # Fallback: try /api/models (some local inference UIs)
        try:
            resp = requests.get(f"{root}/api/models",
                                headers=headers, timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    return [m.get("id", m.get("name", str(m))) for m in data]
                elif isinstance(data, dict):
                    for key in ("data", "models", "model_list"):
                        if key in data and isinstance(data[key], list):
                            return [m.get("id", m.get("name", str(m)))
                                    for m in data[key] if isinstance(m, dict)]
        except Exception:
            pass
        return []

    elif p.kind == "claude":
        # Anthropic doesn't have a model listing endpoint — return well-known models
        return [
            "claude-opus-4-20250514",
            "claude-sonnet-4-20250514",
            "claude-haiku-4-20250414",
        ]

    return []


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_registry: ProviderRegistry | None = None
_registry_lock = threading.Lock()


def get_registry() -> ProviderRegistry:
    global _registry
    if _registry is None:
        with _registry_lock:
            if _registry is None:
                from . import DEFAULT_DATA_DIR
                _registry = ProviderRegistry(data_dir=DEFAULT_DATA_DIR)
    return _registry


def init_registry(data_dir: str = "") -> ProviderRegistry:
    global _registry
    with _registry_lock:
        _registry = ProviderRegistry(data_dir=data_dir)
    return _registry


# ---------------------------------------------------------------------------
# Convenience wrappers (backward compat)
# ---------------------------------------------------------------------------

def list_providers() -> list[str]:
    """Return list of enabled provider IDs."""
    return [p.id for p in get_registry().list()]


def list_available_models() -> dict[str, list[str]]:
    """Return {provider_id: [model, ...]} from cache."""
    return get_registry().get_all_models()


# ---------------------------------------------------------------------------
# Proxy chat handler for distributed architecture
# ---------------------------------------------------------------------------

def _proxy_chat(base_url: str, api_key: str,
                messages: list[dict], tools: list[dict] | None = None,
                stream: bool = False, model: str = "",
                _provider_id: str = "",
                tool_choice: dict | str | None = None,
                temperature: float | None = None,
                response_format: dict | None = None,
                ) -> dict | Generator:
    """
    Handle proxy requests through Master node via WebSocket bus.

    For distributed mode: sends llm.proxy_request through WS bus to Master,
    Master executes the LLM call and streams chunks back.
    Falls back to direct execution if not in distributed mode.
    """
    try:
        from .ws_bus import get_ws_client, is_distributed, MessageType
    except ImportError:
        # ws_bus not available, shouldn't happen in normal operation
        raise RuntimeError("Distributed mode requires ws_bus module")

    if not is_distributed():
        raise RuntimeError(
            f"Provider {_provider_id!r} configured for master_proxy scope "
            "but system is not in distributed mode"
        )

    ws_client = get_ws_client()
    if ws_client is None:
        raise RuntimeError("WebSocket client not initialized for proxy requests")

    # Prepare proxy request
    request_payload = {
        "provider_id": _provider_id,
        "model": model,
        "messages": messages,
        "tools": tools,
        "stream": stream,
    }
    # Forward temperature so the master-side handler can apply it to
    # the actual provider call. Master may ignore it if its protocol
    # handler is older than this field; that's a silent fallback to
    # provider default, which matches the non-distributed behavior.
    if temperature is not None and temperature >= 0:
        request_payload["temperature"] = float(temperature)

    if not stream:
        # Non-streaming: send request, wait for response
        try:
            response = ws_client.send_request(
                MessageType.LLM_PROXY_REQUEST,
                request_payload,
                timeout=120
            )
            if response is None:
                raise RuntimeError("No response from master proxy")

            # Verify response format and return
            if isinstance(response, dict):
                return response
            else:
                raise RuntimeError(f"Unexpected proxy response type: {type(response)}")
        except Exception as e:
            logger.error(f"Proxy chat request failed: {e}")
            raise
    else:
        # Streaming: send request, yield chunks
        def chunk_generator():
            try:
                for chunk in ws_client.stream_request(
                    MessageType.LLM_PROXY_REQUEST,
                    request_payload
                ):
                    if chunk is None:
                        break
                    # chunk should be a string or dict with "chunk" key
                    if isinstance(chunk, str):
                        yield chunk
                    elif isinstance(chunk, dict) and "chunk" in chunk:
                        yield chunk["chunk"]
                    else:
                        logger.warning(f"Unexpected chunk format: {chunk}")
            except Exception as e:
                logger.error(f"Proxy streaming failed: {e}")
                raise

        return chunk_generator()


# ---------------------------------------------------------------------------
# Protocol handlers — each handles one wire format
# ---------------------------------------------------------------------------

def _ollama_chat(base_url: str, api_key: str,
                 messages: list[dict], tools: list[dict] | None = None,
                 stream: bool = False, model: str = "",
                 _provider_id: str = "ollama",
                 tool_choice: dict | str | None = None,
                 temperature: float | None = None,
                 response_format: dict | None = None,
                 ) -> dict | Generator:
    """Ollama — 直接走 OpenAI 兼容端点 /v1/chat/completions。

    不维护独立的协议处理。Ollama 本身实现了完整的 OpenAI 兼容 API，
    tool_calls 按标准格式返回，一套解析代码适配所有模型。
    """
    return _openai_chat(
        base_url, api_key or "ollama",
        messages, tools=tools, stream=stream,
        model=model, _provider_id=_provider_id,
        tool_choice=tool_choice,
        temperature=temperature,
        response_format=response_format,
    )


def _apply_model_directives(messages: list[dict], model: str) -> list[dict]:
    """Apply model-specific directives to messages.

    This is the ONLY place for model-variant handling in the entire LLM stack.
    All directives are injected as standard system/user messages — no protocol
    changes, no provider-specific code paths.

    Currently handled:
      - Qwen3/3.5: inject /no_think to disable extended thinking (slow)
    """
    model_lower = (model or "").lower()

    # Qwen3/3.5: disable /think mode unless user explicitly requests it
    if "qwen3" in model_lower:
        has_think_directive = any(
            ("/think" in _ensure_str(m.get("content"))
             or "/no_think" in _ensure_str(m.get("content")))
            for m in messages
        )
        if not has_think_directive:
            messages = list(messages)
            if messages and messages[0].get("role") == "system":
                messages[0] = dict(messages[0])
                messages[0]["content"] = "/no_think\n" + _ensure_str(
                    messages[0].get("content"))
            else:
                messages.insert(0, {"role": "system", "content": "/no_think"})

    return messages


def _openai_chat(base_url: str, api_key: str,
                 messages: list[dict], tools: list[dict] | None = None,
                 stream: bool = False, model: str = "",
                 _provider_id: str = "openai",
                 tool_choice: dict | str | None = None,
                 temperature: float | None = None,
                 response_format: dict | None = None,
                 ) -> dict | Generator:
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        # Append /v1 only if no version path already present (e.g. /v1, /v3, /api/v3)
        if not re.search(r'/v\d+', url):
            url += "/v1"
        url += "/chat/completions"

    pool = get_connection_pool()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key or 'no-key'}",
    }
    _TIMEOUT = _REQUEST_TIMEOUT

    # Apply model-specific directives (e.g. Qwen3 /no_think)
    messages = _apply_model_directives(messages, model)

    # Sanitize messages: ensure all content fields are plain strings
    # (local models like Qwen/LM Studio reject list-type content with 400)
    # Pass URL so the sanitizer can apply provider-specific quirks (the
    # only place that knows about provider differences). Caller stays
    # provider-agnostic — switching providers requires zero call-site
    # changes.
    safe_messages = _sanitize_messages_for_openai(
        messages, target_url=url, target_model=model
    )

    # ── HARD GUARD: drop orphan tool messages + strip empty tool_calls ──
    # Inline because reload bugs / stale pyc could leave the
    # _sanitize_messages_for_openai version stale. This is THE last
    # chance before HTTP send. Catches:
    #   1. Orphan tool message (no preceding assistant.tool_calls)
    #   2. assistant with `tool_calls: []` (DeepSeek rejects: "empty array")
    _seen_ids: set = set()
    _kept: list = []
    _drop_n = 0
    _drop_ids: list = []
    _empty_tcs_fixed = 0
    for _m in safe_messages:
        _r = _m.get("role")
        if _r == "assistant":
            for _tc in (_m.get("tool_calls") or []):
                if isinstance(_tc, dict):
                    _id = _tc.get("id") or ""
                    if _id:
                        _seen_ids.add(_id)
            # Strip empty tool_calls field — strict APIs reject `[]`
            if "tool_calls" in _m and not _m.get("tool_calls"):
                _m = dict(_m)
                _m.pop("tool_calls", None)
                _empty_tcs_fixed += 1
            _kept.append(_m)
        elif _r == "tool":
            _tid = _m.get("tool_call_id") or ""
            if _tid and _tid in _seen_ids:
                _kept.append(_m)
            else:
                _drop_n += 1
                _drop_ids.append(_tid or "<empty>")
        else:
            _kept.append(_m)
    if _drop_n:
        logger.warning(
            "[hard-guard] dropped %d orphan tool message(s) right before "
            "HTTP send — ids=%s. This is the last-line defense; check why "
            "earlier sanitizer didn't catch it.",
            _drop_n, _drop_ids,
        )
    if _empty_tcs_fixed:
        logger.warning(
            "[hard-guard] stripped empty tool_calls=[] from %d assistant "
            "message(s) before HTTP send (DeepSeek rejects empty array)",
            _empty_tcs_fixed,
        )
    if _drop_n or _empty_tcs_fixed:
        safe_messages = _kept

    valid_tools = _validate_tools(tools)

    # Log multimodal content status for debugging
    _mm_parts = sum(
        1 for m in safe_messages if isinstance(m.get("content"), list)
        for p in (m["content"] if isinstance(m.get("content"), list) else [])
        if isinstance(p, dict) and p.get("type") in ("image_url", "image", "input_image")
    )
    if _mm_parts:
        logger.info(
            "LLM MULTIMODAL: sending %d image part(s) to %s model=%s url=%s",
            _mm_parts, _provider_id, model, url,
        )

    payload: dict = {
        "model": model,
        "messages": safe_messages,
        "stream": stream,
    }
    # Temperature: only inject when caller passed a non-negative value
    # AND the provider declares it accepts the field. -1.0 (or None) means
    # "use provider default" — and o1-class models reject the field
    # entirely (provider sets supports_temperature_param=False).
    if (temperature is not None and temperature >= 0
            and bool(getattr(resolve_strategy(url), "supports_temperature_param", True))):
        payload["temperature"] = float(temperature)
    # Structured output forcing — caller passes either:
    #   {"type": "json_object"}                — any valid JSON
    #   {"type": "json_schema",
    #    "json_schema": {"name": ..., "schema": {...}, "strict": true}}
    # OpenAI / DeepSeek / Doubao / Qwen all honour this. Local models
    # (Ollama / LM Studio) often ignore it (best-effort). Tools and
    # response_format are mutually exclusive for some providers; caller
    # is responsible for picking one.
    if response_format and isinstance(response_format, dict):
        payload["response_format"] = response_format
    if valid_tools:
        payload["tools"] = valid_tools
        payload["stream"] = False
        stream = False
        # Ensure sufficient output length for tool_call arguments.
        # write_file/edit_file can produce large argument JSON. Many
        # providers default to 1024–2048 tokens which causes truncation
        # (finish_reason=length) and empty arguments.
        payload.setdefault("max_tokens", 16384)
        # Encourage parallel tool calling — model returns multiple
        # tool_calls in ONE assistant message when calls are independent.
        # Only send the field for providers that recognize it; GLM/zhipu
        # 400s on unknown fields ("messages 参数非法").
        if resolve_strategy(url).supports_parallel_tool_calls_param:
            payload["parallel_tool_calls"] = True
        # Per-call payload breakdown (system / history / tools split).
        # Anomaly threshold logs at INFO; everything else at DEBUG.
        _log_payload_breakdown(
            safe_messages, valid_tools,
            agent_id=getattr(_TOKEN_CTX, "agent_id", "") or "",
            model=model,
        )
        # Plan D: forced tool_choice — when caller detects a handoff
        # trigger, lock the model to the handoff_request tool so it
        # cannot fabricate a fake result in plain text.
        # OpenAI spec accepts: "none" | "auto" | "required" |
        #   {"type":"function","function":{"name":"..."}}
        if tool_choice:
            payload["tool_choice"] = tool_choice

    if not stream:
        resp = pool.request_with_retry(
            _provider_id, "POST", url, model=model, headers=headers,
            json=payload, timeout=_TIMEOUT)
        if resp.status_code >= 400:
            try:
                err_body = resp.json()
                err_msg = err_body.get("error", {})
                if isinstance(err_msg, dict):
                    err_msg = err_msg.get("message", str(err_body)[:500])
                else:
                    err_msg = str(err_msg)[:500]
            except Exception:
                err_msg = resp.text[:500]
            # ── DeepSeek thinking-mode auto-recovery ──
            # Error: "The reasoning_content in the thinking mode must be
            # passed back". We can't always guarantee reasoning_content
            # survives history transforms (memory compression, /new reset,
            # cross-provider history from glm, etc.). Fallback: retry
            # ONCE with history pruned to system + last user message —
            # loses context but at least doesn't break the chat.
            #
            # When auto-recovery is attempted the FIRST-ATTEMPT error is
            # expected/handled, so log at DEBUG, not ERROR. Only promote
            # to ERROR if recovery also fails.
            is_recoverable = (
                resp.status_code == 400
                and "reasoning_content" in str(err_msg).lower()
            )
            if is_recoverable:
                logger.debug(
                    "OpenAI-compat %d (recoverable, model=%s): %s",
                    resp.status_code, model, err_msg)
                logger.info(
                    "DeepSeek thinking-mode: history missing "
                    "reasoning_content — retrying with trimmed messages "
                    "(system + last user)."
                )
                trimmed: list[dict] = []
                # keep system messages
                for m in safe_messages:
                    if m.get("role") == "system":
                        trimmed.append(m)
                # keep only the last user message
                for m in reversed(safe_messages):
                    if m.get("role") == "user":
                        trimmed.append(m)
                        break
                payload["messages"] = trimmed
                resp2 = pool.request_with_retry(
                    _provider_id, "POST", url, model=model,
                    headers=headers, json=payload, timeout=_TIMEOUT)
                if resp2.status_code < 400:
                    resp = resp2
                else:
                    # Recovery also failed — NOW log the original error
                    # as ERROR so ops can see it.
                    logger.error(
                        "OpenAI-compat %d error after recovery retry "
                        "(model=%s, url=%s): %s",
                        resp.status_code, model, url, err_msg)
                    resp.raise_for_status()
            else:
                # Non-recoverable — log ERROR and raise.
                logger.error(
                    "OpenAI-compat %d error (model=%s, url=%s): %s",
                    resp.status_code, model, url, err_msg)
                # Dump the offending payload's message shape so we can debug
                # "messages 参数非法" / "invalid messages" errors. ERROR level
                # so it shows up without enabling DEBUG.
                try:
                    _shape = []
                    for _idx, _m in enumerate(payload.get("messages", [])):
                        _r = _m.get("role")
                        _entry = {"i": _idx, "role": _r}
                        _c = _m.get("content")
                        if _c is None:
                            _entry["content"] = None
                        elif isinstance(_c, list):
                            _entry["content_kind"] = "list[" + ",".join(
                                str(p.get("type")) if isinstance(p, dict)
                                else type(p).__name__
                                for p in _c
                            ) + "]"
                        elif isinstance(_c, str):
                            _entry["content_len"] = len(_c)
                            if not _c.strip():
                                _entry["content_empty"] = True
                        else:
                            _entry["content_type"] = type(_c).__name__
                        if "tool_calls" in _m:
                            tcs = _m.get("tool_calls")
                            if tcs is None:
                                _entry["tool_calls"] = "None"
                            elif not tcs:
                                _entry["tool_calls"] = "[]"
                            else:
                                _entry["tool_calls_n"] = len(tcs)
                                # arguments shape
                                first = tcs[0] if tcs else {}
                                if isinstance(first, dict):
                                    _id = first.get("id") or ""
                                    _entry["tc0_id"] = (str(_id)[:14] or "<empty>")
                                    fn = first.get("function", {})
                                    if isinstance(fn, dict):
                                        _entry["tc0_name"] = fn.get("name", "?")
                                        _entry["tc0_args_type"] = type(
                                            fn.get("arguments")).__name__
                        if "tool_call_id" in _m:
                            _tcid = _m.get("tool_call_id") or ""
                            _entry["tool_call_id"] = (str(_tcid)[:14] or "<empty>")
                        for _k in ("name", "reasoning_content"):
                            if _k in _m:
                                _entry[_k + "_present"] = True
                        # Surface any non-standard keys the sanitizer missed
                        _known = {"role", "content", "tool_calls",
                                  "tool_call_id", "name", "reasoning_content",
                                  "refusal"}
                        _extra = sorted(set(_m.keys()) - _known)
                        if _extra:
                            _entry["extra_keys"] = _extra
                        _shape.append(_entry)
                    logger.error("400 payload shape (n=%d): %s",
                                 len(_shape), _shape)
                    # Top-level payload keys (tools array length, etc.)
                    _top = {}
                    for _tk, _tv in payload.items():
                        if _tk == "messages":
                            continue
                        if isinstance(_tv, list):
                            _top[_tk] = f"list[{len(_tv)}]"
                            if _tk == "tools" and _tv:
                                _t0 = _tv[0]
                                if isinstance(_t0, dict):
                                    _fn = _t0.get("function", {})
                                    _top["tools[0].name"] = (
                                        _fn.get("name") if isinstance(_fn, dict) else "?")
                        elif isinstance(_tv, (str, int, float, bool)):
                            _top[_tk] = _tv if not isinstance(_tv, str) else _tv[:60]
                        else:
                            _top[_tk] = type(_tv).__name__
                    logger.error("400 payload top-level: %s", _top)
                    # Full GLM/zhipu response body — they often name the bad field.
                    try:
                        logger.error("400 response body: %s", resp.text[:2000])
                    except Exception:
                        pass
                except Exception:
                    pass
                resp.raise_for_status()
        data = resp.json()
        # ── Token usage logging ──
        try:
            _usage = data.get("usage") or {}
            _cr, _cw = _extract_cache_tokens(_usage)
            _log_token_usage(
                provider=_provider_id or "openai",
                model=model,
                prompt_tokens=_usage.get("prompt_tokens", 0),
                completion_tokens=_usage.get("completion_tokens", 0),
                stream=False,
                cache_read=_cr,
                cache_write=_cw,
            )
        except Exception as _te:
            logger.debug("token usage log failed: %s", _te)
        choice = data["choices"][0]
        finish_reason = choice.get("finish_reason", "") or ""
        msg = choice["message"]
        # Normalize finish_reason → canonical stop_reason.  Canonical set:
        #   end_turn       — model finished normally
        #   tool_use       — model wants to call tools (tool_calls populated)
        #   length         — output truncated (max_tokens / context window)
        #   stop_sequence  — hit a configured stop string
        #   content_filter — provider-side safety block
        _OPENAI_STOP_MAP = {
            "stop": "end_turn",
            "length": "length",
            "tool_calls": "tool_use",
            "function_call": "tool_use",
            "content_filter": "content_filter",
        }
        stop_reason = _OPENAI_STOP_MAP.get(finish_reason, finish_reason or "end_turn")
        result: dict = {"message": {"role": "assistant",
                                     "content": msg.get("content", "") or ""},
                         "stop_reason": stop_reason}
        # DeepSeek thinking-mode: surface reasoning_content so caller can
        # pass it back on the next turn (required by their API).
        if msg.get("reasoning_content"):
            result["message"]["reasoning_content"] = msg["reasoning_content"]
        # Detect truncation: finish_reason "length" means output was cut off
        if finish_reason == "length" and msg.get("tool_calls"):
            logger.warning(
                "LLM response TRUNCATED (finish_reason=length, model=%s) "
                "— tool_call arguments may be incomplete. Consider "
                "increasing max_tokens or simplifying the request.",
                model,
            )
        if msg.get("tool_calls"):
            result["message"]["tool_calls"] = []
            for idx, tc in enumerate(msg["tool_calls"]):
                func = tc.get("function")
                if not isinstance(func, dict) or not func:
                    # Some models (e.g. Qwen) may put name/arguments at top level
                    func = {"name": tc.get("name", "unknown"),
                            "arguments": tc.get("arguments", {})}
                args = func.get("arguments", {})
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except (json.JSONDecodeError, TypeError):
                        logger.warning(
                            "tool_call[%d] '%s': failed to parse arguments "
                            "JSON (len=%d, preview=%.120s) — possible "
                            "truncation, falling back to empty args",
                            idx, func.get("name", "?"), len(args),
                            args[:120] if args else "(empty)",
                        )
                        args = {}
                name = func.get("name", "unknown")
                if name and name != "unknown":
                    # Preserve original id or generate one
                    call_id = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
                    result["message"]["tool_calls"].append({
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args),
                        }
                    })
        return result
    else:
        def _gen():
            session = pool.get_session(_provider_id)
            max_stream_retries = pool.max_retries
            for attempt in range(max_stream_retries + 1):
                pool.acquire_slot(_provider_id, model)
                released = False
                try:
                    with session.post(url, headers=headers, json=payload,
                                      stream=True, timeout=_TIMEOUT) as resp:
                        if resp.status_code == 429:
                            retry_after = resp.headers.get("Retry-After")
                            wait = float(retry_after) if retry_after else (
                                pool.backoff_factor * (2 ** attempt))
                            wait = min(wait, 60)
                            logger.warning(
                                "OpenAI stream 429 (attempt %d/%d), "
                                "retrying in %.1fs...",
                                attempt + 1, max_stream_retries + 1, wait)
                            pool.release_slot(_provider_id, model)
                            released = True
                            time.sleep(wait)
                            continue
                        resp.raise_for_status()
                        for line in resp.iter_lines():
                            if not line:
                                continue
                            text = line.decode("utf-8")
                            if text.startswith("data: "):
                                text = text[6:]
                            if text.strip() == "[DONE]":
                                break
                            try:
                                chunk = json.loads(text)
                                delta = chunk["choices"][0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    yield content
                            except (json.JSONDecodeError, KeyError, IndexError):
                                continue
                    return  # 成功
                except (requests.ConnectionError, requests.Timeout) as e:
                    logger.warning(
                        "OpenAI stream connection error (attempt %d/%d): %s",
                        attempt + 1, max_stream_retries + 1, str(e)[:100])
                    if attempt < max_stream_retries:
                        pool.release_slot(_provider_id, model)
                        released = True
                        wait = pool.backoff_factor * (2 ** attempt)
                        time.sleep(wait)
                        continue
                    raise
                finally:
                    if not released:
                        pool.release_slot(_provider_id, model)
        return _gen()


# ---------------------------------------------------------------------------
# Stream-events parsers — yield provider-agnostic event dicts with tool_use
# support. Event schema (see chat_stream_events() for full contract):
#   {"type": "text_delta",          "text": "..."}
#   {"type": "tool_use_start",      "id": "...", "name": "..."}
#   {"type": "tool_input_delta",    "id": "...", "partial_json": "..."}
#   {"type": "tool_use_complete",   "id": "...", "name": "...", "input": {}}
#   {"type": "usage",               "input_tokens": N, "output_tokens": M}
#   {"type": "stop",                "reason": "end_turn"|"tool_use"|"length"}
#   {"type": "error",               "message": "..."}
# ---------------------------------------------------------------------------

def _openai_stream_events(base_url: str, api_key: str,
                          messages: list[dict],
                          tools: list[dict] | None = None,
                          model: str = "",
                          _provider_id: str = "openai",
                          temperature: float | None = None,
                          ) -> Generator[dict, None, None]:
    """OpenAI-compat streaming with tool_calls delta support.

    Covers OpenAI, Ollama, MLX, Unsloth/vLLM, LM Studio — any provider that
    speaks the /v1/chat/completions protocol. Yields event dicts (see schema
    above) rather than raw text chunks, preserving tool_use decisions.

    Defensive quirks:
      - Some local servers (MLX, certain Ollama builds) emit tool_calls all
        at once at stream end rather than incrementally; this parser handles
        both patterns by finalising any still-open tool_call state when the
        stream ends.
      - `stream_options.include_usage` is requested but providers that
        ignore or reject it just get no usage event (non-fatal).
    """
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        if not re.search(r'/v\d+', url):
            url += "/v1"
        url += "/chat/completions"

    pool = get_connection_pool()
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key or 'no-key'}",
    }

    messages = _apply_model_directives(messages, model)
    safe_messages = _sanitize_messages_for_openai(
        messages, target_url=url, target_model=model
    )

    # ── HARD GUARD: orphan tool + empty tool_calls (final layer) ──
    _seen_ids: set = set()
    _kept: list = []
    _drop_n = 0
    _empty_tcs_fixed = 0
    for _m in safe_messages:
        _r = _m.get("role")
        if _r == "assistant":
            for _tc in (_m.get("tool_calls") or []):
                if isinstance(_tc, dict):
                    _id = _tc.get("id") or ""
                    if _id:
                        _seen_ids.add(_id)
            if "tool_calls" in _m and not _m.get("tool_calls"):
                _m = dict(_m)
                _m.pop("tool_calls", None)
                _empty_tcs_fixed += 1
            _kept.append(_m)
        elif _r == "tool":
            _tid = _m.get("tool_call_id") or ""
            if _tid and _tid in _seen_ids:
                _kept.append(_m)
            else:
                _drop_n += 1
        else:
            _kept.append(_m)
    if _drop_n:
        logger.warning(
            "[hard-guard] (stream) dropped %d orphan tool message(s)",
            _drop_n,
        )
    if _empty_tcs_fixed:
        logger.warning(
            "[hard-guard] (stream) stripped empty tool_calls from %d "
            "assistant msg(s)", _empty_tcs_fixed,
        )
    if _drop_n or _empty_tcs_fixed:
        safe_messages = _kept

    valid_tools = _validate_tools(tools)

    payload: dict = {
        "model": model,
        "messages": safe_messages,
        "stream": True,
        # Request final-chunk usage. Providers that don't support it just
        # omit the field; we won't emit a usage event in that case.
        "stream_options": {"include_usage": True},
    }
    if temperature is not None and temperature >= 0:
        payload["temperature"] = float(temperature)
    if valid_tools:
        payload["tools"] = valid_tools
        # Give tool args enough room — write_file/edit_file produce large JSON.
        payload.setdefault("max_tokens", 16384)
        # Parallel tool calling — opt-in per provider (see non-stream path).
        if resolve_strategy(url).supports_parallel_tool_calls_param:
            payload["parallel_tool_calls"] = True
        # Stream-path breakdown — same logger as non-stream.
        _log_payload_breakdown(
            safe_messages, valid_tools,
            agent_id=getattr(_TOKEN_CTX, "agent_id", "") or "",
            model=model,
        )

    session = pool.get_session(_provider_id)
    # 2026-04-30: cap stream retries lower than the global LLM_MAX_RETRIES
    # (5 → 2 by default). Streaming endpoints that need retry are usually
    # genuinely down — burning 6 attempts × backoff just makes the user
    # wait longer before the fallback chain kicks in. Env-tunable.
    max_retries = min(
        pool.max_retries,
        int(os.environ.get("TUDOU_LLM_STREAM_MAX_RETRIES", "2") or 2),
    )

    for attempt in range(max_retries + 1):
        pool.acquire_slot(_provider_id, model)
        released = False
        # Per-attempt state (must reset on retry to avoid duplicate yields)
        tool_states: dict[int, dict] = {}
        finish_reason: str | None = None
        input_tokens = 0
        output_tokens = 0
        cache_read_stream = 0
        cache_write_stream = 0
        first_event_yielded = False

        try:
            with session.post(url, headers=headers, json=payload,
                              stream=True, timeout=_REQUEST_TIMEOUT) as resp:
                if resp.status_code == 429 and not first_event_yielded:
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else (
                        pool.backoff_factor * (2 ** attempt))
                    wait = min(wait, 60)
                    logger.warning(
                        "OpenAI stream_events 429 (attempt %d/%d), "
                        "retrying in %.1fs...",
                        attempt + 1, max_retries + 1, wait)
                    pool.release_slot(_provider_id, model)
                    released = True
                    time.sleep(wait)
                    continue
                resp.raise_for_status()

                for line in resp.iter_lines():
                    if not line:
                        continue
                    text = line.decode("utf-8", errors="replace")
                    if text.startswith("data: "):
                        text = text[6:]
                    if text.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(text)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices") or []
                    if choices:
                        choice = choices[0]
                        delta = choice.get("delta") or {}

                        # Text content
                        content = delta.get("content")
                        if content:
                            yield {"type": "text_delta", "text": content}
                            first_event_yielded = True

                        # Tool calls (incremental)
                        for tc in (delta.get("tool_calls") or []):
                            idx = tc.get("index", 0)
                            state = tool_states.get(idx)
                            if state is None:
                                state = {
                                    "id": tc.get("id") or
                                        f"call_{uuid.uuid4().hex[:8]}",
                                    "name": "",
                                    "args_buf": [],
                                    "started": False,
                                }
                                tool_states[idx] = state
                            else:
                                # Later chunks may carry a real id; upgrade.
                                real_id = tc.get("id")
                                if real_id and state["id"].startswith("call_"):
                                    state["id"] = real_id

                            func = tc.get("function") or {}
                            name = func.get("name") or ""
                            args_part = func.get("arguments") or ""

                            if name:
                                state["name"] = name

                            if not state["started"] and (name or args_part):
                                state["started"] = True
                                yield {
                                    "type": "tool_use_start",
                                    "id": state["id"],
                                    "name": state["name"],
                                }
                                first_event_yielded = True

                            if args_part:
                                state["args_buf"].append(args_part)
                                yield {
                                    "type": "tool_input_delta",
                                    "id": state["id"],
                                    "partial_json": args_part,
                                }
                                first_event_yielded = True

                        fr = choice.get("finish_reason")
                        if fr:
                            finish_reason = fr

                    # Usage chunk (final, only if include_usage honored)
                    usage = chunk.get("usage") or {}
                    if usage:
                        input_tokens = usage.get(
                            "prompt_tokens", input_tokens) or input_tokens
                        output_tokens = usage.get(
                            "completion_tokens",
                            output_tokens) or output_tokens
                        cr_stream, cw_stream = _extract_cache_tokens(usage)
                        if cr_stream or cw_stream:
                            cache_read_stream = cr_stream
                            cache_write_stream = cw_stream

                # Stream ended — finalise any accumulated tool_calls
                for idx in sorted(tool_states.keys()):
                    state = tool_states[idx]
                    # Providers that don't do incremental args (MLX/Qwen
                    # sometimes) may not have emitted a start. Emit one now.
                    if not state["started"]:
                        yield {
                            "type": "tool_use_start",
                            "id": state["id"],
                            "name": state["name"],
                        }
                        first_event_yielded = True
                    full_args = "".join(state["args_buf"])
                    try:
                        inp = json.loads(full_args) if full_args else {}
                    except json.JSONDecodeError:
                        logger.warning(
                            "tool_input JSON parse failed (id=%s, name=%s, "
                            "len=%d) — falling back to raw",
                            state["id"], state["name"], len(full_args))
                        inp = {"_raw": full_args}
                    yield {
                        "type": "tool_use_complete",
                        "id": state["id"],
                        "name": state["name"],
                        "input": inp if isinstance(inp, dict) else {"_raw": full_args},
                    }

                if input_tokens or output_tokens:
                    yield {
                        "type": "usage",
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    }
                    try:
                        _log_token_usage(
                            provider=_provider_id or "openai",
                            model=model,
                            prompt_tokens=input_tokens,
                            completion_tokens=output_tokens,
                            stream=True,
                            cache_read=cache_read_stream,
                            cache_write=cache_write_stream,
                        )
                    except Exception as _te:
                        logger.debug("token usage log failed: %s", _te)

                yield {
                    "type": "stop",
                    "reason": finish_reason or "end_turn",
                }
                return

        except (requests.ConnectionError, requests.Timeout) as e:
            if not first_event_yielded and attempt < max_retries:
                logger.warning(
                    "OpenAI stream_events connection error "
                    "(attempt %d/%d): %s",
                    attempt + 1, max_retries + 1, str(e)[:100])
                pool.release_slot(_provider_id, model)
                released = True
                wait = pool.backoff_factor * (2 ** attempt)
                time.sleep(wait)
                continue
            # Either we already started streaming (can't retry) or out of
            # retries. Yield an error event + stop, then re-raise so the
            # caller can decide.
            yield {"type": "error", "message": str(e)[:200]}
            raise
        finally:
            if not released:
                pool.release_slot(_provider_id, model)


def _apply_anthropic_prompt_cache(payload: dict) -> None:
    """Mark the payload's ``system`` prefix and ``tools`` suffix as cacheable.

    Anthropic prompt caching (GA, Oct-2024) lets you mark specific content
    blocks with ``cache_control: {"type": "ephemeral"}``.  On a cache hit the
    marked prefix costs ~10% of normal input tokens; on a cache write it
    costs ~125% (amortized after one hit).  TTL is 5 minutes.

    Strategy — use 2 of the 4 available breakpoints:
      • system  → convert string to [{text, cache_control}]  (prefix boundary)
      • tools   → cache_control on LAST tool                  (suffix boundary)

    We leave the other 2 breakpoints for future message-prefix caching.

    Environment toggle: ``TUDOU_ANTHROPIC_CACHE=0`` disables entirely
    (useful for debugging or if the account is not on a cache-eligible tier).

    No-op when prompts are too short — Anthropic silently ignores markers on
    prefixes below the min-cacheable-tokens threshold (1024 Haiku / 2048
    Sonnet/Opus), so over-marking is safe.
    """
    import os as _os
    if _os.environ.get("TUDOU_ANTHROPIC_CACHE", "1") == "0":
        return

    # ── system: string → block list with cache_control ──
    sys_val = payload.get("system")
    if isinstance(sys_val, str) and sys_val.strip():
        payload["system"] = [{
            "type": "text",
            "text": sys_val,
            "cache_control": {"type": "ephemeral"},
        }]
    elif isinstance(sys_val, list) and sys_val:
        # Already block list — mark the LAST text block if unmarked.
        last = sys_val[-1]
        if isinstance(last, dict) and "cache_control" not in last:
            last["cache_control"] = {"type": "ephemeral"}

    # ── tools: mark last tool as cache boundary ──
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        last_tool = tools[-1]
        if isinstance(last_tool, dict) and "cache_control" not in last_tool:
            last_tool["cache_control"] = {"type": "ephemeral"}


def _claude_chat(base_url: str, api_key: str,
                 messages: list[dict], tools: list[dict] | None = None,
                 stream: bool = False, model: str = "",
                 _provider_id: str = "claude",
                 tool_choice: dict | str | None = None,
                 temperature: float | None = None,
                 response_format: dict | None = None,
                 ) -> dict | Generator:
    if response_format:
        # Anthropic /v1/messages doesn't have a response_format field.
        # Schema-forced output on Claude is achieved via tool-use:
        # define a single tool with the schema and pass tool_choice
        # forcing it. Caller is responsible for that pattern; we don't
        # auto-convert here to avoid surprising tool side-effects.
        logger.warning(
            "_claude_chat: response_format is ignored on Anthropic — "
            "use tool-use + tool_choice to force schema instead. "
            "(See app.llm.build_schema_tool / force_schema_tool_choice.)",
        )
    url = base_url.rstrip("/") + "/v1/messages"
    pool = get_connection_pool()

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

    system_text = ""
    api_messages = []
    for m in messages:
        if m["role"] == "system":
            c = m.get("content", "")
            system_text += (_ensure_str(c) if not isinstance(c, str) else c) + "\n"
        elif m["role"] == "tool":
            api_messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_use_id", "tool_0"),
                    "content": m["content"],
                }],
            })
        else:
            content = m.get("content", "")
            # Convert OpenAI multimodal format to Claude format
            if isinstance(content, list) and m["role"] == "user":
                claude_parts = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type", "")
                    if ptype == "text":
                        claude_parts.append({"type": "text", "text": part.get("text", "")})
                    elif ptype == "image_url":
                        # Convert data:mime;base64,DATA → Claude source format
                        url = (part.get("image_url") or {}).get("url", "")
                        if url.startswith("data:"):
                            # Parse "data:image/png;base64,iVBOR..."
                            header, _, b64data = url.partition(",")
                            media_type = header.split("data:", 1)[-1].split(";")[0]
                            claude_parts.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type or "image/png",
                                    "data": b64data,
                                },
                            })
                        else:
                            # HTTP URL — Claude supports url source type
                            claude_parts.append({
                                "type": "image",
                                "source": {"type": "url", "url": url},
                            })
                    elif ptype in ("image", "input_image"):
                        # Already Claude-native format
                        claude_parts.append(part)
                content = claude_parts if claude_parts else _ensure_str(content)
            elif not isinstance(content, str):
                content = _ensure_str(content)
            api_messages.append({"role": m["role"], "content": content})

    claude_tools = None
    valid_tools = _validate_tools(tools)
    if valid_tools:
        claude_tools = []
        for t in valid_tools:
            func = t["function"]
            claude_tools.append({
                "name": func["name"],
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
            })

    payload: dict = {
        "model": model,
        "max_tokens": 8192,
        "messages": api_messages,
    }
    _TIMEOUT = _REQUEST_TIMEOUT

    # Anthropic accepts `temperature` in range [0.0, 1.0]. Clamp rather
    # than error — admins may seed >1 values from an OpenAI-era config.
    if temperature is not None and temperature >= 0:
        payload["temperature"] = min(1.0, float(temperature))

    if system_text.strip():
        payload["system"] = system_text.strip()
    if claude_tools:
        payload["tools"] = claude_tools
        # Plan D: translate OpenAI-style tool_choice to Anthropic format.
        #   OpenAI:    {"type":"function","function":{"name":"X"}} | "auto" | "required"
        #   Anthropic: {"type":"tool","name":"X"} | {"type":"auto"} | {"type":"any"}
        if tool_choice:
            _tc = tool_choice
            if isinstance(_tc, dict) and _tc.get("type") == "function":
                _fn_name = (_tc.get("function") or {}).get("name")
                if _fn_name:
                    payload["tool_choice"] = {"type": "tool", "name": _fn_name}
            elif _tc == "required":
                payload["tool_choice"] = {"type": "any"}
            elif _tc == "auto":
                payload["tool_choice"] = {"type": "auto"}
            elif isinstance(_tc, dict) and _tc.get("type") in ("tool", "auto", "any"):
                # Already Anthropic-native
                payload["tool_choice"] = _tc
    _apply_anthropic_prompt_cache(payload)

    if not stream:
        resp = pool.request_with_retry(
            _provider_id, "POST", url, model=model, headers=headers,
            json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        # Anthropic returns stop_reason at top level:
        #   end_turn | tool_use | max_tokens | stop_sequence
        # Normalize to canonical set used throughout Tudou.
        _CLAUDE_STOP_MAP = {
            "end_turn": "end_turn",
            "tool_use": "tool_use",
            "max_tokens": "length",
            "stop_sequence": "stop_sequence",
        }
        _raw_sr = data.get("stop_reason") or "end_turn"
        stop_reason = _CLAUDE_STOP_MAP.get(_raw_sr, _raw_sr)
        result: dict = {"message": {"role": "assistant", "content": ""},
                        "stop_reason": stop_reason}
        tool_calls = []
        for block in data.get("content", []):
            if block["type"] == "text":
                result["message"]["content"] += block["text"]
            elif block["type"] == "tool_use":
                tool_calls.append({
                    "function": {
                        "name": block["name"],
                        "arguments": block["input"],
                    }
                })
        if tool_calls:
            result["message"]["tool_calls"] = tool_calls
        # ── Token usage logging ──
        try:
            _u = data.get("usage") or {}
            _cr, _cw = _extract_cache_tokens(_u)
            _log_token_usage(
                provider=_provider_id or "claude",
                model=model,
                prompt_tokens=_u.get("input_tokens", 0),
                completion_tokens=_u.get("output_tokens", 0),
                stream=False,
                cache_read=_cr,
                cache_write=_cw,
            )
            # Cache observability — surface hit/miss + savings so users can
            # tell whether TUDOU_ANTHROPIC_CACHE is actually working.
            if _cr or _cw:
                # Cache read costs ~10% of normal; cache write ~125%.
                # Rough saved-tokens estimate: read * 0.9 - write * 0.25
                _saved = int(_cr * 0.9 - _cw * 0.25)
                logger.info(
                    "[cache] model=%s read=%d write=%d (≈saved %d in-tokens)",
                    model, _cr, _cw, _saved,
                )
        except Exception as _te:
            logger.debug("token usage log failed: %s", _te)
        return result
    else:
        payload["stream"] = True

        def _gen():
            session = pool.get_session(_provider_id)
            max_stream_retries = pool.max_retries
            for attempt in range(max_stream_retries + 1):
                pool.acquire_slot(_provider_id, model)
                released = False
                try:
                    with session.post(url, headers=headers, json=payload,
                                      stream=True, timeout=_TIMEOUT) as resp:
                        if resp.status_code == 429:
                            retry_after = resp.headers.get("Retry-After")
                            wait = float(retry_after) if retry_after else (
                                pool.backoff_factor * (2 ** attempt))
                            wait = min(wait, 60)
                            logger.warning(
                                "Claude stream 429 (attempt %d/%d), "
                                "retrying in %.1fs...",
                                attempt + 1, max_stream_retries + 1, wait)
                            pool.release_slot(_provider_id, model)
                            released = True
                            time.sleep(wait)
                            continue
                        resp.raise_for_status()
                        for line in resp.iter_lines():
                            if not line:
                                continue
                            text = line.decode("utf-8")
                            if text.startswith("data: "):
                                text = text[6:]
                            try:
                                event = json.loads(text)
                                if event.get("type") == "content_block_delta":
                                    delta = event.get("delta", {})
                                    if delta.get("type") == "text_delta":
                                        yield delta.get("text", "")
                            except (json.JSONDecodeError, KeyError):
                                continue
                    return  # 成功
                except (requests.ConnectionError, requests.Timeout) as e:
                    logger.warning(
                        "Claude stream connection error (attempt %d/%d): %s",
                        attempt + 1, max_stream_retries + 1, str(e)[:100])
                    if attempt < max_stream_retries:
                        pool.release_slot(_provider_id, model)
                        released = True
                        wait = pool.backoff_factor * (2 ** attempt)
                        time.sleep(wait)
                        continue
                    raise
                finally:
                    if not released:
                        pool.release_slot(_provider_id, model)
        return _gen()


def _claude_stream_events(base_url: str, api_key: str,
                          messages: list[dict],
                          tools: list[dict] | None = None,
                          model: str = "",
                          _provider_id: str = "claude",
                          temperature: float | None = None,
                          ) -> Generator[dict, None, None]:
    """Anthropic streaming with full tool_use event support.

    Parses the complete SSE event grammar:
      message_start        → initial usage.input_tokens
      content_block_start  → text or tool_use block begin (emit tool_use_start)
      content_block_delta  → text_delta or input_json_delta (emit text_delta /
                             tool_input_delta)
      content_block_stop   → finalise tool_use (emit tool_use_complete)
      message_delta        → incremental usage.output_tokens, stop_reason
      message_stop         → end (emit usage + stop)
    """
    url = base_url.rstrip("/") + "/v1/messages"
    pool = get_connection_pool()
    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
    }

    # Mirror _claude_chat's message transformation (system flattening,
    # multimodal conversion, tool_result wrapping). To avoid code
    # duplication we reuse the same pre-processing pattern locally.
    system_text = ""
    api_messages: list[dict] = []
    for m in messages:
        if m["role"] == "system":
            c = m.get("content", "")
            system_text += (_ensure_str(c) if not isinstance(c, str) else c) + "\n"
        elif m["role"] == "tool":
            api_messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": m.get("tool_use_id", "tool_0"),
                    "content": m["content"],
                }],
            })
        else:
            content = m.get("content", "")
            if isinstance(content, list) and m["role"] == "user":
                claude_parts = []
                for part in content:
                    if not isinstance(part, dict):
                        continue
                    ptype = part.get("type", "")
                    if ptype == "text":
                        claude_parts.append(
                            {"type": "text", "text": part.get("text", "")})
                    elif ptype == "image_url":
                        _url = (part.get("image_url") or {}).get("url", "")
                        if _url.startswith("data:"):
                            header, _, b64data = _url.partition(",")
                            media_type = header.split(
                                "data:", 1)[-1].split(";")[0]
                            claude_parts.append({
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": media_type or "image/png",
                                    "data": b64data,
                                },
                            })
                        else:
                            claude_parts.append({
                                "type": "image",
                                "source": {"type": "url", "url": _url},
                            })
                    elif ptype in ("image", "input_image"):
                        claude_parts.append(part)
                content = claude_parts if claude_parts else _ensure_str(content)
            elif not isinstance(content, str):
                content = _ensure_str(content)
            api_messages.append({"role": m["role"], "content": content})

    claude_tools = None
    valid_tools = _validate_tools(tools)
    if valid_tools:
        claude_tools = []
        for t in valid_tools:
            func = t["function"]
            claude_tools.append({
                "name": func["name"],
                "description": func.get("description", ""),
                "input_schema": func.get(
                    "parameters", {"type": "object", "properties": {}}),
            })

    payload: dict = {
        "model": model,
        "max_tokens": 8192,
        "messages": api_messages,
        "stream": True,
    }
    # Clamp to Anthropic's [0.0, 1.0] range (see _claude_chat).
    if temperature is not None and temperature >= 0:
        payload["temperature"] = min(1.0, float(temperature))
    if system_text.strip():
        payload["system"] = system_text.strip()
    if claude_tools:
        payload["tools"] = claude_tools
        # Plan D: translate OpenAI-style tool_choice to Anthropic format.
        #   OpenAI:    {"type":"function","function":{"name":"X"}} | "auto" | "required"
        #   Anthropic: {"type":"tool","name":"X"} | {"type":"auto"} | {"type":"any"}
        if tool_choice:
            _tc = tool_choice
            if isinstance(_tc, dict) and _tc.get("type") == "function":
                _fn_name = (_tc.get("function") or {}).get("name")
                if _fn_name:
                    payload["tool_choice"] = {"type": "tool", "name": _fn_name}
            elif _tc == "required":
                payload["tool_choice"] = {"type": "any"}
            elif _tc == "auto":
                payload["tool_choice"] = {"type": "auto"}
            elif isinstance(_tc, dict) and _tc.get("type") in ("tool", "auto", "any"):
                # Already Anthropic-native
                payload["tool_choice"] = _tc
    _apply_anthropic_prompt_cache(payload)

    session = pool.get_session(_provider_id)
    # 2026-04-30: cap stream retries lower than the global LLM_MAX_RETRIES
    # (5 → 2 by default). Streaming endpoints that need retry are usually
    # genuinely down — burning 6 attempts × backoff just makes the user
    # wait longer before the fallback chain kicks in. Env-tunable.
    max_retries = min(
        pool.max_retries,
        int(os.environ.get("TUDOU_LLM_STREAM_MAX_RETRIES", "2") or 2),
    )

    for attempt in range(max_retries + 1):
        pool.acquire_slot(_provider_id, model)
        released = False
        # Per-attempt state
        current_block_type: str | None = None
        current_tool_id: str | None = None
        current_tool_name: str | None = None
        current_tool_buf: list[str] = []
        input_tokens = 0
        output_tokens = 0
        cache_read_stream = 0
        cache_write_stream = 0
        stop_reason: str | None = None
        first_event_yielded = False

        try:
            with session.post(url, headers=headers, json=payload,
                              stream=True, timeout=_REQUEST_TIMEOUT) as resp:
                if resp.status_code == 429 and not first_event_yielded:
                    retry_after = resp.headers.get("Retry-After")
                    wait = float(retry_after) if retry_after else (
                        pool.backoff_factor * (2 ** attempt))
                    wait = min(wait, 60)
                    logger.warning(
                        "Claude stream_events 429 (attempt %d/%d), "
                        "retrying in %.1fs...",
                        attempt + 1, max_retries + 1, wait)
                    pool.release_slot(_provider_id, model)
                    released = True
                    time.sleep(wait)
                    continue
                resp.raise_for_status()

                for line in resp.iter_lines():
                    if not line:
                        continue
                    text = line.decode("utf-8", errors="replace")
                    if text.startswith("data: "):
                        text = text[6:]
                    # Claude also emits lines like "event: message_start" which
                    # we ignore (redundant with the type field in data).
                    if not text.startswith("{"):
                        continue
                    try:
                        event = json.loads(text)
                    except json.JSONDecodeError:
                        continue

                    etype = event.get("type")

                    if etype == "message_start":
                        msg = event.get("message") or {}
                        usage = msg.get("usage") or {}
                        input_tokens = usage.get(
                            "input_tokens", 0) or input_tokens
                        # Anthropic emits cache_*_input_tokens here, not in
                        # message_delta. message_start fires once, so we just
                        # capture and don't accumulate.
                        cr_evt, cw_evt = _extract_cache_tokens(usage)
                        if cr_evt or cw_evt:
                            cache_read_stream = cr_evt
                            cache_write_stream = cw_evt

                    elif etype == "content_block_start":
                        block = event.get("content_block") or {}
                        current_block_type = block.get("type")
                        if current_block_type == "tool_use":
                            current_tool_id = block.get("id") or \
                                f"toolu_{uuid.uuid4().hex[:8]}"
                            current_tool_name = block.get("name") or ""
                            current_tool_buf = []
                            yield {
                                "type": "tool_use_start",
                                "id": current_tool_id,
                                "name": current_tool_name,
                            }
                            first_event_yielded = True

                    elif etype == "content_block_delta":
                        delta = event.get("delta") or {}
                        dtype = delta.get("type")
                        if dtype == "text_delta":
                            t = delta.get("text", "")
                            if t:
                                yield {"type": "text_delta", "text": t}
                                first_event_yielded = True
                        elif dtype == "input_json_delta":
                            partial = delta.get("partial_json", "")
                            if partial:
                                current_tool_buf.append(partial)
                                yield {
                                    "type": "tool_input_delta",
                                    "id": current_tool_id,
                                    "partial_json": partial,
                                }
                                first_event_yielded = True

                    elif etype == "content_block_stop":
                        if current_block_type == "tool_use" and current_tool_id:
                            full_json = "".join(current_tool_buf)
                            try:
                                inp = json.loads(full_json) if full_json else {}
                            except json.JSONDecodeError:
                                logger.warning(
                                    "claude tool_input JSON parse failed "
                                    "(id=%s, name=%s, len=%d) — falling back "
                                    "to raw",
                                    current_tool_id, current_tool_name,
                                    len(full_json))
                                inp = {"_raw": full_json}
                            yield {
                                "type": "tool_use_complete",
                                "id": current_tool_id,
                                "name": current_tool_name,
                                "input": inp if isinstance(inp, dict)
                                    else {"_raw": full_json},
                            }
                        current_block_type = None
                        current_tool_id = None
                        current_tool_name = None
                        current_tool_buf = []

                    elif etype == "message_delta":
                        usage = event.get("usage") or {}
                        if usage:
                            output_tokens = usage.get(
                                "output_tokens", output_tokens) or output_tokens
                        d = event.get("delta") or {}
                        if d.get("stop_reason"):
                            stop_reason = d.get("stop_reason")

                    elif etype == "message_stop":
                        break

                # Emit final usage + stop
                if input_tokens or output_tokens:
                    yield {
                        "type": "usage",
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                    }
                    try:
                        _log_token_usage(
                            provider=_provider_id or "claude",
                            model=model,
                            prompt_tokens=input_tokens,
                            completion_tokens=output_tokens,
                            stream=True,
                            cache_read=cache_read_stream,
                            cache_write=cache_write_stream,
                        )
                    except Exception as _te:
                        logger.debug("token usage log failed: %s", _te)

                yield {
                    "type": "stop",
                    "reason": stop_reason or "end_turn",
                }
                return

        except (requests.ConnectionError, requests.Timeout) as e:
            if not first_event_yielded and attempt < max_retries:
                logger.warning(
                    "Claude stream_events connection error "
                    "(attempt %d/%d): %s",
                    attempt + 1, max_retries + 1, str(e)[:100])
                pool.release_slot(_provider_id, model)
                released = True
                wait = pool.backoff_factor * (2 ** attempt)
                time.sleep(wait)
                continue
            yield {"type": "error", "message": str(e)[:200]}
            raise
        finally:
            if not released:
                pool.release_slot(_provider_id, model)


# Protocol dispatch
_PROTOCOL_HANDLERS = {
    "ollama": _ollama_chat,
    "openai": _openai_chat,
    "claude": _claude_chat,
}


# ---------------------------------------------------------------------------
# Unified interface
# ---------------------------------------------------------------------------

def _resolve_provider(provider_id: str) -> ProviderEntry | None:
    """Look up provider from registry, fallback to legacy config."""
    reg = get_registry()
    p = reg.get(provider_id)
    if p and p.enabled:
        return p

    # Fallback: try to build a virtual provider from legacy config
    cfg = get_config()
    if provider_id == "ollama" or (not provider_id and cfg["provider"] == "ollama"):
        return ProviderEntry(id="ollama", kind="ollama",
                             base_url=cfg["ollama_url"])
    elif provider_id == "openai":
        return ProviderEntry(id="openai", kind="openai",
                             base_url=cfg["openai_base_url"],
                             api_key=cfg["openai_api_key"])
    elif provider_id == "claude":
        return ProviderEntry(id="claude", kind="claude",
                             base_url="https://api.anthropic.com",
                             api_key=cfg["claude_api_key"])
    elif provider_id == "unsloth":
        return ProviderEntry(id="unsloth", kind="openai",
                             base_url=cfg.get("unsloth_base_url",
                                              _DEF_UNSLOTH_URL),
                             api_key=cfg.get("unsloth_api_key", ""))
    return None


def _chat_with_fallback(messages: list[dict], tools: list[dict] | None = None,
                        stream: bool = False, model: str = "",
                        provider_chain: list[ProviderEntry] | None = None,
                        tool_choice: dict | str | None = None,
                        temperature: float | None = None,
                        response_format: dict | None = None,
                        ) -> dict | Generator[str, None, None]:
    """
    Internal function that tries providers in a fallback chain.

    On retryable errors (429, ConnectionError, Timeout), tries the next provider.
    On success, returns immediately. If all fail, raises the last exception.

    Args:
        messages: Chat messages
        tools: Optional tool definitions
        stream: Whether to stream the response
        model: Model name to use
        provider_chain: List of ProviderEntry objects to try in order.
                       If None or empty, uses the primary provider only.

    Returns: dict or Generator depending on stream parameter.
    Raises: Last exception encountered if all providers fail.
    """
    if not provider_chain:
        raise ValueError("provider_chain must not be empty")

    last_error = None

    for idx, entry in enumerate(provider_chain):
        try:
            # Check if provider is configured for master_proxy scope
            if entry.scope == "master_proxy":
                try:
                    from .ws_bus import is_distributed
                    if is_distributed():
                        return _proxy_chat(entry.base_url, entry.api_key,
                                           messages, tools=tools, stream=stream, model=model,
                                           _provider_id=entry.id,
                                           tool_choice=tool_choice,
                                           temperature=temperature,
                                           response_format=response_format)
                except ImportError:
                    pass

            # Apply prompt caching for Anthropic/Claude providers
            msgs_to_send = apply_prompt_cache(messages, entry.kind)

            handler = _PROTOCOL_HANDLERS.get(entry.kind)
            if handler is None:
                raise ValueError(f"Unknown protocol kind: {entry.kind!r}")

            return handler(entry.base_url, entry.api_key,
                           msgs_to_send, tools=tools, stream=stream, model=model,
                           _provider_id=entry.id, tool_choice=tool_choice,
                           temperature=temperature,
                           response_format=response_format)

        except (requests.ConnectionError, requests.Timeout) as e:
            last_error = e
            if idx < len(provider_chain) - 1:
                logger.warning(
                    f"Provider '{entry.id}' failed with {type(e).__name__}: {str(e)[:100]}. "
                    f"Trying fallback provider..."
                )
            continue
        except requests.HTTPError as e:
            # Only retry on 429 (rate limit), not other HTTP errors
            if e.response is not None and e.response.status_code == 429:
                last_error = e
                if idx < len(provider_chain) - 1:
                    logger.warning(
                        f"Provider '{entry.id}' returned 429 (rate limited). "
                        f"Trying fallback provider..."
                    )
                continue
            else:
                raise
        except Exception as e:
            # For any other exception, don't retry, just raise immediately
            raise

    # All providers exhausted
    if last_error:
        raise last_error
    raise RuntimeError("No valid providers in fallback chain")


# ─────────────────────────────────────────────────────────────────────
# Structured-output helpers (Phase 4 of prompt-pipeline cleanup)
# ─────────────────────────────────────────────────────────────────────


def make_json_object_response_format() -> dict:
    """``{"type": "json_object"}`` — coerce model to emit valid JSON,no schema.

    Use when the caller will parse JSON but doesn't have a schema to enforce.
    OpenAI / DeepSeek / Doubao support; Anthropic ignores; Ollama best-effort.
    """
    return {"type": "json_object"}


def make_json_schema_response_format(
    name: str,
    schema: dict,
    strict: bool = True,
) -> dict:
    """OpenAI-format ``response_format`` for json_schema strict mode.

    Args:
        name:    schema 名(模型 prompt 里会引用,简单标识符即可)
        schema:  JSON Schema dict(``{"type": "object", "properties": {...}, ...}``)
        strict:  True → 拒绝任何不符合 schema 的输出(GPT-4o+ / DeepSeek-Chat 支持);
                 False → 模型尽量遵守,但不强制

    Returns the dict suitable for ``chat(response_format=...)``。
    """
    return {
        "type": "json_schema",
        "json_schema": {
            "name": name,
            "schema": schema,
            "strict": strict,
        },
    }


def build_schema_tool(name: str, schema: dict, description: str = "") -> dict:
    """Anthropic-style schema-tool — 把 JSON Schema 包成"假工具"。

    Anthropic 没有 ``response_format`` 字段;强制结构化输出的标准做法是
    定义一个 schema 当 input_schema 的 tool,然后 ``tool_choice`` 强制
    使用它。Claude 必须填这个 tool 的参数才能完成 turn。

    Args:
        name:        tool 名(给模型看的标识)
        schema:      input JSON Schema
        description: 给模型的简短描述(说明这个"工具"做什么)

    Returns OpenAI/Anthropic 共用的 tool dict。配合
    ``tool_choice={"type": "tool", "name": name}`` 使用。
    """
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description or f"返回严格符合 {name} schema 的结构化数据。",
            "parameters": schema,
        },
    }


def force_schema_tool_choice(name: str) -> dict:
    """跟 ``build_schema_tool`` 配套 — Anthropic 强制选 tool 的 tool_choice。"""
    return {"type": "tool", "name": name}


def chat(messages: list[dict], tools: list[dict] | None = None,
         stream: bool = False,
         provider: str = "", model: str = "",
         tool_choice: dict | str | None = None,
         temperature: float | None = None,
         response_format: dict | None = None,
         ) -> dict | Generator[str, None, None]:
    """
    Send a chat request to an LLM backend with automatic fallback chain support.

    When the primary provider fails with a retryable error (429, timeout, connection error),
    automatically tries fallback providers configured in the provider's fallback_providers list.

    Args:
        provider: Provider ID from the registry (e.g. "ollama", "openai").
                  If empty, uses the global config default.
        model:    Model name (e.g. "qwen3.5:9b", "gpt-4o").
                  If empty, uses the global config default.

    Returns normalised Ollama-format dict (non-stream) or text-chunk generator.
    Raises: ValueError if provider not found, or last exception if all providers in chain fail.
    """
    # Caller MUST pass an explicit provider + model. Global config is
    # NOT a fallback source — agents without a bound LLM are expected to
    # fail fast here so the UI can prompt for selection.
    cfg = get_config()
    prov_id = provider or cfg["provider"]
    mdl = model or cfg["model"]

    # Safety net: if BOTH the caller AND global config are empty there's
    # nothing to call. Agent-chat has a STRICT gate earlier (REST layer
    # returns 409 NO_LLM_CONFIGURED before reaching chat()). Standalone
    # tools (REPL, web.py) keep the global-config fallback.
    if not prov_id or not mdl:
        raise ValueError(
            "NO_LLM_CONFIGURED: no LLM provider/model resolved. "
            "Bind an LLM to this agent or set a global default."
        )

    entry = _resolve_provider(prov_id)
    if entry is None:
        raise ValueError(
            f"Unknown provider: {prov_id!r}. "
            f"Available: {list_providers()}"
        )

    # Build fallback chain from registry
    reg = get_registry()
    provider_chain = reg.get_fallback_chain(entry.id)

    # Fallback: if the resolved provider isn't in registry (e.g., from legacy config),
    # just use it as a single-item chain
    if not provider_chain:
        provider_chain = [entry]

    return _chat_with_fallback(messages, tools=tools, stream=stream,
                                model=mdl, provider_chain=provider_chain,
                                tool_choice=tool_choice,
                                temperature=temperature,
                                response_format=response_format)


def chat_no_stream(messages: list[dict], tools: list[dict] | None = None,
                   provider: str = "", model: str = "",
                   tool_choice: dict | str | None = None,
                   temperature: float | None = None,
                   response_format: dict | None = None) -> dict:
    """Convenience: always returns a dict (no streaming)."""
    result = chat(messages, tools=tools, stream=False,
                  provider=provider, model=model,
                  tool_choice=tool_choice,
                  temperature=temperature,
                  response_format=response_format)
    assert isinstance(result, dict)
    # Post-process: some models return tool_calls embedded in content
    # rather than the standard ``message.tool_calls`` array (notably
    # GLM-4.5-air emits ``<arg_key>...<arg_value>...`` XML inline).
    # The V2 tool_parsers package already knows how to recognise this
    # pattern; we route through it here so V1 chat sees a normalized
    # OpenAI-shaped dict regardless of provider quirks.
    try:
        result = _postprocess_xml_tool_calls(result, model=model)
    except Exception as _pe:
        logger.debug("xml tool_call postprocess skipped: %s", _pe)
    return result


def _postprocess_xml_tool_calls(result: dict, *, model: str = "") -> dict:
    """If ``result.message.content`` contains XML tool_call markup but
    ``message.tool_calls`` is empty/missing, run V2 ParserRegistry.
    Returns the (possibly modified) result dict.
    """
    if not isinstance(result, dict):
        return result
    msg = result.get("message")
    if not isinstance(msg, dict):
        return result
    content = msg.get("content") or ""
    if not isinstance(content, str) or not content:
        return result
    # Cheap pre-check — if no XML tool markup and no JSON-in-content
    # signals, skip the heavier parser path.
    needs_parse = (
        "<tool_call>" in content
        or ("<arg_key>" in content and "<arg_value>" in content)
        or ("<function" in content and "<parameter" in content)
    )
    if not needs_parse:
        return result
    # If the message already has tool_calls AND no XML in content,
    # nothing to do. Otherwise run the parser.
    if msg.get("tool_calls"):
        # Strip the XML markup from content so the chat UI doesn't
        # double-render (it'll show the structured tool_calls instead).
        msg["content"] = _strip_xml_tool_markup(content)
        return result
    try:
        from .v2.bridges.tool_parsers import get_registry
    except Exception:
        return result
    registry = get_registry()
    parser = registry.resolve(model or "")
    try:
        normalized = parser.parse(msg)
    except Exception as _e:
        logger.debug("V2 parser failed for model=%s: %s", model, _e)
        return result
    try:
        normalized_dict = normalized.to_openai_dict()
    except Exception:
        return result
    if isinstance(normalized_dict, dict):
        # Replace just the assistant message; keep envelope keys
        result["message"] = normalized_dict
    return result


def _strip_xml_tool_markup(content: str) -> str:
    """Remove ``<tool_call>...</tool_call>`` blocks (and bare
    ``<arg_key>/<arg_value>`` runs from GLM-style emissions) from
    chat content so the rendered text doesn't show raw XML."""
    if not content:
        return content
    # Standard wrapped form
    cleaned = re.sub(r"<tool_call>[\s\S]*?</tool_call>", "", content)
    # GLM stray runs (no wrapper, just arg_key/arg_value pairs)
    cleaned = re.sub(
        r"(?:<arg_key>[\s\S]*?</arg_key>\s*<arg_value>[\s\S]*?</arg_value>\s*)+",
        "", cleaned,
    )
    return cleaned.strip() or content   # never return empty when there was content


# ---------------------------------------------------------------------------
# Provider-agnostic streaming with tool_use support
# ---------------------------------------------------------------------------
# The legacy chat(stream=True) yields plain text chunks (Generator[str]),
# which can't carry tool_use decisions. chat_stream_events() is the
# replacement for callers that want true streaming *and* tool-use events.
#
# Event schema (stable contract):
#   {"type": "text_delta",        "text": "..."}
#   {"type": "tool_use_start",    "id": "...", "name": "..."}
#   {"type": "tool_input_delta",  "id": "...", "partial_json": "..."}
#   {"type": "tool_use_complete", "id": "...", "name": "...", "input": {...}}
#   {"type": "usage",             "input_tokens": N, "output_tokens": M}
#   {"type": "stop",              "reason": "end_turn"|"tool_use"|"length"|...}
#   {"type": "error",             "message": "..."}
#
# Fallback-chain semantics: if the primary provider fails *before* yielding
# the first event (connection refused, 429, etc.), the next provider in the
# chain is tried. Once the first event has been yielded we are committed to
# that provider — fallback is best-effort, not transactional.
# ---------------------------------------------------------------------------

def _dispatch_stream_events(entry: "ProviderEntry",
                            messages: list[dict],
                            tools: list[dict] | None,
                            model: str,
                            temperature: float | None = None,
                            ) -> Generator[dict, None, None]:
    """Route to the right provider-specific stream parser based on kind."""
    kind = entry.kind
    if kind == "claude":
        return _claude_stream_events(
            entry.base_url, entry.api_key,
            messages, tools=tools, model=model,
            _provider_id=entry.id,
            temperature=temperature,
        )
    # ollama / openai / unsloth / any other OpenAI-compat server
    return _openai_stream_events(
        entry.base_url, entry.api_key,
        messages, tools=tools, model=model,
        _provider_id=entry.id,
        temperature=temperature,
    )


def chat_stream_events(messages: list[dict],
                       tools: list[dict] | None = None,
                       provider: str = "",
                       model: str = "",
                       temperature: float | None = None,
                       ) -> Generator[dict, None, None]:
    """Unified streaming entry yielding provider-agnostic event dicts.

    This is the tool-aware replacement for `chat(messages, stream=True)`.
    The older function is preserved for backward compatibility (it yields
    plain text strings and drops tool decisions).

    Args:
        provider: Provider ID from registry. Empty = global config default.
        model:    Model name. Empty = global config default.

    Yields:
        dict events per the schema documented at the top of this section.

    Raises:
        ValueError if provider not found.
        Last encountered exception if all providers in the fallback chain
        fail before producing any event.
    """
    # Caller MUST pass an explicit provider + model. Global config is
    # NOT a fallback source — agents without a bound LLM are expected to
    # fail fast here so the UI can prompt for selection.
    cfg = get_config()
    prov_id = provider or cfg["provider"]
    mdl = model or cfg["model"]

    # Safety net: if BOTH the caller AND global config are empty there's
    # nothing to call. Agent-chat has a STRICT gate earlier (REST layer
    # returns 409 NO_LLM_CONFIGURED before reaching chat()). Standalone
    # tools (REPL, web.py) keep the global-config fallback.
    if not prov_id or not mdl:
        raise ValueError(
            "NO_LLM_CONFIGURED: no LLM provider/model resolved. "
            "Bind an LLM to this agent or set a global default."
        )

    entry = _resolve_provider(prov_id)
    if entry is None:
        raise ValueError(
            f"Unknown provider: {prov_id!r}. "
            f"Available: {list_providers()}"
        )

    reg = get_registry()
    provider_chain = reg.get_fallback_chain(entry.id)
    if not provider_chain:
        provider_chain = [entry]

    last_error: Exception | None = None
    for idx, pentry in enumerate(provider_chain):
        try:
            gen_iter = iter(_dispatch_stream_events(
                pentry, messages, tools, mdl, temperature=temperature))
            # Pull the first event here so we can fall over to the next
            # provider on a pre-stream error (connection refused, 429, DNS).
            try:
                first = next(gen_iter)
            except StopIteration:
                return  # empty stream — shouldn't happen, but treat as success
            yield first
            for ev in gen_iter:
                yield ev
            return
        except (requests.ConnectionError, requests.Timeout,
                requests.HTTPError) as e:
            last_error = e
            if idx < len(provider_chain) - 1:
                logger.warning(
                    "stream_events provider %s failed (%s); trying next",
                    pentry.id, str(e)[:120])
                continue
            raise

    if last_error:
        raise last_error
