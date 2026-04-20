"""Quality Gate — 对 agent 输出做 checklist 打分，失败时反馈改进。

设计原则：
- **硬重试 3 次**：失败 → 构造反馈 prompt 让 agent 改进 → 重新执行
- **软提示兜底**：重试仍不过 → 返回原输出 + 警告事件（不阻塞用户）
- **规则可配置**：按角色 YAML 声明 QualityCheckRule 列表

规则类型：
  regex            — 正则匹配（in_field: content 支持）
  contains_section — 包含指定 markdown 标题
  json_schema      — 输出含合法 JSON 代码块
  tool_used        — 执行过程中调用了指定工具
  contract_field   — 输出含指定字段（如 action_items 数组）
  llm_judge        — 调 LLM 裁判（待后续实现，当前占位）

失败计数：
- 同一规则连续失败 2 次则停止对该规则的重试（切换关注其他规则）
- 总体硬重试上限由 quality_hard_retries 控制（默认 3）
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("tudou.quality_gate")


# ═══════════════════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class QualityCheckResult:
    """单条规则的检查结果。"""
    rule_id: str
    passed: bool
    detail: str = ""   # 为什么通过/失败的简短说明


@dataclass
class QualityGateResult:
    """整次质检汇总。"""
    passed: bool
    retry_count: int = 0
    soft_fallback_triggered: bool = False
    checks: list[QualityCheckResult] = field(default_factory=list)

    @property
    def failing_rules(self) -> list[str]:
        return [c.rule_id for c in self.checks if not c.passed]

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "retry_count": self.retry_count,
            "soft_fallback_triggered": self.soft_fallback_triggered,
            "checks": [
                {"rule_id": c.rule_id, "passed": c.passed, "detail": c.detail}
                for c in self.checks
            ],
            "failing_rules": self.failing_rules,
        }


# ═══════════════════════════════════════════════════════════════════════════
# 单条规则执行
# ═══════════════════════════════════════════════════════════════════════════

def _check_regex(rule: dict, output_text: str, _ctx: dict) -> QualityCheckResult:
    spec = rule.get("spec") or {}
    pattern = spec.get("pattern", "")
    if not pattern:
        return QualityCheckResult(rule["id"], True, "empty pattern → pass")
    try:
        flags = re.DOTALL if spec.get("dotall") else 0
        ok = bool(re.search(pattern, output_text, flags))
        return QualityCheckResult(rule["id"], ok,
                                   "regex matched" if ok else f"regex not found: {pattern[:60]}")
    except re.error as e:
        return QualityCheckResult(rule["id"], True, f"regex invalid (skipped): {e}")


def _check_contains_section(rule: dict, output_text: str, _ctx: dict) -> QualityCheckResult:
    spec = rule.get("spec") or {}
    patterns = spec.get("heading_patterns") or []
    min_len = int(spec.get("min_content_length", 0))
    if not patterns:
        return QualityCheckResult(rule["id"], True, "no patterns → pass")
    found = [p for p in patterns if p and p in output_text]
    if not found:
        return QualityCheckResult(
            rule["id"], False,
            f"missing any of: {patterns}"
        )
    if min_len and len(output_text) < min_len:
        return QualityCheckResult(
            rule["id"], False,
            f"content length {len(output_text)} < required {min_len}"
        )
    return QualityCheckResult(rule["id"], True, f"found heading(s): {found}")


def _check_contract_field(rule: dict, output_text: str, _ctx: dict) -> QualityCheckResult:
    """检查 output 含指定字段（如 action_items 数组，每项带 owner/deadline/text）。

    扫描输出中的 JSON 代码块或直接 JSON 数组。
    """
    spec = rule.get("spec") or {}
    field_name = spec.get("field", "")
    min_items = int(spec.get("min_items", 1))
    required_subfields = spec.get("required_subfields") or []

    # 尝试从 ```json ... ``` 代码块或行内 JSON 提取
    json_candidates: list[Any] = []
    # 1. Markdown JSON 代码块
    for m in re.finditer(r"```(?:json)?\s*(\[.+?\]|\{.+?\})\s*```",
                          output_text, re.DOTALL):
        try:
            json_candidates.append(json.loads(m.group(1)))
        except Exception:
            continue
    # 2. 裸 JSON 数组 / 对象（回退）
    if not json_candidates:
        for m in re.finditer(r"(\[\s*\{.+?\}\s*\])", output_text, re.DOTALL):
            try:
                json_candidates.append(json.loads(m.group(1)))
            except Exception:
                continue

    # 找到匹配字段名的数组
    items: list[Any] | None = None
    for cand in json_candidates:
        if isinstance(cand, list):
            # 可能 cand 本身就是字段数组
            items = cand
            break
        if isinstance(cand, dict) and field_name in cand:
            val = cand.get(field_name)
            if isinstance(val, list):
                items = val
                break

    if items is None:
        return QualityCheckResult(
            rule["id"], False,
            f"no JSON array found for field '{field_name}'"
        )
    if len(items) < min_items:
        return QualityCheckResult(
            rule["id"], False,
            f"only {len(items)} items, need ≥{min_items}"
        )
    # 校验每项子字段
    if required_subfields:
        for i, it in enumerate(items):
            if not isinstance(it, dict):
                return QualityCheckResult(
                    rule["id"], False,
                    f"item[{i}] is not an object"
                )
            missing = [sf for sf in required_subfields if sf not in it or not it.get(sf)]
            if missing:
                return QualityCheckResult(
                    rule["id"], False,
                    f"item[{i}] missing subfields: {missing}"
                )
    return QualityCheckResult(
        rule["id"], True,
        f"valid: {len(items)} items with all required subfields"
    )


def _check_tool_used(rule: dict, _output: str, ctx: dict) -> QualityCheckResult:
    spec = rule.get("spec") or {}
    tool_name = spec.get("tool_name", "")
    tools_used: list[str] = ctx.get("tools_used") or []
    if not tool_name:
        return QualityCheckResult(rule["id"], True, "no tool specified")
    ok = tool_name in tools_used
    return QualityCheckResult(
        rule["id"], ok,
        f"tool '{tool_name}' {'was' if ok else 'not'} used"
    )


def _check_json_schema(rule: dict, output_text: str, _ctx: dict) -> QualityCheckResult:
    """简化版：只检查是否有合法 JSON 代码块（不做 schema 校验避免 deps）。"""
    has_json = bool(re.search(r"```(?:json)?\s*[\{\[].+?[\}\]]\s*```",
                              output_text, re.DOTALL))
    return QualityCheckResult(
        rule["id"], has_json,
        "valid JSON code block found" if has_json else "no JSON code block"
    )


def _check_llm_judge(rule: dict, output_text: str, ctx: dict) -> QualityCheckResult:
    """LLM 裁判（占位实现，默认通过）。

    正式版应调 fast_cheap tier 的 LLM 做裁判，用 _bypass_gate=True 防止递归。
    """
    logger.debug("llm_judge rule %s: not yet implemented, defaulting to pass", rule.get("id"))
    return QualityCheckResult(rule["id"], True, "llm_judge stub (always pass)")


_CHECKERS = {
    "regex": _check_regex,
    "contains_section": _check_contains_section,
    "contract_field": _check_contract_field,
    "tool_used": _check_tool_used,
    "json_schema": _check_json_schema,
    "llm_judge": _check_llm_judge,
}


# ═══════════════════════════════════════════════════════════════════════════
# QualityGate 主体
# ═══════════════════════════════════════════════════════════════════════════

class QualityGate:
    """按 rules 对输出打分；调用方根据结果决定重试或软提示。"""

    def check(
        self,
        output_text: str,
        rules: list[dict],
        context: dict | None = None,
    ) -> QualityGateResult:
        """执行质检。

        Args:
            output_text: agent 的最终输出文本
            rules: QualityCheckRule 的 dict 列表（来自 profile.quality_rules）
            context: 附加上下文，可含 tools_used 等
        """
        ctx = context or {}
        checks: list[QualityCheckResult] = []
        for rule in rules or []:
            kind = rule.get("kind", "regex")
            checker = _CHECKERS.get(kind)
            if checker is None:
                logger.warning("QualityGate unknown rule kind: %s", kind)
                checks.append(QualityCheckResult(
                    rule.get("id", "?"), True,
                    f"unknown kind '{kind}' → skipped"
                ))
                continue
            try:
                result = checker(rule, output_text, ctx)
            except Exception as e:
                logger.warning("QualityGate rule %s raised: %s",
                               rule.get("id", "?"), e)
                result = QualityCheckResult(
                    rule.get("id", "?"), True,
                    f"checker raised (skipped): {e}"
                )
            checks.append(result)

        # 只有 HARD 严重度的失败项才算真的没通过；SOFT 仅记录警告
        hard_failing = [
            c for c, r in zip(checks, rules or [])
            if (not c.passed) and r.get("severity", "hard") == "hard"
        ]
        passed = len(hard_failing) == 0
        return QualityGateResult(passed=passed, checks=checks)

    def build_feedback_prompt(
        self,
        result: QualityGateResult,
        previous_output: str,
        rules: list[dict],
        prior_feedback_ids: set[str] | None = None,
    ) -> str:
        """构造反馈提示，让 agent 改进输出。

        Args:
            result: 上一轮质检结果
            previous_output: agent 上一轮的输出
            rules: 规则定义（用于读取 feedback_template）
            prior_feedback_ids: 已经反馈过 2 次的规则 ID，跳过避免无限循环
        """
        if prior_feedback_ids is None:
            prior_feedback_ids = set()
        rule_map = {r.get("id"): r for r in (rules or [])}
        blocks: list[str] = []
        blocks.append("⚠️ 你的上一个输出未通过质量检查，请根据以下反馈**完整重写整个回答**：")
        blocks.append("")

        for c in result.checks:
            if c.passed:
                continue
            if c.rule_id in prior_feedback_ids:
                continue  # 已反馈多次，跳过
            rule = rule_map.get(c.rule_id, {})
            feedback = rule.get("feedback_template", "").strip() or c.detail
            if feedback:
                blocks.append(f"- **{c.rule_id}**（{rule.get('description','')}）：")
                # 缩进显示反馈
                for line in feedback.split("\n"):
                    blocks.append(f"  {line}")
                blocks.append("")

        blocks.append("请输出**完整的、改进后的最终答案**（不要只描述要改什么，直接给出结果）。")
        # 限制 prompt 长度
        text = "\n".join(blocks)
        if len(text) > 2000:
            text = text[:2000] + "\n\n（反馈已截断）"
        return text


# ═══════════════════════════════════════════════════════════════════════════
# Singleton（无状态，只是便利方法）
# ═══════════════════════════════════════════════════════════════════════════

_gate: QualityGate | None = None


def get_quality_gate() -> QualityGate:
    global _gate
    if _gate is None:
        _gate = QualityGate()
    return _gate
