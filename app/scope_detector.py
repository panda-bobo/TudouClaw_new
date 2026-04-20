"""Scope Detector — 平台级场景识别。

从当前用户消息（可选：对话历史 / 参与者数）判断属于哪类场景，
返回一组场景标签（来自 STANDARD_SCOPE_TAGS）。

**非技术用户不接触此模块** —— 场景识别规则在这里集中维护，
Playbook YAML 中的 `applies_in` 只引用标签，不写表达式。

设计原则：
- 纯正则+关键词，零 LLM 调用（每次聊天前必跑，不能慢）
- 规则优先级清晰（casual_chat 命中时直接返回，不叠加）
- 允许多标签共存（例：会议 + 决策评审）
- 有兜底（任何消息至少返回一个标签）
"""
from __future__ import annotations

import re
from typing import Iterable

from .role_preset_v2 import STANDARD_SCOPE_TAGS


# 预编译正则 —— 每次聊天前都要跑，编译一次后复用
_RE_MEETING_MARKERS = re.compile(
    r"(主持人[：:]|发言人[：:]|参会人|会议纪要|agenda)",
    re.IGNORECASE,
)
_RE_SPEAKER_MENTIONS = re.compile(r"@[\w\u4e00-\u9fff]+[：:]")
_RE_RETRO = re.compile(r"(复盘|回顾|retro|retrospective|反思总结)", re.IGNORECASE)
_RE_DECISION = re.compile(r"(决策|评审|选方案|决议|是否.{0,6}(通过|采纳)|定下来)")
_RE_TECH_REVIEW = re.compile(
    r"(技术方案|架构|API设计|系统设计|选型|技术评审|RFC|接口契约|可扩展性)",
    re.IGNORECASE,
)
_RE_PRD = re.compile(
    r"(PRD|需求文档|产品需求|用户故事|user stor|验收标准|acceptance criteria)",
    re.IGNORECASE,
)
_RE_CUSTOMER = re.compile(
    r"(客户|合同|报价|条款|甲方|乙方|成单|deal|商务谈判)",
    re.IGNORECASE,
)
_RE_DATA_ANALYSIS = re.compile(
    r"(数据异常|指标|下跌|上升|转化率|留存|活跃|DAU|MAU|LTV|CAC|GMV|ARR|"
    r"根因分析|漏斗)",
    re.IGNORECASE,
)
_RE_TASK_PLANNING = re.compile(
    r"(里程碑|阻塞|排期|action\s*item|backlog|sprint|任务分配|工期)",
    re.IGNORECASE,
)
_RE_QUESTION_MARK = re.compile(r"[?？]")


def _count_speakers_in_text(text: str) -> int:
    """粗略统计文本里的发言人数（基于 "名字：" 模式）。"""
    matches = re.findall(r"(?m)^([\w\u4e00-\u9fff·\-\.]{1,20})[：:]", text)
    return len(set(matches)) if matches else 0


def detect_scopes(
    message: str,
    history: Iterable[dict] | None = None,
    participants: int | None = None,
) -> list[str]:
    """识别当前对话场景，返回 scope tag 列表（STANDARD_SCOPE_TAGS 子集）。

    参数：
      message      — 当前用户消息
      history      — 对话历史（可选，当前未使用，预留）
      participants — 参会人数（可选，外部 session 可提供）

    返回至少 1 个标签（兜底 one_on_one）。
    """
    if not message:
        return ["one_on_one"]

    msg = message.strip()

    tags: list[str] = []

    # 1) 会议识别（多说话人格式 / 3+ 参与者 / 纪要关键词）
    is_meeting = False
    if _RE_MEETING_MARKERS.search(msg):
        is_meeting = True
    elif _count_speakers_in_text(msg) >= 3:
        is_meeting = True
    elif len(_RE_SPEAKER_MENTIONS.findall(msg)) >= 2:
        is_meeting = True
    elif participants is not None and participants >= 3:
        is_meeting = True
    if is_meeting:
        tags.append("meeting")

    # 3) 复盘
    if _RE_RETRO.search(msg):
        tags.append("retro")

    # 4) 决策评审
    if _RE_DECISION.search(msg):
        tags.append("decision_review")

    # 5) 技术方案评审
    if _RE_TECH_REVIEW.search(msg):
        tags.append("tech_review")

    # 6) PRD 撰写
    if _RE_PRD.search(msg):
        tags.append("prd_writing")

    # 7) 客户对话
    if _RE_CUSTOMER.search(msg):
        tags.append("customer_conversation")

    # 8) 数据分析
    if _RE_DATA_ANALYSIS.search(msg):
        tags.append("data_analysis")

    # 9) 任务规划
    if _RE_TASK_PLANNING.search(msg):
        tags.append("task_planning")

    # 合法性过滤（防止未来手误加非标准 tag）
    tags = [t for t in tags if t in STANDARD_SCOPE_TAGS]

    if tags:
        return tags

    # 10) 兜底：无任何岗位相关标签命中
    #     → 短消息且无问号视为闲聊；否则视为 1v1 严肃沟通
    if len(msg) < 30 and not _RE_QUESTION_MARK.search(msg):
        return ["casual_chat"]
    return ["one_on_one"]


__all__ = ["detect_scopes"]
