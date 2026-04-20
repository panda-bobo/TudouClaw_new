"""RoleSOP — 角色工作流（Standard Operating Procedure）引擎。

设计原则：
- **不平行于 WorkflowEngine**：只做简单的 stage tracker，按 YAML 定义的顺序推进
- **由 Agent.chat() 驱动**：每次 chat 调用前 inject 当前 stage prompt，chat 后评估 exit condition
- **中等严格度**：stage 顺序推进 + exit 条件 + 允许回退
- **per-session 状态**：每个 agent + session_id 对应一个 SOP 实例，存在内存中

业务流程：
    Agent.chat() 被调用
      ↓
    Pre-hook: RoleSOPManager.get_or_start(agent_id, session, sop_id)
      ↓ inject_stage_prompt() → 当前 stage 的 goal/guidance 作为 system message
      ↓
    LLM 调用 + 工具执行
      ↓
    Post-hook: RoleSOPManager.evaluate_exit(instance, agent_output)
      ↓ exit_condition 通过 → advance_to_next()
      ↓ 不通过 → 保持当前 stage（用户下一轮继续该 stage）

Exit condition 类型：
  contains_section  — 输出含指定 markdown 标题
  regex             — 正则匹配
  any               — 默认通过（agent 自主判断）
"""
from __future__ import annotations

import logging
import re
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("tudou.role_sop")


# ═══════════════════════════════════════════════════════════════════════════
# 数据结构
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SOPStage:
    id: str
    display_name: str
    goal: str = ""
    guidance: str = ""
    exit_condition: dict = field(default_factory=dict)
    allow_rollback: bool = False
    rollback_to: str = ""
    next: str | None = None

    @classmethod
    def from_dict(cls, d: dict) -> "SOPStage":
        return cls(
            id=d.get("id", ""),
            display_name=d.get("display_name", d.get("id", "")),
            goal=d.get("goal", ""),
            guidance=d.get("guidance", ""),
            exit_condition=d.get("exit_condition") or {"type": "any"},
            allow_rollback=bool(d.get("allow_rollback", False)),
            rollback_to=d.get("rollback_to", ""),
            next=d.get("next"),
        )


@dataclass
class SOPTemplate:
    sop_id: str
    display_name: str
    version: int = 1
    stages: list[SOPStage] = field(default_factory=list)

    def stage_map(self) -> dict[str, SOPStage]:
        return {s.id: s for s in self.stages}

    def first_stage(self) -> SOPStage | None:
        return self.stages[0] if self.stages else None

    @classmethod
    def from_dict(cls, d: dict) -> "SOPTemplate":
        return cls(
            sop_id=d.get("sop_id", ""),
            display_name=d.get("display_name", ""),
            version=int(d.get("version", 1)),
            stages=[SOPStage.from_dict(s) for s in (d.get("stages") or [])],
        )


@dataclass
class SOPInstance:
    """Per-session SOP state. Lives in memory; persists via Agent state if needed."""
    instance_id: str
    agent_id: str
    session_id: str
    sop_id: str
    current_stage: str
    stage_history: list[dict] = field(default_factory=list)
    # [{stage_id, entered_at, exited_at, outcome: advanced|rolled_back|stuck}]
    completed: bool = False


# ═══════════════════════════════════════════════════════════════════════════
# Exit condition 判定
# ═══════════════════════════════════════════════════════════════════════════

def _check_exit_condition(condition: dict, output_text: str) -> bool:
    """判断 agent 输出是否满足 stage 的 exit 条件。

    条件类型：
      type: contains_section — spec.heading_patterns / min_content_length
      type: regex            — spec.pattern / in_field
      type: any              — 总是通过（agent 自主判断）
    """
    if not condition:
        return True
    ctype = condition.get("type", "any")
    spec = condition.get("spec") or {}

    if ctype == "any":
        return True

    if ctype == "contains_section":
        patterns = spec.get("heading_patterns") or []
        min_len = int(spec.get("min_content_length", 0))
        has_section = any(p and p in output_text for p in patterns)
        meets_len = len(output_text) >= min_len if min_len else True
        return has_section and meets_len

    if ctype == "regex":
        pattern = spec.get("pattern", "")
        if not pattern:
            return True
        try:
            return bool(re.search(pattern, output_text, re.DOTALL))
        except re.error:
            logger.warning("SOP exit regex invalid: %s", pattern)
            return True

    logger.debug("SOP: unknown exit condition type=%s, defaulting to True", ctype)
    return True


