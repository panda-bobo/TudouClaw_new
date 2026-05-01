"""L3 long-term semantic memory extractor.

Single-purpose module — extracts durable facts from agent activity and
upserts them into the L3 store. Deliberately isolated from
``app/core/memory.py`` so the storage layer stays pure CRUD.

Trigger model (callers wire these):
  * **Task DONE** (primary): when an agent task or project task transitions
    to DONE, the caller invokes ``extract_on_task_done()`` with the task
    summary + recent message window.
  * **Strong signal** (immediate): when the user message contains explicit
    "remember/记住/以后/永远/never/always" phrasing,
    ``extract_on_strong_signal()`` fires this turn — preferences are
    timeless and shouldn't wait for task completion.
  * **K-turn fallback**: pure-chat sessions that never produce a task DONE
    still need to capture preferences. ``should_run_fallback()`` returns
    True after K turns of silence so the caller can fire extraction with a
    rolling message window.

Design constraints (locked):
  * Hardcoded prompt — no operator ``extra_context``, no agent persona
    contamination. The extractor sees ONLY this module's prompt.
  * No tools — pure ``(text in) → (JSON out)`` LLM call.
  * Dedup is local (vector/bigram via ``store.upsert_fact``), NOT by
    sending existing facts to the LLM (token cost would scale with L3
    size).
  * Quality gates run on every extracted candidate: confidence floor,
    bad-phrase blocklist, plan-step prefix rejection, category whitelist,
    outcome-must-have-substance check.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable, Optional

from .memory import SemanticFact, get_memory_manager

logger = logging.getLogger("tudouclaw.l3_extractor")

# ---------------------------------------------------------------------------
# Module-level state — K-turn fallback bookkeeping
# ---------------------------------------------------------------------------

# agent_id → unix timestamp of last successful extraction (any path).
_LAST_EXTRACT_AT: dict[str, float] = {}

# agent_id → turns elapsed since last successful extraction. Caller calls
# ``note_chat_turn(agent_id)`` once per chat write-back; ``should_run_fallback``
# returns True when this crosses ``K_TURN_FALLBACK``.
_TURNS_SINCE_EXTRACT: dict[str, int] = {}

# Threshold for the K-turn fallback trigger. Pure-chat sessions (no task
# DONE, no strong signals) need this safety net or preferences are never
# captured.
K_TURN_FALLBACK = 20

# Strong-signal trigger phrases — match user message text. Any hit fires
# immediate extraction (this turn, before task completion). Case-insensitive.
_STRONG_SIGNALS = (
    "记住", "以后", "永远", "下次都", "每次都",
    "禁止", "不要", "别再", "不准",
    "remember", "always", "never", "don't", "do not",
    "from now on", "going forward",
)

# ---------------------------------------------------------------------------
# The extractor prompts — HARDCODED, language-paired. Do not pull from
# operator config or agent persona. If you need to tune extraction behavior,
# edit these strings directly in code review.
# ---------------------------------------------------------------------------

L3_EXTRACTION_PROMPT_ZH = """你是 L3 长期记忆抽取器。L3 = 跨会话仍有用的事实,不是日志。

【六类 L3 定义】
- preference: 用户长期画像 / 偏好 / 风格约束 / 禁忌
  例: "用户偏好: 报告类输出统一生成 PPT,完成后发到邮箱"
  例: "用户偏好: 回复用中文,直接给结论不铺垫"
  例: "用户禁忌: 不使用 emoji,不写多段总结"

- rule: 操作硬约束 / 踩坑教训 / 前置条件(下次能少走弯路)
  例: "调 web_search 必须传 max_results,默认无限拉会爆 token"
  例: "写文件必须在 ~/.tudou_claw/workspaces/<agent_id>/ 下,禁止根目录或 /tmp"
  例: "调 pptxgenjs 前必须 npm i -g pptxgenjs,否则报 module not found"

- intent: 用户的真实长期目标 / 约束 / 成功标准(不是单次任务)
  例: "用户长期目标: 把项目做成多 agent 协作平台,优先稳定性"
  ✗ "用户要生成一个 PPT" — 这是单次任务,L2 自然就有了

