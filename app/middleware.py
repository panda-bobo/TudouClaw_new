"""
TudouClaw Middleware Pipeline — 确定性执行管道。

Harness Engineering 核心组件之一：Hooks/Middleware 确保 agent 的每一步
都经过质量关卡，而非依赖模型判断。

Pipeline stages:
    PRE_TOOL    → 工具调用前 (参数 lint、权限检查、注入)
    POST_TOOL   → 工具调用后 (结果校验、截断、审计)
    PRE_LLM     → LLM 调用前 (上下文压缩、token 预算、model routing)
    POST_LLM    → LLM 响应后 (幻觉检测、输出格式校验)
    COMPACTION  → 上下文压缩 (独立阶段，可手动或自动触发)

每个 stage 可以注册多个 Middleware；按 priority 升序执行。
Middleware 返回 MiddlewareResult:
  - CONTINUE  → 继续下一个中间件
  - SHORT_CIRCUIT → 跳过后续中间件，用 result.value 作为最终结果
  - ERROR → 中止，记录错误

设计原则:
  - 中间件是纯函数 (input → output)，不持有状态
  - 所有中间件共享 MiddlewareContext (只读 agent 信息 + 可变 data dict)
  - Pipeline 本身无副作用；副作用由调用方根据返回值执行
  - 失败安全：单个中间件异常不阻塞 pipeline，仅记录跳过
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .defaults import (
    CONTEXT_WARN_HIGH, CONTEXT_WARN_MEDIUM, CONTEXT_WARN_LOW,
    MAX_TOOL_RESULT_CHARS,
)

logger = logging.getLogger("tudou.middleware")


# ─────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────

class Stage(str, Enum):
    """Pipeline stages where middleware can be injected."""
    PRE_TOOL = "pre_tool"
    POST_TOOL = "post_tool"
    PRE_LLM = "pre_llm"
    POST_LLM = "post_llm"
    COMPACTION = "compaction"


class Action(str, Enum):
    """Middleware decision — what the pipeline should do next."""
    CONTINUE = "continue"           # 继续下一个中间件
    SHORT_CIRCUIT = "short_circuit" # 跳过剩余中间件，直接返回 value
    ERROR = "error"                 # 中止 pipeline，记录错误


@dataclass
class MiddlewareContext:
    """Read/write context passed through all middlewares in a stage.

    Fields prefixed ``_`` are considered internal and should not be
    modified by middlewares.
    """
    # ── Read-only agent info ──
    agent_id: str = ""
    agent_name: str = ""
    # ── Stage-specific payload (mutable — middlewares transform it) ──
    tool_name: str = ""
    tool_arguments: dict = field(default_factory=dict)
    tool_result: str = ""
    messages: list[dict] = field(default_factory=list)
    provider: str = ""
    model: str = ""
    llm_response: dict = field(default_factory=dict)
    # ── Shared data bag for cross-middleware communication ──
    data: dict = field(default_factory=dict)
    # ── Metrics (populated by pipeline) ──
    _timings: dict = field(default_factory=dict)


@dataclass
class MiddlewareResult:
    """Return value from a single middleware invocation."""
    action: Action = Action.CONTINUE
    value: Any = None          # 仅 SHORT_CIRCUIT/ERROR 时有意义
    message: str = ""          # 人类可读说明
    modified_ctx: bool = False # 是否修改了 context（供审计用）


@dataclass
class PipelineResult:
    """Aggregate result from running a full pipeline stage."""
    ok: bool = True
    short_circuited: bool = False
    value: Any = None
    errors: list[str] = field(default_factory=list)
    timings: dict = field(default_factory=dict)
    ctx: MiddlewareContext = field(default_factory=MiddlewareContext)


# ─────────────────────────────────────────────────────────────
# Middleware 注册项
# ─────────────────────────────────────────────────────────────

@dataclass
class MiddlewareEntry:
    """A registered middleware function with metadata."""
    name: str
    stage: Stage
    fn: Callable[[MiddlewareContext], MiddlewareResult]
    priority: int = 100      # 越小越先执行
    enabled: bool = True
    description: str = ""


# ─────────────────────────────────────────────────────────────
# Pipeline
# ─────────────────────────────────────────────────────────────

class MiddlewarePipeline:
    """统一中间件管道。

    用法::

        pipe = MiddlewarePipeline()
        pipe.register(MiddlewareEntry(
            name="lint_check",
            stage=Stage.PRE_TOOL,
            fn=tool_lint_check,
            priority=10,
        ))
        # 在 agent 工具调用前:
        ctx = MiddlewareContext(tool_name="bash", tool_arguments={...})
        result = pipe.run(Stage.PRE_TOOL, ctx)
        if result.short_circuited:
            return result.value  # 直接返回，不执行工具
    """

    def __init__(self) -> None:
        self._entries: dict[Stage, list[MiddlewareEntry]] = {s: [] for s in Stage}
        self._stats: dict[str, dict] = {}  # name → {calls, errors, total_ms}

    # ── Registration ──

    def register(self, entry: MiddlewareEntry) -> None:
        """Register a middleware. Duplicate names in the same stage are replaced."""
        stage_list = self._entries[entry.stage]
        # Remove existing with same name
        stage_list[:] = [e for e in stage_list if e.name != entry.name]
        stage_list.append(entry)
        stage_list.sort(key=lambda e: e.priority)
        self._stats.setdefault(entry.name, {"calls": 0, "errors": 0, "total_ms": 0})
        logger.debug("Middleware registered: %s @ %s (priority=%d)",
                      entry.name, entry.stage.value, entry.priority)

    def unregister(self, name: str, stage: Stage | None = None) -> bool:
        """Remove a middleware by name. If stage is None, remove from all stages."""
        removed = False
        stages = [stage] if stage else list(Stage)
        for s in stages:
            before = len(self._entries[s])
            self._entries[s] = [e for e in self._entries[s] if e.name != name]
            if len(self._entries[s]) < before:
                removed = True
        return removed

    def list_entries(self, stage: Stage | None = None) -> list[MiddlewareEntry]:
        """List registered middlewares, optionally filtered by stage."""
        if stage:
            return list(self._entries[stage])
        result = []
        for s in Stage:
            result.extend(self._entries[s])
        return result

    # ── Execution ──

    def run(self, stage: Stage, ctx: MiddlewareContext) -> PipelineResult:
        """Execute all enabled middlewares for a stage in priority order.

        Each middleware receives the same *ctx* object (mutations are visible
        to subsequent middlewares). A SHORT_CIRCUIT stops the chain early.
        Exceptions are caught per-middleware — the chain continues.
        """
        entries = [e for e in self._entries[stage] if e.enabled]
        result = PipelineResult(ctx=ctx)

        for entry in entries:
            t0 = time.monotonic()
            try:
                mr = entry.fn(ctx)
                elapsed = (time.monotonic() - t0) * 1000
                self._stats[entry.name]["calls"] += 1
                self._stats[entry.name]["total_ms"] += elapsed
                result.timings[entry.name] = elapsed

                if mr.action == Action.SHORT_CIRCUIT:
                    result.short_circuited = True
                    result.value = mr.value
                    logger.info("Middleware %s short-circuited: %s",
                                 entry.name, mr.message)
                    return result

                if mr.action == Action.ERROR:
                    result.ok = False
                    result.errors.append(f"{entry.name}: {mr.message}")
                    self._stats[entry.name]["errors"] += 1
                    logger.warning("Middleware %s error: %s", entry.name, mr.message)
                    # Continue chain — errors are non-fatal by default

            except Exception as exc:
                elapsed = (time.monotonic() - t0) * 1000
                self._stats[entry.name]["calls"] += 1
                self._stats[entry.name]["errors"] += 1
                self._stats[entry.name]["total_ms"] += elapsed
                result.timings[entry.name] = elapsed
                result.errors.append(f"{entry.name}: exception: {exc}")
                logger.error("Middleware %s raised: %s", entry.name, exc,
                              exc_info=True)
                # Swallow and continue — fail-safe

        return result

    def get_stats(self) -> dict:
        """Return per-middleware call/error/timing statistics."""
        return dict(self._stats)


# ─────────────────────────────────────────────────────────────
# 内建中间件实现
# ─────────────────────────────────────────────────────────────

# ====== P0: Tool Argument Lint Check ======

def tool_lint_check(ctx: MiddlewareContext) -> MiddlewareResult:
    """PRE_TOOL: Validate tool arguments against the tool's JSON Schema.

    If the tool schema defines ``required`` properties, checks they exist.
    Also validates basic types (string, number, integer, boolean, array, object).
    Returns SHORT_CIRCUIT with a descriptive error if validation fails,
    so the agent can self-correct without wasting an execution round-trip.
    """
    from . import tools as _tools

    tool_name = ctx.tool_name
    arguments = ctx.tool_arguments

    # Diagnostic: log every PRE_TOOL invocation with the keys we see —
    # makes it possible to verify _raw recovery from logs without adding
    # ad-hoc print statements. Costs ~1 line per tool call.
    try:
        _arg_keys = list(arguments.keys()) if isinstance(arguments, dict) else type(arguments).__name__
        logger.info("[lint_check] tool=%s arg_keys=%s", tool_name, _arg_keys)
    except Exception:
        pass

    # ── Recovery: extract args from {"_raw": "<json>"} fallback ──
    # Streaming token-by-token assembly in app/llm.py wraps the raw
    # buffer in ``{"_raw": ...}`` when it can't json.loads at end-of-
    # stream. That fallback fires even for valid-but-large payloads
    # because some providers split the JSON across more SSE chunks
    # than our buffer flushes — by the time we re-attempt the parse
    # here (post-stream, full string), it usually succeeds. Without
    # this recovery the agent sees "'path': 必填参数缺失" and has no
    # idea its tool call was actually well-formed.
    # Trigger recovery if args has `_raw` and NO real (non-underscore) keys.
    # Underscore-prefixed keys (_agent_profile / _caller_agent_id / agent_id)
    # are injected by agent_execution.py for some tools — they don't count
    # as "real" args from the LLM, so don't disqualify recovery.
    _has_raw = isinstance(arguments, dict) and "_raw" in arguments
    _real_keys = (
        [k for k in arguments.keys() if k != "_raw" and not k.startswith("_")]
        if isinstance(arguments, dict) else []
    )
    if _has_raw and not _real_keys:
        raw_str = arguments.get("_raw") or ""
        try:
            recovered = json.loads(raw_str) if raw_str else {}
            if isinstance(recovered, dict):
                arguments = recovered
                ctx.tool_arguments = recovered  # propagate to downstream middleware
                logger.info(
                    "[lint_check] recovered tool '%s' args from _raw "
                    "fallback (%d chars JSON)", tool_name, len(raw_str),
                )
        except json.JSONDecodeError as _je:
            # Genuinely broken JSON — give the LLM a SPECIFIC error so
            # it knows to retry the tool call with valid JSON, not waste
            # cycles wondering why required fields are "missing".
            _bad_schema = _find_tool_schema(tool_name)
            _bad_sig = _format_schema_signature(_bad_schema) if _bad_schema else ""
            _err_text = (
                f"Tool '{tool_name}' arguments JSON 解析失败: {_je}\n"
                f"收到的原始文本(前 200 字符): {raw_str[:200]!r}\n"
                f"请重新发起 tool call,确保 arguments 是合法 JSON 对象。"
            )
            if _bad_sig:
                _err_text += f"\n\n📋 工具签名: {_bad_sig}"
            return MiddlewareResult(
                action=Action.SHORT_CIRCUIT,
                value=_err_text,
                message=f"lint_check: _raw JSON parse failed ({_je})",
            )

    # Find the schema for this tool
    schema = _find_tool_schema(tool_name)
    if schema is None:
        # No schema found — skip validation (MCP tools, dynamic tools)
        return MiddlewareResult()

    params_schema = schema.get("parameters", {})
    errors = _validate_json_schema(arguments, params_schema, prefix="")

    if errors:
        # Pull an example from the tool's description (schema's GOTCHA /
        # Example block) AND build a compact signature from the schema
        # itself so the LLM sees a correct call alongside the diagnostic,
        # not just "validation failed". Claude Code error handling does
        # the same — error + signature + example is ~10x more recoverable
        # than error alone.
        example_hint = _extract_schema_example(schema)
        sig_hint = _format_schema_signature(schema)
        error_text = (
            f"Tool '{tool_name}' 参数校验失败:\n"
            + "\n".join(f"  - {e}" for e in errors[:5])
            + "\n请修正参数后重试。"
        )
        if sig_hint:
            error_text += f"\n\n📋 工具签名: {sig_hint}"
        if example_hint:
            error_text += f"\n\n✅ 正确调用示例:\n{example_hint}"
        return MiddlewareResult(
            action=Action.SHORT_CIRCUIT,
            value=error_text,
            message=f"lint_check: {len(errors)} error(s)",
        )

    return MiddlewareResult()


def _format_schema_signature(schema: dict) -> str:
    """Compact tool signature for tool-validation error messages.

    Thin shim over ``app.core.prompt_schemas.render_tool_signature``
    so middleware doesn't have to import the schema module at every
    call site. Returns "" on any error.
    """
    try:
        from .core.prompt_schemas import from_tool_definition, render_tool_signature
        return render_tool_signature(from_tool_definition(schema), multiline=True)
    except Exception:
        return ""


def _extract_schema_example(schema: dict) -> str:
    """Return a one-shot example snippet for this tool, if available.

    Looks in the following order:
      1. ``schema["example"]``                  — explicit field (preferred)
      2. ``schema["examples"][0]``              — array form
      3. A parse of ``schema["description"]``   — matches "Example:" line
    Returns "" if none found. Never raises.
    """
    if not isinstance(schema, dict):
        return ""
    ex = schema.get("example")
    if isinstance(ex, str) and ex.strip():
        return ex.strip()
    exs = schema.get("examples")
    if isinstance(exs, list) and exs:
        first = exs[0]
        if isinstance(first, str) and first.strip():
            return first.strip()
    # Fall back: scan description for "Example:" or similar marker.
    desc = schema.get("description") or ""
    if isinstance(desc, str):
        # Accept several common prefixes, first-match wins
        import re as _re
        m = _re.search(r"(?:^|\n)(?:Example|示例|正确示例)\s*[:：]\s*(.+?)(?=\n\n|\Z)",
                       desc, flags=_re.DOTALL)
        if m:
            snippet = m.group(1).strip()
            # Cap at ~600 chars — don't flood the LLM with an entire reference
            return snippet[:600]
    return ""


def _find_tool_schema(tool_name: str) -> dict | None:
    """Look up a tool's function schema from TOOL_DEFINITIONS."""
    try:
        from . import tools as _tools
        # Try alias resolution first
        canonical = _tools._TOOL_ALIASES.get(tool_name, tool_name)
        for defn in _tools.TOOL_DEFINITIONS:
            fn = defn.get("function", {})
            if fn.get("name") == canonical:
                return fn
    except Exception:
        pass
    return None


