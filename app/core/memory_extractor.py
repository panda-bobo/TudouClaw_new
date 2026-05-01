"""零-LLM 事实抽取 — Pattern + 切词 + (可选) 预处理模型兜底。

设计目标：在 ``MemoryManager.extract_facts`` 走 LLM 之前先跑一遍
确定性抽取，把 60%+ 的高频"用户偏好 / 经验规则"句式直接命中，让
LLM 调用频率（成本最高的一段）下降到原来的 ~30%。

灵感来源：gbrain 的 signal-detector 思路 —— 模式优先，LLM 兜底。

抽取目标：
  * **preference** —— 用户偏好 / 禁忌（"我喜欢 X" / "禁止 X" / "不要 X"）
  * **rule** —— 经验规则 / 行为指令（"记住 X" / "下次 X" / "always/never"）
  * **contact** —— email / phone / URL（已经在
    ``MemoryManager._extract_contacts_deterministic`` 处理，这里**不**重复）

不在这里抽：
  * intent / reasoning / outcome / reflection —— 这些需要语义理解，
    交给 LLM。零-LLM 提取专注于"句式高度规律"的两类。

输出形态：
    [{
        "content":    "...",
        "category":   "preference" | "rule",
        "confidence": 0.7-0.95,
        "source":     "pattern:<rule_id>",   # 便于 audit & 自愈循环
        "raw_match":  "...",                  # 命中的原始片段，便于调试
    }, ...]

调用方应自行调 ``MemoryManager.upsert_fact`` 把它们写进 L3。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass

logger = logging.getLogger("tudou.memory_extractor")


# ──────────────────────────────────────────────────────────────────────
# Pattern 目录
# ──────────────────────────────────────────────────────────────────────
# 每条 pattern 描述：
#   * id           —— 内部标识，写到 fact.source 便于追踪
#   * regex        —— 一个有 group 的正则；group(1) 必须是"内容主体"
#                     （即提取出来作为 fact 内容的部分）
#   * category     —— preference | rule
#   * confidence   —— 0.7-0.95；强信号给高分，模糊信号给低分
#   * lang         —— 'zh' | 'en'，便于按语言开关
#   * prefix       —— 写到 fact.content 前面的标签（"用户偏好:" / "规则:"）
#
# 强信号词（confidence ≥ 0.9）："禁止 / 必须 / 一定要 / 记住 / 永远 /
#                                never / always (instructional)"
# 中信号词（0.85）："喜欢 / 不喜欢 / 倾向 / 习惯 / 下次 / prefer"
# 弱信号词（≤ 0.75）："好像 / 觉得 / 我看 / 似乎"


@dataclass
class _Pattern:
    id: str
    regex: re.Pattern
    category: str
    confidence: float
    lang: str
    prefix: str


# 内容截断：fact 的主体部分太长会污染 L3，统一上限 80 字符
_CONTENT_MAX = 80


def _build_patterns() -> list[_Pattern]:
    """编译模式列表。运行时只编译一次（模块加载时）。"""
    out: list[_Pattern] = []

    # ── 中文 · preference 强信号 ──
    for rid, pat in [
        ("zh.deny.strict",   r"(?:禁止|不许|不能|不准)\s*(.{2,80}?)(?:[。!?\n]|$)"),
        ("zh.must",          r"(?:必须|务必|一定要|绝对要)\s*(.{2,80}?)(?:[。!?\n]|$)"),
        ("zh.never",         r"(?:永远不|从来不|绝不)\s*(.{2,80}?)(?:[。!?\n]|$)"),
    ]:
        out.append(_Pattern(
            id=rid,
            regex=re.compile(pat),
            category="preference",
            confidence=0.92,
            lang="zh",
            prefix="用户偏好",
        ))

    # ── 中文 · preference 中信号 ──
    for rid, pat in [
        ("zh.like",          r"我(?:喜欢|喜爱|偏好|偏爱|倾向于?|习惯|总是)\s*(.{2,80}?)(?:[。!?\n,，、]|$)"),
        ("zh.dislike",       r"我(?:不喜欢|讨厌|反感|不想|不要)\s*(.{2,80}?)(?:[。!?\n,，、]|$)"),
        ("zh.dont",          r"(?:不要|别|请勿|请不要)\s*(.{2,80}?)(?:[。!?\n]|$)"),
    ]:
        out.append(_Pattern(
            id=rid,
            regex=re.compile(pat),
            category="preference",
            confidence=0.85,
            lang="zh",
            prefix="用户偏好",
        ))

    # ── 中文 · rule 强信号 ──
    for rid, pat in [
        ("zh.remember",      r"(?:记住|请记住|你要记住|记得)\s*(?:[:：])?\s*(.{4,80}?)(?:[。!?\n]|$)"),
        ("zh.next.time",     r"(?:下次|下一次|以后|之后|今后)\s*(.{4,80}?)(?:[。!?\n]|$)"),
        ("zh.before",        r"(.{2,40}?)\s*(?:之前|前面)\s*(?:先|应该|必须)\s*(.{2,40}?)(?:[。!?\n]|$)"),
    ]:
        out.append(_Pattern(
            id=rid,
            regex=re.compile(pat),
            category="rule",
            confidence=0.90,
            lang="zh",
            prefix="规则",
        ))

    # ── 英文 · preference ──
    for rid, pat, conf in [
        ("en.prefer",   r"\bI\s+(?:prefer|like|love|enjoy)\s+(.{4,80}?)(?:[.!?\n,]|$)", 0.85),
        ("en.dont",     r"\bI\s+(?:don't|do\s+not|hate|dislike)\s+(?:want|like|need)?\s*(.{4,80}?)(?:[.!?\n,]|$)", 0.85),
        ("en.never",    r"\b(?:never|do\s+not\s+ever)\s+(.{4,80}?)(?:[.!?\n,]|$)", 0.88),
        ("en.always",   r"\bI\s+always\s+(.{4,80}?)(?:[.!?\n,]|$)", 0.85),
    ]:
        out.append(_Pattern(
            id=rid,
            regex=re.compile(pat, re.IGNORECASE),
            category="preference",
            confidence=conf,
            lang="en",
            prefix="user preference",
        ))

    # ── 英文 · rule ──
    for rid, pat, conf in [
        ("en.remember",  r"\b(?:remember|keep in mind|note that)\s+(?:that\s+)?(.{4,80}?)(?:[.!?\n]|$)", 0.88),
        ("en.next.time", r"\bnext\s+time\s+(.{4,80}?)(?:[.!?\n]|$)", 0.88),
        ("en.from.now",  r"\bfrom\s+now\s+on\s*,?\s*(.{4,80}?)(?:[.!?\n]|$)", 0.90),
        ("en.do.not",    r"\b(?:do\s+not|don't)\s+(.{4,80}?)(?:[.!?\n]|$)", 0.85),
    ]:
        out.append(_Pattern(
            id=rid,
            regex=re.compile(pat, re.IGNORECASE),
            category="rule",
            confidence=conf,
            lang="en",
            prefix="rule",
        ))

    return out


_PATTERNS = _build_patterns()


# ──────────────────────────────────────────────────────────────────────
# 抽取入口
# ──────────────────────────────────────────────────────────────────────


def _normalize_content(s: str) -> str:
    """trim + 折叠空白 + 截断长度。"""
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip("，。！？,.!?:：;；")
    if len(s) > _CONTENT_MAX:
        s = s[:_CONTENT_MAX].rstrip() + "…"
    return s


def _is_too_short(s: str) -> bool:
    # 抓到的内容太短 → 常常是 regex 误命中（"我喜欢" 后只跟一个字）
    if len(s) < 4:
        return True
    return False


def _signal_strength_boost(text: str, lang: str) -> float:
    """命中后再扫一遍上下文：如果出现"非常 / 一定 / 严禁 / strictly /
    absolutely" 这种强化词，把 confidence 再加 0.03。"""
    boost_words_zh = ("非常", "一定", "严禁", "绝对", "千万")
    boost_words_en = ("strictly", "absolutely", "definitely", "must")
    words = boost_words_zh if lang == "zh" else boost_words_en
    return 0.03 if any(w in text.lower() for w in words) else 0.0