# ═══════════════════════════════════════════════════════════════════════════
# Registry: 加载 data/sops/*.yaml
# ═══════════════════════════════════════════════════════════════════════════

class SOPTemplateRegistry:
    def __init__(self):
        self._templates: dict[str, SOPTemplate] = {}
        self._scan_dirs: list[Path] = []
        self._lock = threading.Lock()

    def add_scan_dir(self, path: str | Path) -> None:
        p = Path(path)
        if p not in self._scan_dirs:
            self._scan_dirs.append(p)

    def load(self) -> int:
        try:
            import yaml
        except ImportError:
            logger.error("PyYAML not installed — SOP loading disabled")
            return 0

        count = 0
        with self._lock:
            for d in self._scan_dirs:
                if not d.is_dir():
                    continue
                for suffix in ("*.yaml", "*.yml", "*.json"):
                    for f in sorted(d.glob(suffix)):
                        try:
                            with open(f, "r", encoding="utf-8") as fp:
                                data = yaml.safe_load(fp) if f.suffix != ".json" else __import__("json").load(fp)
                        except Exception as e:
                            logger.warning("SOP load failed (%s): %s", f.name, e)
                            continue
                        if not isinstance(data, dict):
                            continue
                        try:
                            tpl = SOPTemplate.from_dict(data)
                        except Exception as e:
                            logger.warning("SOP from_dict failed (%s): %s", f.name, e)
                            continue
                        if not tpl.sop_id:
                            tpl.sop_id = f.stem
                        self._templates[tpl.sop_id] = tpl
                        count += 1
        logger.info("SOPTemplateRegistry loaded %d SOPs", count)
        return count

    def get(self, sop_id: str) -> SOPTemplate | None:
        return self._templates.get(sop_id)

    def all(self) -> dict[str, SOPTemplate]:
        return dict(self._templates)


# ═══════════════════════════════════════════════════════════════════════════
# Manager: 按 agent+session 维护 SOPInstance
# ═══════════════════════════════════════════════════════════════════════════