def _validate_json_schema(value: Any, schema: dict, prefix: str) -> list[str]:
    """Lightweight JSON Schema validation (subset). Returns list of error strings."""
    errors: list[str] = []
    if not isinstance(schema, dict):
        return errors

    schema_type = schema.get("type")

    # Check required properties
    if schema_type == "object":
        required = set(schema.get("required", []))
        properties = schema.get("properties", {})

        if not isinstance(value, dict):
            if required:
                errors.append(f"{prefix or 'root'}: 期望 object, 得到 {type(value).__name__}")
            return errors

        # Missing required fields
        for req in required:
            if req not in value and not req.startswith("_"):
                errors.append(f"{prefix}.{req}: 必填参数缺失" if prefix else f"'{req}': 必填参数缺失")

        # Type check each provided property
        for prop_name, prop_value in value.items():
            if prop_name.startswith("_"):
                continue  # Skip internal params
            if prop_name in properties:
                prop_schema = properties[prop_name]
                sub_prefix = f"{prefix}.{prop_name}" if prefix else prop_name
                errors.extend(_validate_single_type(prop_value, prop_schema, sub_prefix))

    return errors


def _validate_single_type(value: Any, schema: dict, name: str) -> list[str]:
    """Validate a single value against a property schema. Returns error list."""
    if not isinstance(schema, dict) or value is None:
        return []

    expected_type = schema.get("type")
    if not expected_type:
        return []

    errors: list[str] = []

    type_map = {
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "array": list,
        "object": dict,
    }

    expected_py_type = type_map.get(expected_type)
    if expected_py_type and not isinstance(value, expected_py_type):
        # Allow int where number is expected
        if expected_type == "number" and isinstance(value, (int, float)):
            pass
        # Allow string-encoded numbers/booleans (common LLM output)
        elif expected_type in ("number", "integer") and isinstance(value, str):
            try:
                float(value)  # Parseable — will be coerced by tool
            except ValueError:
                errors.append(f"'{name}': 期望 {expected_type}, 得到 \"{value}\"")
        else:
            errors.append(f"'{name}': 期望 {expected_type}, 得到 {type(value).__name__}")

    # Enum check
    enum_values = schema.get("enum")
    if enum_values and value not in enum_values:
        errors.append(f"'{name}': 值 \"{value}\" 不在允许范围 {enum_values}")

    return errors