def extract_patterns(text: str) -> list[dict]:
    """对一段文本跑所有 pattern，返回候选 fact 列表。

    Args:
        text: 通常是 user message（assistant 回应里偏好 / 规则的密度
            远低于用户输入，目前不扫 assistant 文本以避免噪声）

    Returns:
        list of {content, category, confidence, source, raw_match}
    """
    if not text or not text.strip():
        return []

    out: list[dict] = []
    seen_content: set[str] = set()  # 同段文本同义命中只保留最强一条

    for p in _PATTERNS:
        for m in p.regex.finditer(text):
            try:
                # group(1) 是主体；某些 pattern 可能有多个 group（zh.before）
                if p.regex.groups >= 2 and m.group(2):
                    body = f"{m.group(1)} 之前先 {m.group(2)}"
                else:
                    body = m.group(1) if p.regex.groups else m.group(0)
            except (IndexError, AttributeError):
                continue
            body = _normalize_content(body)
            if _is_too_short(body):
                continue
            # dedup by normalized body within this batch
            key = (p.category, body.lower())
            if key in seen_content:
                continue
            seen_content.add(key)
            conf = min(0.99, p.confidence + _signal_strength_boost(text, p.lang))
            content = f"{p.prefix}: {body}"
            out.append({
                "content": content,
                "category": p.category,
                "confidence": round(conf, 2),
                "source": f"pattern:{p.id}",
                "raw_match": m.group(0)[:120],
            })

    return out


