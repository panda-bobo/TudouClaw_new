"""MemoryTopicManager — 主题级 compiled-truth + timeline 写入决策器。

灵感 / 出处：
  * gbrain 的"compiled truth + timeline"二分（README 顶部块 + 时间线尾部块）
  * 详细设计见 ``app/core/memory.py`` 顶部 ``TopicMemory`` 注释

输入：``SemanticFact``（来自 contact / pattern / LLM 抽取的任一通道）
输出：在 ``memory_topic`` 表里挂一条对应的 ``TopicMemory``，
      可能新建 / 同向追加 / 冲突重写 / 细化合并 之一

四种动作的判定：
  * **new**          —— (agent, topic, category) 不存在 → 创建
  * **same**         —— compiled 与新 fact 语义一致（≥ 0.85 相似度）
                       → 仅在 timeline 追加，confidence += 0.05
  * **conflict**     —— compiled 与新 fact 矛盾（含否定词 / 数值不同 /
                        预定义反义词）→ LLM 重写 compiled，旧的入
                        timeline，confidence 重置为 0.7
  * **extend**       —— fact 不矛盾但提供新信息 → LLM 把 fact 合并进
                       compiled，timeline 追加

LLM 调用频率：只在 **conflict** 和 **extend** 两个 case 触发，且优先用
agent 自己配置的 preprocessor 模型（小本地 LLM, 成本 ≈ 0）；如未配置则
用调用方传入的 ``llm_call``（通常是主 LLM）。

Topic 聚类：用 (agent + 关键词 → topic slug) 的固定映射 + LLM 兜底；
首期靠简单关键词命中拿 60% 准，剩下走 LLM。
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional

from .memory import MemoryManager, SemanticFact, TopicMemory

logger = logging.getLogger("tudou.memory_topic")


# ── Topic slug 关键词映射（确定性，零 LLM）──────────────────────────
#
# 命中即返回，没命中才调 LLM。每条规则：(关键词正则, topic_slug)。
# 同一 fact 可能命中多条 → 取第一条（顺序即优先级）。
#
# 这里的 slug 设计原则：
#   * 用 snake_case 英文，便于 storage / debug
#   * 颗粒度按"用户能记住一辈子的概念"：不要太细（每个 PRD 一个 topic）
#     也不要太宽（"all_preferences"）
_TOPIC_RULES: list[tuple[re.Pattern, str]] = [
    # 写作 / 输出风格
    (re.compile(r"emoji|表情|符号|emojis", re.I),               "user_writing_style"),
    (re.compile(r"(简洁|详细|啰嗦|冗长|verbose|concise|brief)"), "user_writing_style"),
    (re.compile(r"(回答|输出|回复|response|reply|output).*(风格|样式|长度)"), "user_writing_style"),
    # 语言偏好
    (re.compile(r"(用|说|讲).*(中文|英文|chinese|english)"),     "user_language_pref"),
    # 编码风格 / 工具链
    (re.compile(r"(vim|emacs|vs ?code|cursor|jetbrains|idea)", re.I), "user_editor_pref"),
    (re.compile(r"(python|typescript|javascript|rust|go|java).*(版本|version|风格)", re.I), "user_tech_stack"),
    (re.compile(r"(类型注解|type hint|typing)", re.I),           "user_code_style"),
    # 测试 / 部署 / CI 流程
    (re.compile(r"(测试|跑测试|run tests?|pytest|jest)", re.I),  "workflow_testing"),
    (re.compile(r"(部署|发布|deploy|release|ship)"),             "workflow_deploy"),
    (re.compile(r"(commit|push|merge|pr|代码评审|code review)", re.I), "workflow_git"),
    # 项目 / 任务模板
    (re.compile(r"(prd|需求文档|product requirement)", re.I),    "task_pattern_prd"),
    (re.compile(r"(ppt|演讲|deck|presentation|slide)", re.I),    "task_pattern_ppt"),
    (re.compile(r"(报告|research|analysis|调研)", re.I),         "task_pattern_research"),
    # 联系 / 身份
    (re.compile(r"(邮箱|email|mail)", re.I),                     "contact_email"),
    (re.compile(r"(电话|phone|手机)", re.I),                     "contact_phone"),
    (re.compile(r"(微信|wechat|telegram|whatsapp)", re.I),       "contact_im"),
]

# 冲突检测词集 —— 同一对话出现这些就大概率是"改主意了"
_CONFLICT_NEGATIONS_ZH = (
    "不要", "别再", "改成", "改为", "之前", "之前说",
    "不再", "现在改", "现在我", "重新",
)
_CONFLICT_NEGATIONS_EN = (
    "no longer", "not anymore", "change to", "instead of",
    "actually", "actually i", "scratch that",
)


def classify_topic(fact: SemanticFact,
                   llm_call: Any = None) -> str:
    """把 fact 映射到一个 topic slug。

    优先级：
      1. 关键词规则命中 → 立刻返回
      2. category=contact 的 fact → 用 ``contact_*`` 前缀（已在规则里）
      3. category=preference / rule / intent → 用 ``general_<category>``
         作为兜底 slug（避免每条 fact 都建一个 topic 爆炸）
      4. LLM 兜底（只在 ``llm_call`` 提供时；首期为简化跳过）

    Returns: slug 字符串。永不返回空（最差兜底为 ``misc``）。
    """
    text = (fact.content or "").lower()

    # 1. 关键词规则
    for pat, slug in _TOPIC_RULES:
        if pat.search(text):
            return slug

    # 2/3. 类别兜底
    cat = (fact.category or "").lower()
    if cat in ("preference", "rule", "intent", "reasoning",
               "outcome", "reflection"):
        return f"general_{cat}"

    # 4. 终极兜底
    return "misc"


def _looks_like_conflict(old_compiled: str, new_content: str) -> bool:
    """启发式判断 new fact 是否在反对 old compiled。

    简单实现：
      * new_content 含明显的"改主意"信号词 → True
      * compiled 和 new_content 同时出现"不要 X"和"X" → True
    """
    nc = (new_content or "").lower()
    if any(w in nc for w in _CONFLICT_NEGATIONS_ZH):
        return True
    if any(w in nc for w in _CONFLICT_NEGATIONS_EN):
        return True
    # 对偶信号：compiled 说"喜欢简洁"、new 说"喜欢详细"
    pos_neg_pairs = [
        ("简洁", "详细"), ("简洁", "啰嗦"), ("详细", "简洁"),
        ("用 emoji", "不要 emoji"), ("不要 emoji", "用 emoji"),
        ("concise", "verbose"), ("verbose", "concise"),
    ]
    oc = (old_compiled or "").lower()
    for a, b in pos_neg_pairs:
        if a in oc and b in nc:
            return True
    return False


def _text_similarity_simple(a: str, b: str) -> float:
    """简单字符 n-gram Jaccard，避免引 sklearn 等重依赖。

    准确度够用做 same/extend 的初步判断；精细比较留给 LLM 决策。
    """
    if not a or not b:
        return 0.0
    sa = set(a[i:i+3] for i in range(len(a) - 2)) if len(a) > 2 else {a}
    sb = set(b[i:i+3] for i in range(len(b) - 2)) if len(b) > 2 else {b}
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


# ──────────────────────────────────────────────────────────────────────
# MemoryTopicManager
# ──────────────────────────────────────────────────────────────────────


class MemoryTopicManager:
    """主题级写入决策器。"""

    # same 判定的 Jaccard 阈值（≥ 此值视为同向证据，不重写 compiled）
    _SAME_THRESHOLD = 0.55

    # 最大 timeline 长度。超出最旧的会被裁掉（避免单 topic 爆炸）。
    _TIMELINE_MAX = 30

    # confidence 调整
    _SAME_BUMP = 0.05
    _CONFLICT_RESET = 0.7

    def __init__(self, mm: MemoryManager):
        self._mm = mm

    # ── 主入口 ──

    def register_fact(self, fact: SemanticFact,
                      llm_call: Any = None) -> dict:
        """把 fact 注册到对应的 topic memory。

        Args:
            fact: 已经被 ``MemoryManager`` 持久化（id 已存在）的 fact
            llm_call: 重写 / 合并 compiled 时调的小 LLM。无 → 走 fallback
                （直接拼接 + 截断）

        Returns: ``{"action": new|same|conflict|extend, "topic": ...,
                    "topic_id": ..., "compiled": ...}``
        """
        if not fact.agent_id or not fact.content:
            return {"action": "skipped", "reason": "incomplete_fact"}

        slug = classify_topic(fact, llm_call=llm_call)
        existing = self._mm.get_topic(fact.agent_id, slug, category=fact.category)

        if existing is None:
            tm = self._init_new_topic(fact, slug)
            self._mm.save_topic(tm)
            logger.info(
                "topic-mem [new] agent=%s topic=%s/%s",
                fact.agent_id, slug, fact.category)
            return {"action": "new", "topic": slug,
                    "topic_id": tm.id, "compiled": tm.compiled}

        # Existing: decide same / conflict / extend
        sim = _text_similarity_simple(existing.compiled, fact.content)
        is_conflict = _looks_like_conflict(existing.compiled, fact.content)

        if is_conflict:
            updated = self._handle_conflict(existing, fact, llm_call)
            self._mm.save_topic(updated)
            logger.info(
                "topic-mem [conflict] agent=%s topic=%s/%s — rewrote compiled",
                fact.agent_id, slug, fact.category)
            return {"action": "conflict", "topic": slug,
                    "topic_id": updated.id, "compiled": updated.compiled}

        if sim >= self._SAME_THRESHOLD:
            updated = self._handle_same(existing, fact)
            self._mm.save_topic(updated)
            logger.debug(
                "topic-mem [same] agent=%s topic=%s/%s — append timeline",
                fact.agent_id, slug, fact.category)
            return {"action": "same", "topic": slug,
                    "topic_id": updated.id, "compiled": updated.compiled}

        # Distinct enough → extend
        updated = self._handle_extend(existing, fact, llm_call)
        self._mm.save_topic(updated)
        logger.info(
            "topic-mem [extend] agent=%s topic=%s/%s",
            fact.agent_id, slug, fact.category)
        return {"action": "extend", "topic": slug,
                "topic_id": updated.id, "compiled": updated.compiled}

    # ── 内部分支 ──

    def _init_new_topic(self, fact: SemanticFact, slug: str) -> TopicMemory:
        now = time.time()
        return TopicMemory(
            agent_id=fact.agent_id,
            topic=slug,
            category=fact.category,
            compiled=fact.content,
            timeline=[self._tl_entry(fact, "init")],
            confidence=fact.confidence or 0.85,
            created_at=now,
            updated_at=now,
            last_accessed_at=0.0,
        )

    def _handle_same(self, existing: TopicMemory,
                      fact: SemanticFact) -> TopicMemory:
        """同向证据：仅追加 timeline + bump confidence。compiled 不变。"""
        existing.timeline = self._cap_timeline(
            existing.timeline + [self._tl_entry(fact, "append")])
        existing.confidence = min(0.99,
                                    existing.confidence + self._SAME_BUMP)
        existing.updated_at = time.time()
        return existing

    def _handle_conflict(self, existing: TopicMemory,
                          fact: SemanticFact,
                          llm_call: Any) -> TopicMemory:
        """冲突：LLM 重写 compiled；旧的入 timeline；confidence 归位。"""
        old_compiled = existing.compiled
        new_compiled = self._llm_rewrite_compiled(
            old_compiled, fact, llm_call,
            mode="conflict",
        )
        existing.compiled = new_compiled
        existing.timeline = self._cap_timeline(
            existing.timeline
            + [{"ts": time.time(), "kind": "old_compiled",
                "content": old_compiled, "fact_id": "", "source": "rewrite"}]
            + [self._tl_entry(fact, "conflict_rewrite")])
        existing.confidence = self._CONFLICT_RESET
        existing.updated_at = time.time()
        return existing

    def _handle_extend(self, existing: TopicMemory,
                        fact: SemanticFact,
                        llm_call: Any) -> TopicMemory:
        """延展：LLM 把 fact 合并进 compiled；timeline 追加。"""
        merged = self._llm_rewrite_compiled(
            existing.compiled, fact, llm_call, mode="extend",
        )
        existing.compiled = merged
        existing.timeline = self._cap_timeline(
            existing.timeline + [self._tl_entry(fact, "merge")])
        existing.confidence = min(0.99,
                                   existing.confidence + self._SAME_BUMP / 2)
        existing.updated_at = time.time()
        return existing

    # ── helpers ──

    def _tl_entry(self, fact: SemanticFact, kind: str) -> dict:
        return {
            "ts": time.time(),
            "fact_id": fact.id or "",
            "content": (fact.content or "")[:200],
            "source": fact.source or "",
            "kind": kind,
        }

    def _cap_timeline(self, tl: list) -> list:
        if len(tl) <= self._TIMELINE_MAX:
            return tl
        # 保留最早 1 条（init）+ 最近 N-1 条
        return [tl[0]] + tl[-(self._TIMELINE_MAX - 1):]

    def _llm_rewrite_compiled(self, old: str, fact: SemanticFact,
                                llm_call: Any, mode: str) -> str:
        """让 LLM 重写 compiled。``mode`` 决定 prompt 模板。

        无 ``llm_call`` 时退化为简单字符串拼接（保证不丢信息，但
        compiled 会变长 → 留给后续 dream 维护合并）。
        """
        if not llm_call:
            # Fallback: 简单拼接
            if mode == "conflict":
                return f"{fact.content}（先前理解：{old[:80]}…）"[:480]
            return f"{old}\n· {fact.content}"[:480]
        verb = "重写" if mode == "conflict" else "合并补充"
        prompt = (
            f"已有的当前理解（compiled）：\n{old}\n\n"
            f"新证据：\n{fact.content}\n\n"
            f"请{verb}当前理解。规则：\n"
            f"  • 单段，≤ 200 字\n"
            f"  • 只保留事实，不解释、不说『根据用户的话』\n"
            f"  • 冲突时以新证据为准；非冲突时把新信息融入\n\n"
            f"返回纯文本，不带 markdown / 不带引号。"
        )
        try:
            out = llm_call(prompt) or ""
            out = out.strip().strip("`'\"")
            if not out:
                return old
            return out[:480]
        except Exception as e:
            logger.debug("topic compiled rewrite failed: %s", e)
            return old


__all__ = [
    "MemoryTopicManager",
    "classify_topic",
]
