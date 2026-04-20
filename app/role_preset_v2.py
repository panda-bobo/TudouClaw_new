"""RolePresetV2 — 角色全维度增强声明

背景：老的 ROLE_PRESETS 只有 system_prompt + 工具白名单，角色差异停留在语言风格。
V2 将角色配置扩展到 7 个维度：Knowledge / Tooling / Methodology / Quality /
LLM-Tier / Collaboration / Evolution。

关键原则：**声明式配置 + 复用现有子系统**
- YAML 声明 → RolePresetRegistry 加载
- create_agent() 时把声明投射到 AgentProfile（扩展字段）+ MCPManager + RAG + Skills
- 不新建并行引擎；SOP 复用 WorkflowTemplate，经验复用 ExperienceLibrary

加载路径：
  data/roles/*.yaml  →  RolePresetRegistry  →  ROLE_PRESETS 融合
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("tudou.role_preset_v2")


# ═══════════════════════════════════════════════════════════════════════════
# 平台场景标签（非技术用户从这里多选，不写表达式）
# ═══════════════════════════════════════════════════════════════════════════

STANDARD_SCOPE_TAGS = (
    "casual_chat",            # 闲聊 / 寒暄
    "one_on_one",             # 1v1 沟通
    "meeting",                # 多人会议
    "retro",                  # 复盘
    "task_planning",          # 任务规划
    "decision_review",        # 决策评审
    "prd_writing",            # PRD 撰写
    "tech_review",            # 技术方案评审
    "customer_conversation",  # 客户对话
    "data_analysis",          # 数据分析
)

SCOPE_TAG_LABELS_ZH = {
    "casual_chat": "闲聊",
    "one_on_one": "1v1 沟通",
    "meeting": "会议",
    "retro": "复盘",
    "task_planning": "任务规划",
    "decision_review": "决策评审",
    "prd_writing": "PRD 撰写",
    "tech_review": "技术方案评审",
    "customer_conversation": "客户对话",
    "data_analysis": "数据分析",
}


# ═══════════════════════════════════════════════════════════════════════════
# 子结构
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class QualityCheckRule:
    """质量检查规则。

    kind 枚举：
      regex           — spec={"pattern": "...", "in_field": "content"}
      contains_section — spec={"heading": "## Decisions"}
      json_schema     — spec={"schema": {...}, "in_field": "content"}
      tool_used       — spec={"tool_name": "..."} 从 ExecutionAnalysis 中判定
      contract_field  — spec={"field": "action_items", "min_items": 1}
      llm_judge       — spec={"prompt": "...", "tier": "fast_cheap"}
    """
    id: str
    description: str
    kind: str = "regex"
    spec: dict = field(default_factory=dict)
    severity: str = "hard"  # hard | soft
    feedback_template: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "QualityCheckRule":
        return cls(
            id=d.get("id", ""),
            description=d.get("description", ""),
            kind=d.get("kind", "regex"),
            spec=d.get("spec") or {},
            severity=d.get("severity", "hard"),
            feedback_template=d.get("feedback_template", ""),
        )


@dataclass
class PlaybookRule:
    """角色行为规则。

    **非技术用户可编辑**：id 用于 diff / 统计；applies_in 是场景标签多选（留空=所有场景）；
    severity=hard 会进 QualityGate 硬验收，soft 只做提示。
    """
    id: str
    statement: str
    applies_in: list[str] = field(default_factory=list)  # 从 STANDARD_SCOPE_TAGS 中勾选
    severity: str = "hard"  # hard | soft
    feedback_template: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PlaybookRule":
        return cls(
            id=d.get("id", ""),
            statement=d.get("statement", ""),
            applies_in=list(d.get("applies_in") or []),
            severity=d.get("severity", "hard"),
            feedback_template=d.get("feedback_template", ""),
        )


@dataclass
class Playbook:
    """岗位 Playbook —— 把"身份 / 专业领域 / 做事逻辑"中的【做事逻辑】结构化。

    **字段划分**（非技术维护友好）：
      - core_identity: 一句岗位本质
      - thinking_pattern: 有序思考步骤
      - must_do: 必须做的规则（按场景触发）
      - forbid: 红线（禁止做）
      - required_sections_when: 场景 → 输出必含章节
      - example_good / example_bad: 可选示例

    **不在此暴露的技术项**（由平台内置）：
      - scope_detector：场景识别规则（平台统一实现）
      - context_extractors：上下文抽取器（平台统一实现）
    """
    core_identity: str = ""
    thinking_pattern: list[str] = field(default_factory=list)
    must_do: list[PlaybookRule] = field(default_factory=list)
    forbid: list[PlaybookRule] = field(default_factory=list)
    required_sections_when: dict = field(default_factory=dict)  # scope_tag -> [section headings]
    example_good: str = ""
    example_bad: str = ""

    def to_dict(self) -> dict:
        return {
            "core_identity": self.core_identity,
            "thinking_pattern": list(self.thinking_pattern),
            "must_do": [r.to_dict() if hasattr(r, "to_dict") else r for r in self.must_do],
            "forbid": [r.to_dict() if hasattr(r, "to_dict") else r for r in self.forbid],
            "required_sections_when": dict(self.required_sections_when),
            "example_good": self.example_good,
            "example_bad": self.example_bad,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "Playbook":
        if not isinstance(d, dict):
            return cls()
        must_do = [
            PlaybookRule.from_dict(r) if isinstance(r, dict) else r
            for r in (d.get("must_do") or [])
        ]
        forbid = [
            PlaybookRule.from_dict(r) if isinstance(r, dict) else r
            for r in (d.get("forbid") or [])
        ]
        return cls(
            core_identity=d.get("core_identity", ""),
            thinking_pattern=list(d.get("thinking_pattern") or []),
            must_do=must_do,
            forbid=forbid,
            required_sections_when=dict(d.get("required_sections_when") or {}),
            example_good=d.get("example_good", ""),
            example_bad=d.get("example_bad", ""),
        )

    def is_empty(self) -> bool:
        return (not self.core_identity and not self.thinking_pattern
                and not self.must_do and not self.forbid
                and not self.required_sections_when)


@dataclass
class KPIDefinition:
    """KPI 定义。

    measurement 枚举：
      ratio        — 0-1 的比率
      count        — 计数
      duration_s   — 耗时（秒）
      bool         — True/False
    """
    key: str
    label: str
    measurement: str = "ratio"
    target: float | None = None
    extractor: str = ""  # dotted path to an extractor function (optional)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "KPIDefinition":
        return cls(
            key=d.get("key", ""),
            label=d.get("label", ""),
            measurement=d.get("measurement", "ratio"),
            target=d.get("target"),
            extractor=d.get("extractor", ""),
        )


# ═══════════════════════════════════════════════════════════════════════════
# 主结构
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class RolePresetV2:
    """7 维度角色声明。

    由 YAML 文件加载，create_agent() 时应用到 AgentProfile。
    """

    # 身份
    role_id: str
    display_name: str
    version: int = 2
    category: str = ""  # research|development|product|data|office|business
    icon: str = ""
    system_prompt: str = ""

    # 1) Knowledge
    rag_namespaces: list[str] = field(default_factory=list)
    knowledge_templates: list[str] = field(default_factory=list)   # TemplateLibrary IDs
    few_shot_skill_ids: list[str] = field(default_factory=list)    # PromptPack IDs

    # 2) Tooling
    default_mcp_bindings: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    auto_approve_tools: list[str] = field(default_factory=list)

    # 3) Methodology
    sop_template_id: str = ""
    sop_autostart: bool = True

    # 4) Quality
    quality_rules: list[QualityCheckRule] = field(default_factory=list)
    quality_hard_retries: int = 3
    quality_soft_fallback: bool = True

    # 5) LLM Tier
    llm_tier: str = ""  # "reasoning_strong" | "coding_strong" | ...
    llm_tier_overrides: dict[str, str] = field(default_factory=dict)

    # 6) Collaboration
    input_contract: dict = field(default_factory=dict)
    output_contract: dict = field(default_factory=dict)

    # 7) Evolution
    kpi_definitions: list[KPIDefinition] = field(default_factory=list)
    experience_bootstrap: list[str] = field(default_factory=list)

    # 8) Playbook（做事逻辑 —— 条件触发 + 机器验收的结构化容器）
    playbook: Playbook = field(default_factory=Playbook)

    # 遗留桥
    legacy_profile_overrides: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["quality_rules"] = [r.to_dict() if hasattr(r, "to_dict") else r
                               for r in self.quality_rules]
        d["kpi_definitions"] = [k.to_dict() if hasattr(k, "to_dict") else k
                                 for k in self.kpi_definitions]
        d["playbook"] = self.playbook.to_dict() if hasattr(self.playbook, "to_dict") else self.playbook
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "RolePresetV2":
        quality_rules = [
            QualityCheckRule.from_dict(r) if isinstance(r, dict) else r
            for r in (d.get("quality_rules") or [])
        ]
        kpi_definitions = [
            KPIDefinition.from_dict(k) if isinstance(k, dict) else k
            for k in (d.get("kpi_definitions") or [])
        ]
        return cls(
            role_id=d.get("role_id") or d.get("key", ""),  # yaml 用 key 也兼容
            display_name=d.get("display_name") or d.get("name", ""),
            version=int(d.get("version", 2)),
            category=d.get("category", ""),
            icon=d.get("icon", ""),
            system_prompt=d.get("system_prompt", ""),
            rag_namespaces=list(d.get("rag_namespaces") or []),
            knowledge_templates=list(d.get("knowledge_templates") or []),
            few_shot_skill_ids=list(d.get("few_shot_skill_ids") or []),
            default_mcp_bindings=list(d.get("default_mcp_bindings") or []),
            allowed_tools=list(d.get("allowed_tools") or []),
            denied_tools=list(d.get("denied_tools") or []),
            auto_approve_tools=list(d.get("auto_approve_tools") or []),
            sop_template_id=d.get("sop_template_id", ""),
            sop_autostart=bool(d.get("sop_autostart", True)),
            quality_rules=quality_rules,
            quality_hard_retries=int(d.get("quality_hard_retries", 3)),
            quality_soft_fallback=bool(d.get("quality_soft_fallback", True)),
            llm_tier=d.get("llm_tier", ""),
            llm_tier_overrides=dict(d.get("llm_tier_overrides") or {}),
            input_contract=dict(d.get("input_contract") or {}),
            output_contract=dict(d.get("output_contract") or {}),
            kpi_definitions=kpi_definitions,
            experience_bootstrap=list(d.get("experience_bootstrap") or []),
            playbook=Playbook.from_dict(d.get("playbook")),
            legacy_profile_overrides=dict(d.get("legacy_profile_overrides") or {}),
        )


# ═══════════════════════════════════════════════════════════════════════════
# YAML 加载
# ═══════════════════════════════════════════════════════════════════════════

def load_role_yaml(file_path: str | Path) -> RolePresetV2 | None:
    """从单个 YAML 文件加载 RolePresetV2。解析失败返回 None 并记日志。"""
    try:
        import yaml
    except ImportError:
        logger.error("PyYAML not installed — RolePresetV2 YAML loading disabled")
        return None

    path = Path(file_path)
    if not path.is_file():
        logger.warning("RolePresetV2 YAML not found: %s", path)
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        logger.warning("RolePresetV2 YAML parse failed (%s): %s", path.name, e)
        return None

    if not isinstance(data, dict):
        logger.warning("RolePresetV2 YAML not a mapping (%s)", path.name)
        return None

    try:
        preset = RolePresetV2.from_dict(data)
    except Exception as e:
        logger.warning("RolePresetV2 from_dict failed (%s): %s", path.name, e)
        return None

    # 文件名兜底补 role_id
    if not preset.role_id:
        preset.role_id = path.stem

    return preset


# ═══════════════════════════════════════════════════════════════════════════
# V1 → V2 迁移（Plan B：老角色自动迁移为 V2 骨架，V2 字段为空 → 行为完全兼容）
# ═══════════════════════════════════════════════════════════════════════════

def migrate_legacy_preset(role_id: str, legacy_dict: dict) -> RolePresetV2:
    """把老 ROLE_PRESETS 条目迁移为 V2 骨架。

    策略：
    - 保留 system_prompt 和必要字段
    - V2 特有维度（SOP、quality、tier）留空 → Pre/Post hook 空转 → 行为等同老版
    - 只设定 role_id/display_name/system_prompt/legacy_profile_overrides
    """
    name = legacy_dict.get("name") or role_id
    system_prompt = legacy_dict.get("system_prompt", "")
    profile_obj = legacy_dict.get("profile")
    legacy_overrides: dict = {}
    if profile_obj is not None:
        try:
            # profile 是 AgentProfile dataclass 或 dict
            if hasattr(profile_obj, "to_dict"):
                legacy_overrides = profile_obj.to_dict()
            elif isinstance(profile_obj, dict):
                legacy_overrides = dict(profile_obj)
        except Exception:
            legacy_overrides = {}

    return RolePresetV2(
        role_id=role_id,
        display_name=name,
        version=2,
        system_prompt=system_prompt,
        legacy_profile_overrides=legacy_overrides,
    )


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def preset_to_legacy_dict(preset: RolePresetV2) -> dict:
    """把 V2 preset 渲染成老 ROLE_PRESETS 条目（给不感知 V2 的代码用）。

    用于融合到 ROLE_PRESETS：老代码继续读 system_prompt/name/profile，不受影响。
    """
    return {
        "name": preset.display_name,
        "system_prompt": preset.system_prompt,
        # profile 字段由 registry 融合时处理（需要访问 AgentProfile 类）
        "_v2_preset": preset,   # 标记位，registry 融合时识别
    }