- reasoning: 选型理由 / 假设(避免下次重新讨论)
  例: "本项目选 SQLite 而非 Postgres,理由:单机部署 + 无运维"

- outcome: 任务的物质产出(必须含具体路径/文件名/版本/时间)
  例: "2026-04-26 生成 Q2 财报 PPT,路径 ~/Desktop/Q2_finance.pptx,32页"
  ✗ "执行了 write_file 操作" — 这是 L2,不是 L3
  ✗ "生成了报告" — 没有具体物质标记,不抽

- reflection: 改进 / 优化 / 流程心得
  例: "下次先 grep 再 read,直接 read 整个文件浪费 token"

【绝对不要记】
- 一次性命令的执行细节(L2 范畴)
- 代码里能 grep 出来的事实(文件路径细节、变量名)
- "[步骤N]" 开头的内容
- "无法分析 / 缺少信息 / 状态已更新 / 操作执行完毕" 这类空话
- 和当前对话上下文等价的"现状"陈述

【confidence 评分标准】
- 1.0  = 用户原话明确说出
- 0.8  = 强推断(用户行为/语气清晰可见)
- 0.5  = 弱推断,可能性大
- <0.5 = 不要输出

【输出格式】
严格 JSON 数组,无高价值返回 [] 即可:
[{"content": "自包含一句话,脱离上下文也能看懂", "category": "preference|rule|intent|reasoning|outcome|reflection", "confidence": 0.0-1.0}]

不要输出任何解释 / markdown 代码块标记 / 注释。只输出 JSON 数组本身。
"""

L3_EXTRACTION_PROMPT_EN = """You are an L3 long-term memory extractor. L3 = facts useful across sessions, not logs.

[Six L3 categories]
- preference: User's long-term profile / preferences / style constraints / taboos.
  Ex: "User prefers: deliver reports as PPT and email them, not paste text"
  Ex: "User prefers: reply in English, give the conclusion first without preamble"
  Ex: "User taboo: no emoji, no multi-paragraph summaries"