# ====== P1: Context Compaction Middleware ======

def context_compaction_check(ctx: MiddlewareContext) -> MiddlewareResult:
    """PRE_LLM: Check if context needs compaction before sending to LLM.

    Instead of implementing compression here (that's in agent._compress_context),
    this middleware sets a signal in ctx.data so the caller knows to trigger
    compaction. This keeps the middleware pure and side-effect-free.

    Thresholds:
      - SOFT (50%): set data["compaction_needed"] = "soft" — suggest compression
      - HARD (75%): set data["compaction_needed"] = "hard" — force compression
      - CRITICAL (90%): set data["compaction_needed"] = "critical" — aggressive trim
    """
    messages = ctx.messages
    if not messages:
        return MiddlewareResult()

    # Estimate tokens: ~4 chars per token (rough but fast)
    total_chars = sum(len(str(m.get("content", ""))) for m in messages)
    estimated_tokens = total_chars // 4

    # Get context limit from data bag (caller provides it)
    context_limit = ctx.data.get("context_limit", 128000)

    ratio = estimated_tokens / max(context_limit, 1)

    if ratio >= CONTEXT_WARN_HIGH:
        ctx.data["compaction_needed"] = "critical"
        ctx.data["compaction_ratio"] = ratio
        ctx.data["estimated_tokens"] = estimated_tokens
        return MiddlewareResult(
            message=f"context at {ratio:.0%} — critical compaction needed",
            modified_ctx=True,
        )
    elif ratio >= CONTEXT_WARN_MEDIUM:
        ctx.data["compaction_needed"] = "hard"
        ctx.data["compaction_ratio"] = ratio
        ctx.data["estimated_tokens"] = estimated_tokens
        return MiddlewareResult(
            message=f"context at {ratio:.0%} — hard compaction needed",
            modified_ctx=True,
        )
    elif ratio >= CONTEXT_WARN_LOW:
        ctx.data["compaction_needed"] = "soft"
        ctx.data["compaction_ratio"] = ratio
        ctx.data["estimated_tokens"] = estimated_tokens
        return MiddlewareResult(
            message=f"context at {ratio:.0%} — soft compaction suggested",
            modified_ctx=True,
        )

    return MiddlewareResult()