# ──────────────────────────────────────────────────────────────────────
# Skip-LLM 决策
# ──────────────────────────────────────────────────────────────────────
# Pattern 抓到的 fact 总数和置信度决定要不要再调 LLM。
# 阈值取得保守 —— 让 LLM 只在 pattern 几乎无所获时跑。


def should_skip_llm(pattern_facts: list[dict],
                    config_threshold: int = 2) -> bool:
    """Pattern 抽到 ``config_threshold`` 条 confidence ≥ 0.85 的 fact 时，
    跳过 LLM 抽取（这轮对话的高频信号已经被 pattern 捕到）。

    返回 True = 跳过 LLM 调用。
    """
    if not pattern_facts:
        return False
    high_conf = [f for f in pattern_facts if f.get("confidence", 0) >= 0.85]
    return len(high_conf) >= config_threshold


# ──────────────────────────────────────────────────────────────────────
# 预处理模型兜底接口（preview，先留 hook）
# ──────────────────────────────────────────────────────────────────────


def llm_extract_via_preprocessor(text: str,
                                  preprocessor_call,
                                  pattern_hits: list[dict],
                                  ) -> list[dict]:
    """用 agent 的 preprocessor 模型（小本地 LLM, 通常 qwen2.5:3b）
    抽取 pattern 漏掉的 fact。

    本地小模型成本 ≈ 0（vs 云 LLM），所以即使 pattern 没命中也可以
    用它兜底，比直接走主 LLM 抽取便宜 100x+。

    Args:
        text: user message
        preprocessor_call: 函数 (prompt: str) -> str，调本地小模型
        pattern_hits: 已经被 pattern 抓到的 fact，传给 prompt 让小模型
            知道哪些已经覆盖了，避免重复。

    Returns:
        list of fact dicts（同 ``extract_patterns`` 的形态，source 标
        记为 "preproc:<model>"）
    """
    if not preprocessor_call:
        return []
    already = "\n".join(f"- {f['content']}" for f in pattern_hits[:10])
    prompt = (
        "从下面的用户消息中抽取「偏好 (preference)」「规则 (rule)」类长期事实，"
        "JSON 数组返回，每条：{content, category, confidence (0-1)}。"
        "不要重复下面已抓取的；如果消息没有这类信息，返回 []。\n\n"
        f"已抓取：\n{already if already else '(无)'}\n\n"
        f"用户消息：\n{text[:1500]}\n\n"
        "JSON："
    )
    try:
        raw = preprocessor_call(prompt) or ""
    except Exception as e:
        logger.debug("preprocessor extract failed: %s", e)
        return []
    raw = raw.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:-1])
    try:
        import json as _json
        items = _json.loads(raw)
    except Exception:
        return []
    if not isinstance(items, list):
        return []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        c = str(it.get("content", "")).strip()
        cat = str(it.get("category", "")).strip().lower()
        if not c or cat not in ("preference", "rule"):
            continue
        conf = float(it.get("confidence", 0.7) or 0.7)
        out.append({
            "content": _normalize_content(c)[:_CONTENT_MAX],
            "category": cat,
            "confidence": round(min(0.95, max(0.4, conf)), 2),
            "source": "preproc:llm",
            "raw_match": "",
        })
    return out


__all__ = [
    "extract_patterns",
    "should_skip_llm",
    "llm_extract_via_preprocessor",
]