- rule: Hard operational constraints / lessons learned / preconditions (so we don't trip again).
  Ex: "web_search must be called with max_results — default fetches unbounded and blows the token budget"
  Ex: "Write files only under ~/.tudou_claw/workspaces/<agent_id>/ — never root or /tmp"
  Ex: "Run `npm i -g pptxgenjs` before calling it, or it errors with 'module not found'"

- intent: User's real long-term goal / constraint / success criteria (NOT a single-task ask).
  Ex: "User's long-term goal: build the project into a multi-agent coordination platform, stability first"
  ✗ "User wants a PPT" — this is a single task, L2 already captures it

- reasoning: Selection rationale / assumptions (so we don't re-litigate next time).
  Ex: "This project chose SQLite over Postgres because: single-machine deploy + no ops"

- outcome: Concrete task deliverables (MUST include a substance marker — path / filename / version / date).
  Ex: "2026-04-26 generated Q2 finance PPT at ~/Desktop/Q2_finance.pptx, 32 pages"
  ✗ "Executed write_file" — that's L2
  ✗ "Generated a report" — no concrete substance marker, do not record

- reflection: Improvements / optimizations / process lessons.
  Ex: "Next time grep before reading whole files — reading wastes tokens"

[Never record]
- One-shot command execution details (L2 territory)
- Facts you can grep from the code (file paths, variable names)
- Anything starting with "[Step N]" or "[步骤N]"
- Vacuous fillers: "unable to analyze", "missing info", "status updated, no failure", "operation completed"
- "Current state" descriptions equivalent to the live conversation context

[Confidence scale]
- 1.0  = User said it explicitly
- 0.8  = Strong inference (clear behavior / tone)
- 0.5  = Weak inference, plausible
- <0.5 = Do not output

[Output format]
Strict JSON array. If nothing high-value, just output []:
[{"content": "self-contained single sentence understandable out of context", "category": "preference|rule|intent|reasoning|outcome|reflection", "confidence": 0.0-1.0}]

Do not output any explanation, markdown code fences, or comments. Output the JSON array only.
"""


def _select_prompt(lang: str) -> str:
    """Return the language-appropriate extractor prompt. Default to ZH for
    backward compat with existing TudouClaw deployments (which are
    Chinese-first); English agents (lang starting with 'en') get the EN
    version."""
    if (lang or "").lower().startswith("en"):
        return L3_EXTRACTION_PROMPT_EN
    return L3_EXTRACTION_PROMPT_ZH

# ---------------------------------------------------------------------------
# Quality gates (kept in-module; mirror what memory.py used to enforce inline)
# ---------------------------------------------------------------------------

_BAD_PHRASES = (
    "无法确定", "无法分析", "未知日期", "[未知日期]",
    "[日期] 无法", "[日期]无法",
    "缺少日期", "缺少最终结果", "缺少关键信息",
    "状态已更新无失败", "无失败原因",
    "执行了该步骤", "状态已更新, 无失败",
    "operation completed", "status updated, no failure",
)

_VALID_CATEGORIES = (
    "preference", "rule", "intent", "reasoning", "outcome", "reflection",
)

# Outcome must contain at least one substance marker (path/file/version/date/url).
_OUTCOME_SUBSTANCE_RE = re.compile(
    r"(/[\w./-]+|\.\w{2,5}\b|v\d+|\d{4}-\d{2}-\d{2}|http[s]?://|\d+\s*(行|页|条|个))"
)

_DEDUP_THRESHOLD = 0.85  # higher than memory.py default (0.75) — we want
                          # tighter dedup since we're not sending facts to LLM


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def has_strong_signal(text) -> bool:
    """True iff ``text`` contains an explicit "remember this" trigger phrase.

    Used by the chat write-back path to fire extraction immediately on the
    current turn (don't wait for task DONE / fallback).

    Tolerates non-string ``text`` (e.g. multimodal vision turns where the
    user message is a list of content blocks). Returns False rather than
    raising — this keeps a vision turn from poisoning the memory pipeline.
    """
    if not text:
        return False
    if not isinstance(text, str):
        # Multimodal list / dict — pull text-only parts if possible.
        try:
            if isinstance(text, list):
                parts = []
                for p in text:
                    if isinstance(p, dict) and p.get("type") == "text":
                        parts.append(str(p.get("text", "")))
                    elif isinstance(p, str):
                        parts.append(p)
                text = " ".join(parts)
            else:
                text = str(text)
        except Exception:
            return False
    if not text:
        return False
    low = text.lower()
    return any(s in low for s in _STRONG_SIGNALS)


def note_chat_turn(agent_id: str) -> None:
    """Increment the per-agent fallback turn counter. Call once per chat
    write-back, before deciding whether to fire fallback extraction."""
    _TURNS_SINCE_EXTRACT[agent_id] = _TURNS_SINCE_EXTRACT.get(agent_id, 0) + 1


def should_run_fallback(agent_id: str) -> bool:
    """True iff this agent has gone ``K_TURN_FALLBACK`` write-back turns
    without any successful extraction (task DONE / strong signal /
    previous fallback). Time-cooldown also enforced (no re-fire within
    5 min) to avoid burst fires when many turns happen quickly."""
    if _TURNS_SINCE_EXTRACT.get(agent_id, 0) < K_TURN_FALLBACK:
        return False
    last_at = _LAST_EXTRACT_AT.get(agent_id, 0.0)
    if time.time() - last_at < 300:
        return False
    return True


def extract_on_task_done(agent_id: str,
                         task_title: str,
                         task_description: str,
                         task_result: str,
                         recent_messages: list[dict],
                         llm_call: Callable[[str], str],
                         store: Optional[Any] = None,
                         lang: str = "zh") -> list[SemanticFact]:
    """Primary trigger — invoked when an agent or project task transitions
    to DONE. Builds task-scoped context, calls the extractor LLM, and
    upserts surviving facts.

    ``lang`` selects between the ZH/EN extractor prompts; pass the agent's
    ``profile.language`` (defaults to ZH for any non-en value).

    Fails silently on any error — extraction must NEVER bubble up and
    break the calling business logic (task status transition).
    """
    if store is None:
        store = get_memory_manager()
    if store is None or not llm_call:
        return []
    context = _build_task_context(task_title, task_description,
                                  task_result, recent_messages, lang=lang)
    return _run_extraction(agent_id, context, llm_call, store, lang=lang,
                           source_label=f"task_done:{task_title[:40]}")


def extract_on_strong_signal(agent_id: str,
                             user_message: str,
                             assistant_response: str,
                             llm_call: Callable[[str], str],
                             store: Optional[Any] = None,
                             lang: str = "zh") -> list[SemanticFact]:
    """Strong-signal trigger — user explicitly said "remember X / 以后都...".

    Skips the task-completion gate and fires this turn. Recent messages
    are not bundled (the relevant signal is in the just-spoken turn)."""
    if store is None:
        store = get_memory_manager()
    if store is None or not llm_call:
        return []
    if (lang or "").lower().startswith("en"):
        context = (
            "[Strong signal — user explicitly asked to remember]\n"
            f"User: {user_message[:1500]}\n"
            f"Assistant: {assistant_response[:1500]}"
        )
    else:
        context = (
            "【强信号触发 — 用户明确要求记住】\n"
            f"用户: {user_message[:1500]}\n"
            f"助手: {assistant_response[:1500]}"
        )
    return _run_extraction(agent_id, context, llm_call, store, lang=lang,
                           source_label="strong_signal")


def extract_fallback(agent_id: str,
                     recent_messages: list[dict],
                     llm_call: Callable[[str], str],
                     store: Optional[Any] = None,
                     lang: str = "zh") -> list[SemanticFact]:
    """K-turn fallback — pure chat sessions with no task boundary.

    Caller decides when to call (after ``should_run_fallback`` returns True).
    """
    if store is None:
        store = get_memory_manager()
    if store is None or not llm_call:
        return []
    if not recent_messages:
        return []
    if (lang or "").lower().startswith("en"):
        context = (
            f"[Fallback trigger — {K_TURN_FALLBACK}+ turns since last extraction]\n"
            "Recent dialogue window:\n\n"
            f"{_format_messages_window(recent_messages)}"
        )
    else:
        context = (
            f"【兜底触发 — 距离上次抽取已 {K_TURN_FALLBACK}+ 轮】\n"
            "以下是最近的对话窗口:\n\n"
            f"{_format_messages_window(recent_messages)}"
        )
    return _run_extraction(agent_id, context, llm_call, store, lang=lang,
                           source_label="fallback_kturn")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _build_task_context(title: str, description: str, result: str,
                        recent_messages: list[dict],
                        lang: str = "zh") -> str:
    """Compose the LLM-facing task summary block. Labels are localized;
    the schema/prompt itself is selected upstream by ``_select_prompt``."""
    en = (lang or "").lower().startswith("en")
    if en:
        parts: list[str] = ["[Task DONE — please extract L3 memory]"]
        if title:
            parts.append(f"Task: {title[:200]}")
        if description:
            parts.append(f"Description: {description[:500]}")
        if result:
            parts.append(f"Final result: {result[:1500]}")
        if recent_messages:
            parts.append("\nRelated dialogue window:")
            parts.append(_format_messages_window(recent_messages))
    else:
        parts = ["【任务完成,请抽取 L3 记忆】"]
        if title:
            parts.append(f"任务: {title[:200]}")
        if description:
            parts.append(f"描述: {description[:500]}")
        if result:
            parts.append(f"最终结果: {result[:1500]}")
        if recent_messages:
            parts.append("\n相关对话窗口:")
            parts.append(_format_messages_window(recent_messages))
    return "\n".join(parts)


def _format_messages_window(messages: list[dict], max_msgs: int = 6,
                            max_chars_per_msg: int = 400) -> str:
    """Render a slice of the message list as text for the extractor prompt.

    Skips tool-call/result messages — they're noisy and rarely contain
    L3-worthy signal. Keeps user + assistant text only.
    """
    out: list[str] = []
    seen = 0
    for m in reversed(messages or []):
        if seen >= max_msgs:
            break
        if not isinstance(m, dict):
            continue
        role = m.get("role", "")
        if role not in ("user", "assistant"):
            continue
        content = m.get("content", "")
        if not isinstance(content, str) or not content.strip():
            continue
        out.append(f"[{role}] {content[:max_chars_per_msg]}")
        seen += 1
    return "\n".join(reversed(out))


def _run_extraction(agent_id: str, context: str,
                    llm_call: Callable[[str], str],
                    store: Any, source_label: str,
                    lang: str = "zh") -> list[SemanticFact]:
    """Single shared pipeline: prompt → LLM → parse → quality gate → upsert."""
    prompt = f"{_select_prompt(lang)}\n\n{context}"
    try:
        raw = llm_call(prompt)
    except Exception as e:  # noqa: BLE001
        logger.warning("L3 extraction LLM call failed (%s): %s",
                       source_label, e)
        return []

    candidates = _parse_output(raw)
    if not candidates:
        return []

    saved: list[SemanticFact] = []
    now = time.time()
    source_str = f"{source_label}@{time.strftime('%Y-%m-%d %H:%M', time.localtime(now))}"

    for fd in candidates:
        passed, reason = _quality_gate(fd)
        if not passed:
            logger.debug("L3 reject (%s): %s", reason,
                         str(fd.get("content", ""))[:60])
            continue

        fact = SemanticFact(
            agent_id=agent_id,
            category=fd["category"],
            content=fd["content"],
            source=source_str,
            confidence=float(fd.get("confidence", 0.7)),
        )
        try:
            outcome = store.upsert_fact(fact, threshold=_DEDUP_THRESHOLD,
                                        prefer_category_match=True)
            if outcome.get("action") in ("inserted", "updated"):
                saved.append(fact)
        except Exception as e:  # noqa: BLE001
            logger.warning("L3 upsert failed for fact %r: %s",
                           fact.content[:60], e)

    if saved:
        _LAST_EXTRACT_AT[agent_id] = now
        _TURNS_SINCE_EXTRACT[agent_id] = 0  # reset fallback counter
        logger.info("L3 extracted %d fact(s) for agent %s via %s",
                    len(saved), agent_id, source_label)
    return saved


def _parse_output(raw: str) -> list[dict]:
    """Strip code fences if present, then JSON-parse. Tolerant of trailing
    text — extracts the first JSON array we can find."""
    if not raw or not raw.strip():
        return []
    s = raw.strip()
    # Strip ```json / ``` fences (LLM often adds these despite instructions).
    if s.startswith("```"):
        lines = s.split("\n")
        # drop opening fence
        lines = lines[1:]
        # drop closing fence if present
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        s = "\n".join(lines).strip()

    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        # Last-ditch: find first [...] block
        m = re.search(r"\[\s*\{.*?\}\s*\]", s, re.DOTALL)
        if not m:
            return []
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return []

    if not isinstance(data, list):
        return []
    # Coerce each item to dict; drop non-dicts
    return [d for d in data if isinstance(d, dict)]


def _quality_gate(fd: dict) -> tuple[bool, str]:
    """Return (passed, rejection_reason). Mirrors the gates that used to live
    inline in memory.extract_facts, plus a new outcome-substance check."""
    content = str(fd.get("content", "")).strip()
    if not content:
        return False, "empty"
    if len(content) < 10:
        return False, "too_short"
    # Bad-phrase blocklist
    if any(p in content for p in _BAD_PHRASES):
        return False, "bad_phrase"
    # Plan-step leakage
    if content.startswith("[步骤") or content.startswith("[Step "):
        return False, "plan_step_leak"
    # Category whitelist
    cat = fd.get("category", "")
    if cat not in _VALID_CATEGORIES:
        return False, f"bad_category:{cat!r}"
    # Confidence floor
    try:
        conf = float(fd.get("confidence", 0.7))
    except (TypeError, ValueError):
        return False, "bad_confidence_type"
    if conf < 0.5:
        return False, f"low_confidence:{conf:.2f}"
    # Outcome must include a substance marker (path/file/version/date/url/count)
    if cat == "outcome" and not _OUTCOME_SUBSTANCE_RE.search(content):
        return False, "outcome_no_substance"
    return True, ""