class RoleSOPManager:
    """每个 (agent_id, session_id) 对应一个 SOPInstance。"""

    def __init__(self, registry: SOPTemplateRegistry):
        self._registry = registry
        self._instances: dict[tuple[str, str], SOPInstance] = {}
        self._lock = threading.Lock()

    def _key(self, agent_id: str, session_id: str) -> tuple[str, str]:
        return (agent_id, session_id or "default")

    def get_or_start(self, agent_id: str, session_id: str,
                     sop_id: str) -> SOPInstance | None:
        """获取或新建 SOP 实例。返回 None 表示 sop_id 无效。"""
        tpl = self._registry.get(sop_id)
        if tpl is None:
            return None
        with self._lock:
            key = self._key(agent_id, session_id)
            if key in self._instances:
                return self._instances[key]
            first = tpl.first_stage()
            if first is None:
                return None
            inst = SOPInstance(
                instance_id=uuid.uuid4().hex[:12],
                agent_id=agent_id,
                session_id=session_id or "default",
                sop_id=sop_id,
                current_stage=first.id,
            )
            inst.stage_history.append({
                "stage_id": first.id,
                "entered_at": __import__("time").time(),
                "outcome": "entered",
            })
            self._instances[key] = inst
            logger.info("SOP [%s] started for agent %s: stage=%s",
                        sop_id, agent_id[:8], first.id)
            return inst

    def get(self, agent_id: str, session_id: str) -> SOPInstance | None:
        return self._instances.get(self._key(agent_id, session_id))

    def current_stage_prompt(self, inst: SOPInstance) -> str:
        """构造当前 stage 的 system message 文本。"""
        tpl = self._registry.get(inst.sop_id)
        if tpl is None:
            return ""
        stage = tpl.stage_map().get(inst.current_stage)
        if stage is None:
            return ""
        lines = [
            f"## 工作流当前阶段：{stage.display_name}（{stage.id}）",
            "",
        ]
        if stage.goal:
            lines.append(f"**阶段目标**：{stage.goal.strip()}")
            lines.append("")
        if stage.guidance:
            lines.append("**工作指引**：")
            lines.append(stage.guidance.strip())
            lines.append("")
        # Show remaining stages as roadmap
        all_stages = [s.id for s in tpl.stages]
        idx = all_stages.index(stage.id) if stage.id in all_stages else -1
        if idx >= 0 and idx < len(all_stages) - 1:
            remaining = all_stages[idx + 1:]
            lines.append(f"**后续阶段**：{' → '.join(remaining)}")
        return "\n".join(lines)

    def evaluate_exit(self, inst: SOPInstance, agent_output: str) -> str:
        """根据 agent 输出评估当前 stage 是否完成，推进或保持。

        Returns:
            "advanced"    — 已推进到下一 stage
            "completed"   — 已推进到末尾，SOP 完成
            "stuck"       — exit 条件不满足，保持当前 stage（下一轮继续）
            "not_found"   — SOP template 或 stage 未找到
        """
        tpl = self._registry.get(inst.sop_id)
        if tpl is None:
            return "not_found"
        stage_map = tpl.stage_map()
        cur = stage_map.get(inst.current_stage)
        if cur is None:
            return "not_found"

        passed = _check_exit_condition(cur.exit_condition, agent_output or "")
        if not passed:
            logger.debug("SOP [%s] agent %s stage %s: exit condition NOT met",
                         inst.sop_id, inst.agent_id[:8], cur.id)
            return "stuck"

        # 推进
        with self._lock:
            import time as _t
            # Mark current stage exited
            if inst.stage_history:
                inst.stage_history[-1]["exited_at"] = _t.time()
                inst.stage_history[-1]["outcome"] = "advanced"
            next_id = cur.next
            if not next_id:
                inst.completed = True
                logger.info("SOP [%s] agent %s: COMPLETED (final stage %s)",
                            inst.sop_id, inst.agent_id[:8], cur.id)
                return "completed"
            if next_id not in stage_map:
                logger.warning("SOP [%s] stage %s.next=%s but stage not found",
                               inst.sop_id, cur.id, next_id)
                return "not_found"
            inst.current_stage = next_id
            inst.stage_history.append({
                "stage_id": next_id,
                "entered_at": _t.time(),
                "outcome": "entered",
            })
            logger.info("SOP [%s] agent %s: advanced %s → %s",
                        inst.sop_id, inst.agent_id[:8], cur.id, next_id)
            return "advanced"

    def rollback(self, inst: SOPInstance, reason: str = "") -> bool:
        """回退到当前 stage 的 rollback_to（如允许）。"""
        tpl = self._registry.get(inst.sop_id)
        if tpl is None:
            return False
        stage_map = tpl.stage_map()
        cur = stage_map.get(inst.current_stage)
        if cur is None or not cur.allow_rollback or not cur.rollback_to:
            return False
        if cur.rollback_to not in stage_map:
            return False
        with self._lock:
            import time as _t
            if inst.stage_history:
                inst.stage_history[-1]["exited_at"] = _t.time()
                inst.stage_history[-1]["outcome"] = "rolled_back"
                inst.stage_history[-1]["rollback_reason"] = reason
            inst.current_stage = cur.rollback_to
            inst.stage_history.append({
                "stage_id": cur.rollback_to,
                "entered_at": _t.time(),
                "outcome": "entered_via_rollback",
            })
            logger.info("SOP [%s] agent %s: ROLLBACK %s → %s (%s)",
                        inst.sop_id, inst.agent_id[:8], cur.id, cur.rollback_to, reason)
            return True

    def clear_instance(self, agent_id: str, session_id: str) -> None:
        """清除 session 的 SOP 状态（如用户切换话题）。"""
        with self._lock:
            self._instances.pop(self._key(agent_id, session_id), None)


# ═══════════════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════════════

_sop_registry: SOPTemplateRegistry | None = None
_sop_manager: RoleSOPManager | None = None


def get_sop_registry() -> SOPTemplateRegistry:
    global _sop_registry
    if _sop_registry is None:
        _sop_registry = SOPTemplateRegistry()
        _sop_registry.add_scan_dir(Path.cwd() / "data" / "sops")
        import os as _os
        user_sops = Path(_os.path.expanduser("~")) / ".tudou_claw" / "sops"
        if user_sops.is_dir():
            _sop_registry.add_scan_dir(user_sops)
    return _sop_registry


def get_sop_manager() -> RoleSOPManager:
    global _sop_manager
    if _sop_manager is None:
        _sop_manager = RoleSOPManager(get_sop_registry())
    return _sop_manager


def init_sop(extra_scan_dirs: list[str] | None = None) -> tuple[SOPTemplateRegistry, RoleSOPManager]:
    reg = get_sop_registry()
    if extra_scan_dirs:
        for d in extra_scan_dirs:
            reg.add_scan_dir(d)
    reg.load()
    mgr = get_sop_manager()
    return reg, mgr