# ====== P1: Smart Model Router Middleware ======

def smart_model_router(ctx: MiddlewareContext) -> MiddlewareResult:
    """PRE_LLM: Analyze message complexity and route to appropriate model.

    Criteria (combined heuristic score):
      1. Message length (chars) — proxy for task complexity
      2. Tool call density in recent messages — complex workflows need better models
      3. Keyword signals — "分析", "设计", "重构", "debug" etc. → complex
      4. Image/multimodal content → needs vision model

    Writes routing decision to ctx.data["model_route"] for the caller.
    Does NOT directly override ctx.model — caller decides whether to apply.
    """
    messages = ctx.messages
    if not messages:
        return MiddlewareResult()

    # Get the last user message
    last_user_msg = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            content = m.get("content", "")
            if isinstance(content, str):
                last_user_msg = content
            elif isinstance(content, list):
                last_user_msg = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict)
                )
            break

    # ── Heuristic Scoring ──
    score = 0  # 0-100, higher = more complex

    # 1. Message length
    msg_len = len(last_user_msg)
    if msg_len > 3000:
        score += 30
    elif msg_len > 1000:
        score += 15
    elif msg_len > 300:
        score += 5

    # 2. Recent tool call density (complex workflows)
    recent = messages[-20:] if len(messages) > 20 else messages
    tool_calls = sum(1 for m in recent if m.get("role") == "tool")
    if tool_calls > 10:
        score += 20
    elif tool_calls > 5:
        score += 10

    # 3. Complexity keywords
    complexity_keywords = {
        "zh": ["分析", "设计", "架构", "重构", "优化", "调试", "debug", "性能",
               "安全", "漏洞", "review", "评审", "规划", "方案", "对比"],
        "en": ["analyze", "design", "architect", "refactor", "optimize",
               "debug", "performance", "security", "vulnerability", "review",
               "plan", "compare", "evaluate", "strategy"],
    }
    lower_msg = last_user_msg.lower()
    keyword_hits = 0
    for lang_keywords in complexity_keywords.values():
        for kw in lang_keywords:
            if kw in lower_msg:
                keyword_hits += 1
    score += min(keyword_hits * 5, 25)

    # 4. Conversation depth (longer = likely more complex context)
    if len(messages) > 40:
        score += 10
    elif len(messages) > 20:
        score += 5

    # 5. Check for multimodal content
    is_multimodal = False
    for m in messages[-3:]:
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in ("image_url", "image"):
                    is_multimodal = True
                    break

    # ── Routing Decision ──
    if is_multimodal:
        route = "multimodal"
    elif score >= 60:
        route = "complex"
    elif score <= 15:
        route = "simple"
    else:
        route = "default"

    ctx.data["model_route"] = route
    ctx.data["model_route_score"] = score
    ctx.data["model_route_multimodal"] = is_multimodal

    return MiddlewareResult(
        message=f"route={route} score={score} multimodal={is_multimodal}",
        modified_ctx=True,
    )


