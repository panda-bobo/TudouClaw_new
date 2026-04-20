"""Playbook Runtime — 把角色 Playbook 声明转成 LLM 可消费的上下文 + 质检规则。

两个入口：
  build_playbook_context(preset, active_scopes) -> str
      返回要注入 LLM 的 system 消息内容（已按场景筛选 must_do / forbid）。
      空 playbook 返回 ""。

  derive_quality_rules_from_playbook(preset, active_scopes) -> list[QualityCheckRule]
      从 required_sections_when 派生 QualityGate 规则，与原生 quality_rules 合并使用。

**不在此做 LLM 调用**。纯数据装配。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .role_preset_v2 import RolePresetV2, Playbook, PlaybookRule, QualityCheckRule


def _rule_applies(rule, active_scopes: list[str]) -> bool:
    """空 applies_in = 所有场景；否则 active_scopes 与 rule.applies_in 有交集即生效。"""
    applies = list(getattr(rule, "applies_in", []) or [])
    if not applies:
        return True
    return any(s in applies for s in active_scopes)


def build_playbook_context(preset, active_scopes: list[str]) -> str:
    """构建 playbook 注入内容。返回 Markdown 字符串或 ""（表示无注入）。"""
    pb = getattr(preset, "playbook", None)
    if pb is None or pb.is_empty():
        return ""

    lines: list[str] = []

    # 身份 + 思考模式（无条件）
    header_added = False
    if pb.core_identity:
        lines.append("## 岗位 Playbook")
        header_added = True
        lines.append(f"\n**角色定位**：{pb.core_identity}")

    if pb.thinking_pattern:
        if not header_added:
            lines.append("## 岗位 Playbook")
            header_added = True
        lines.append("\n**思考步骤**：")
        for i, step in enumerate(pb.thinking_pattern, 1):
            lines.append(f"{i}. {step}")

    # 场景筛选：must_do / forbid
    active_must_do = [r for r in pb.must_do if _rule_applies(r, active_scopes)]
    active_forbid = [r for r in pb.forbid if _rule_applies(r, active_scopes)]

    if active_must_do:
        if not header_added:
            lines.append("## 岗位 Playbook")
            header_added = True
        scopes_label = "、".join(active_scopes) if active_scopes else "本次对话"
        lines.append(f"\n**{scopes_label}场景下必须做到**：")
        for r in active_must_do:
            lines.append(f"- {r.statement}")

    if active_forbid:
        if not header_added:
            lines.append("## 岗位 Playbook")
            header_added = True
        lines.append("\n**以下行为禁止**：")
        for r in active_forbid:
            lines.append(f"- {r.statement}")

    # required_sections_when —— 给 LLM 的输出要求提示
    required_sections: set[str] = set()
    for scope in active_scopes:
        for s in pb.required_sections_when.get(scope, []):
            required_sections.add(s)
    if required_sections:
        if not header_added:
            lines.append("## 岗位 Playbook")
            header_added = True
        sections_str = "、".join(f"`{s}`" for s in sorted(required_sections))
        lines.append(f"\n**输出必须包含以下章节**：{sections_str}")

    return "\n".join(lines).strip()


def derive_quality_rules_from_playbook(preset, active_scopes: list[str]) -> list:
    """从 playbook 派生可进 QualityGate 的 QualityCheckRule 列表。

    当前只派生 required_sections_when → contains_section 规则。
    must_do / forbid 的遵守度靠 Pre-hook 的 prompt 注入实现，不进 Post 质检
    （避免 LLM-judge 链路慢+失败率）。
    """
    from .role_preset_v2 import QualityCheckRule

    pb = getattr(preset, "playbook", None)
    if pb is None or pb.is_empty():
        return []

    derived: list[QualityCheckRule] = []
    seen_sections: set[tuple[str, str]] = set()
    for scope in active_scopes:
        for section in pb.required_sections_when.get(scope, []):
            key = (scope, section)
            if key in seen_sections:
                continue
            seen_sections.add(key)
            rule = QualityCheckRule(
                id=f"pb_section_{scope}_{_slug(section)}",
                description=f"{scope} 场景输出必须含「{section}」章节",
                kind="contains_section",
                spec={"heading_patterns": [section]},
                severity="hard",
                feedback_template=f"请在输出中补充 `{section}` 章节。",
            )
            derived.append(rule)

    return derived


def _slug(s: str) -> str:
    """规则 id 里嵌章节名，避免非法字符。"""
    return "".join(c if c.isalnum() else "_" for c in s).strip("_")[:40]


__all__ = ["build_playbook_context", "derive_quality_rules_from_playbook"]
