"""RolePresetV2 Registry — 启动时扫描 data/roles/*.yaml，融合到 ROLE_PRESETS。

融合策略（Plan B 自动迁移）：
1. 扫描 data/roles/*.yaml → 得到 V2 presets
2. 对每个 V2 preset：
   - 如果老 ROLE_PRESETS 已有同名 key → 用 V2 的 display_name/system_prompt 覆盖，
     同时在条目里附加 _v2_preset 标记供 create_agent() 识别
   - 如果没有 → 直接添加为新角色
3. 对老 ROLE_PRESETS 中没有 V2 YAML 的条目（coder/reviewer 等）：
   - 不动，保持老行为（V2 特有 hook 空转）

这样：
- 老角色（coder）不写 YAML → 完全不受影响
- 新角色（meeting_assistant）写了 YAML → 通过 _v2_preset 激活 7 维能力
"""
from __future__ import annotations

import logging
from pathlib import Path
from threading import Lock

from .role_preset_v2 import RolePresetV2, load_role_yaml, preset_to_legacy_dict

logger = logging.getLogger("tudou.role_preset_registry")


class RolePresetRegistry:
    """V2 角色注册表（singleton）。"""

    def __init__(self):
        self._presets: dict[str, RolePresetV2] = {}
        self._scan_dirs: list[Path] = []
        self._lock = Lock()
        self._loaded = False

    def add_scan_dir(self, path: str | Path) -> None:
        p = Path(path)
        if p not in self._scan_dirs:
            self._scan_dirs.append(p)

    def load(self) -> int:
        """扫描所有 scan dirs 加载 YAML。返回加载的 preset 数量。"""
        with self._lock:
            count = 0
            for d in self._scan_dirs:
                if not d.is_dir():
                    continue
                for f in sorted(d.glob("*.yaml")):
                    preset = load_role_yaml(f)
                    if preset is None:
                        continue
                    self._presets[preset.role_id] = preset
                    count += 1
                # 也支持 .yml 后缀
                for f in sorted(d.glob("*.yml")):
                    preset = load_role_yaml(f)
                    if preset is None:
                        continue
                    self._presets[preset.role_id] = preset
                    count += 1
            self._loaded = True
            logger.info("RolePresetRegistry loaded %d V2 presets from %d dirs",
                        count, len(self._scan_dirs))
            return count

    def get(self, role_id: str) -> RolePresetV2 | None:
        return self._presets.get(role_id)

    def all(self) -> dict[str, RolePresetV2]:
        return dict(self._presets)

    def merge_into_legacy(self, legacy_presets: dict) -> int:
        """把 V2 presets 融合到老的 ROLE_PRESETS dict。

        返回融合/新增的条目数。
        老条目会被保留并加上 _v2_preset 标记；新角色直接加为完整条目（含 profile）。
        """
        merged = 0
        # 延迟导入避免循环
        from .agent import AgentProfile  # type: ignore

        for role_id, preset in self._presets.items():
            existing = legacy_presets.get(role_id)
            if existing is not None:
                # 老角色 → 覆盖 display_name 和 system_prompt（如 V2 有），附加标记
                if preset.system_prompt:
                    existing["system_prompt"] = preset.system_prompt
                if preset.display_name:
                    existing["name"] = preset.display_name
                existing["_v2_preset"] = preset
                merged += 1
                logger.debug("RolePresetRegistry merged V2 into legacy role %s", role_id)
            else:
                # 新角色 → 完整条目
                profile_dict = preset.legacy_profile_overrides or {}
                try:
                    profile = AgentProfile.from_dict(profile_dict) if profile_dict else AgentProfile()
                except Exception as e:
                    logger.warning("Role %s: AgentProfile.from_dict failed: %s → using default", role_id, e)
                    profile = AgentProfile()
                # 把 V2 的工具白/黑名单投射到 profile（让老代码的工具检查生效）
                if preset.allowed_tools:
                    profile.allowed_tools = list(preset.allowed_tools)
                if preset.denied_tools:
                    profile.denied_tools = list(preset.denied_tools)
                if preset.auto_approve_tools:
                    profile.auto_approve_tools = list(preset.auto_approve_tools)
                legacy_presets[role_id] = {
                    "name": preset.display_name,
                    "system_prompt": preset.system_prompt,
                    "profile": profile,
                    "_v2_preset": preset,
                }
                merged += 1
                logger.info("RolePresetRegistry added new V2 role: %s", role_id)
        return merged


# ═══════════════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════════════

_registry: RolePresetRegistry | None = None


def get_registry() -> RolePresetRegistry:
    """获取全局 registry singleton，延迟初始化。"""
    global _registry
    if _registry is None:
        _registry = RolePresetRegistry()
        # 默认扫描目录（相对于 cwd / 项目根）
        _registry.add_scan_dir(Path.cwd() / "data" / "roles")
        # 也支持用户目录（和 skills 一致的约定）
        import os as _os
        user_dir = Path(_os.path.expanduser("~")) / ".tudou_claw" / "roles"
        if user_dir.is_dir():
            _registry.add_scan_dir(user_dir)
    return _registry


def init_registry(extra_scan_dirs: list[str] | None = None) -> RolePresetRegistry:
    """启动时初始化 registry 并触发 load。

    调用方（如 FastAPI lifespan）传入额外扫描目录。
    """
    reg = get_registry()
    if extra_scan_dirs:
        for d in extra_scan_dirs:
            reg.add_scan_dir(d)
    reg.load()
    return reg