# ====== P0: Tool Result Truncation (POST_TOOL) ======

def tool_result_truncation(ctx: MiddlewareContext) -> MiddlewareResult:
    """POST_TOOL: Truncate excessively long tool results.

    Many tools (bash, read_file) can return huge outputs that waste tokens.
    This middleware ensures results stay within a reasonable size.
    """
    result = ctx.tool_result
    if not isinstance(result, str):
        return MiddlewareResult()

    max_chars = ctx.data.get("max_tool_result_chars", MAX_TOOL_RESULT_CHARS)
    if len(result) <= max_chars:
        return MiddlewareResult()

    # Keep head + tail, insert truncation marker
    keep_head = int(max_chars * 0.7)
    keep_tail = int(max_chars * 0.2)
    truncated = (
        result[:keep_head]
        + f"\n\n... [截断：结果过长，{len(result)} 字符，已保留头尾部分] ...\n\n"
        + result[-keep_tail:]
    )
    ctx.tool_result = truncated
    return MiddlewareResult(
        message=f"truncated {len(result)} → {len(truncated)} chars",
        modified_ctx=True,
    )


# ─────────────────────────────────────────────────────────────
# 全局 Pipeline 实例 + 初始化
# ─────────────────────────────────────────────────────────────

_GLOBAL_PIPELINE: MiddlewarePipeline | None = None


def long_task_isolation_check(ctx: MiddlewareContext) -> MiddlewareResult:
    """PRE_TOOL: Block sub-task agents from writing outside their wd.

    No-op for non-sub-task agents (vast majority). For agents whose
    current task is a long-task sub-task, calls
    ``app.long_task.isolation.check_write_path`` and SHORT_CIRCUITs
    with a clear error if the write target escapes the wd.
    """
    if not ctx.agent_id:
        return MiddlewareResult(action=Action.CONTINUE)
    try:
        from .hub import get_hub
        from .long_task.isolation import check_write_path
    except Exception:
        return MiddlewareResult(action=Action.CONTINUE)
    try:
        hub = get_hub()
        agent = hub.get_agent(ctx.agent_id) if hub else None
    except Exception:
        return MiddlewareResult(action=Action.CONTINUE)
    if agent is None:
        return MiddlewareResult(action=Action.CONTINUE)
    err = check_write_path(
        agent=agent,
        tool_name=ctx.tool_name,
        args=ctx.tool_arguments if isinstance(ctx.tool_arguments, dict) else {},
    )
    if err:
        return MiddlewareResult(action=Action.SHORT_CIRCUIT, value=err)
    return MiddlewareResult(action=Action.CONTINUE)


def init_pipeline() -> MiddlewarePipeline:
    """Create and configure the global middleware pipeline with built-in middlewares."""
    global _GLOBAL_PIPELINE
    pipe = MiddlewarePipeline()

    # ── P0: PRE_TOOL ──
    pipe.register(MiddlewareEntry(
        name="tool_lint_check",
        stage=Stage.PRE_TOOL,
        fn=tool_lint_check,
        priority=10,
        description="校验工具参数是否符合 JSON Schema",
    ))
    # Long-task subsystem write-path isolation. Runs after lint so the
    # arguments are already _raw-recovered by the time we inspect the
    # path arg. Priority 20 puts it after lint.
    pipe.register(MiddlewareEntry(
        name="long_task_isolation",
        stage=Stage.PRE_TOOL,
        fn=long_task_isolation_check,
        priority=20,
        description="子任务 agent 写入路径隔离 (长任务子系统)",
    ))

    # ── P0: POST_TOOL ──
    pipe.register(MiddlewareEntry(
        name="tool_result_truncation",
        stage=Stage.POST_TOOL,
        fn=tool_result_truncation,
        priority=10,
        description="截断过长的工具返回结果",
    ))

    # ── P1: PRE_LLM ──
    pipe.register(MiddlewareEntry(
        name="context_compaction_check",
        stage=Stage.PRE_LLM,
        fn=context_compaction_check,
        priority=10,
        description="检测上下文是否需要压缩",
    ))
    pipe.register(MiddlewareEntry(
        name="smart_model_router",
        stage=Stage.PRE_LLM,
        fn=smart_model_router,
        priority=20,
        description="根据任务复杂度建议模型路由",
    ))

    _GLOBAL_PIPELINE = pipe
    logger.info("Middleware pipeline initialized with %d entries",
                 len(pipe.list_entries()))
    return pipe


def get_pipeline() -> MiddlewarePipeline | None:
    """Return the global pipeline (None if not initialized)."""
    return _GLOBAL_PIPELINE


def ensure_pipeline() -> MiddlewarePipeline:
    """Return the global pipeline, initializing it if needed."""
    global _GLOBAL_PIPELINE
    if _GLOBAL_PIPELINE is None:
        return init_pipeline()
    return _GLOBAL_PIPELINE
