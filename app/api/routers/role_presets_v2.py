"""RolePresetV2 CRUD API — 管理 7 维度角色声明。

端点：
  GET    /api/role_presets_v2              列出所有 V2 角色（摘要）
  GET    /api/role_presets_v2/{role_id}    单个角色完整配置
  POST   /api/role_presets_v2              创建角色（写入 ~/.tudou_claw/roles/{role_id}.yaml）
  PUT    /api/role_presets_v2/{role_id}    更新
  DELETE /api/role_presets_v2/{role_id}    删除（仅用户目录内的）
  POST   /api/role_presets_v2/reload       重新扫描 YAML + 融合
  GET    /api/role_presets_v2/{role_id}/kpi 查询 KPI rollup

注：内建 YAML（data/roles/*.yaml）只读；写操作落到用户目录。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import yaml
from fastapi import APIRouter, Body, Depends, HTTPException

from ..deps.auth import CurrentUser, get_current_user

logger = logging.getLogger("tudouclaw.api.role_presets_v2")

router = APIRouter(prefix="/api/role_presets_v2", tags=["role_presets_v2"])


_USER_DIR = Path(os.path.expanduser("~")) / ".tudou_claw" / "roles"


def _require_admin(user: CurrentUser) -> None:
    if not getattr(user, "is_super_admin", False):
        raise HTTPException(status_code=403, detail="Admin access required")


def _user_yaml_path(role_id: str) -> Path:
    if not role_id or "/" in role_id or "\\" in role_id or role_id.startswith("."):
        raise HTTPException(status_code=400, detail="invalid role_id")
    _USER_DIR.mkdir(parents=True, exist_ok=True)
    return _USER_DIR / f"{role_id}.yaml"


def _summary(preset) -> dict:
    return {
        "role_id": preset.role_id,
        "display_name": preset.display_name,
        "version": preset.version,
        "llm_tier": preset.llm_tier,
        "sop_template_id": preset.sop_template_id,
        "quality_rule_count": len(preset.quality_rules),
        "kpi_count": len(preset.kpi_definitions),
        "has_mcp_bindings": bool(preset.default_mcp_bindings),
        "has_rag": bool(preset.rag_namespaces),
    }


@router.get("")
async def list_presets(user: CurrentUser = Depends(get_current_user)):
    from ...role_preset_registry import get_registry
    reg = get_registry()
    return {"presets": [_summary(p) for p in reg.all().values()]}


@router.get("/{role_id}")
async def get_preset(role_id: str, user: CurrentUser = Depends(get_current_user)):
    from ...role_preset_registry import get_registry
    reg = get_registry()
    preset = reg.get(role_id)
    if preset is None:
        raise HTTPException(status_code=404, detail=f"role not found: {role_id}")
    return {"preset": preset.to_dict()}


@router.post("")
async def create_preset(
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
):
    _require_admin(user)
    role_id = str(body.get("role_id", "")).strip()
    if not role_id:
        raise HTTPException(status_code=400, detail="role_id required")

    from ...role_preset_registry import get_registry
    reg = get_registry()
    if reg.get(role_id) is not None:
        raise HTTPException(status_code=409, detail=f"role already exists: {role_id}")

    path = _user_yaml_path(role_id)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(body, f, allow_unicode=True, sort_keys=False)
    reg.load()
    preset = reg.get(role_id)
    if preset is None:
        raise HTTPException(status_code=500, detail="YAML saved but failed to load")
    return {"ok": True, "preset": preset.to_dict(), "path": str(path)}


@router.put("/{role_id}")
async def update_preset(
    role_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
):
    _require_admin(user)
    path = _user_yaml_path(role_id)
    body["role_id"] = role_id
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(body, f, allow_unicode=True, sort_keys=False)
    from ...role_preset_registry import get_registry
    reg = get_registry()
    reg.load()
    preset = reg.get(role_id)
    if preset is None:
        raise HTTPException(status_code=500, detail="YAML saved but failed to load")
    return {"ok": True, "preset": preset.to_dict(), "path": str(path)}


@router.delete("/{role_id}")
async def delete_preset(
    role_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    _require_admin(user)
    path = _user_yaml_path(role_id)
    if not path.exists():
        raise HTTPException(status_code=404, detail="no user-local YAML to delete (built-in roles are read-only)")
    path.unlink()
    from ...role_preset_registry import get_registry
    reg = get_registry()
    # Drop from in-memory registry too
    reg._presets.pop(role_id, None)  # noqa: SLF001
    reg.load()
    return {"ok": True}


@router.post("/reload")
async def reload_presets(user: CurrentUser = Depends(get_current_user)):
    _require_admin(user)
    from ...role_preset_registry import get_registry
    reg = get_registry()
    count = reg.load()
    return {"ok": True, "count": count}


@router.get("/meta/scope_tags")
async def scope_tags_catalog(user: CurrentUser = Depends(get_current_user)):
    """平台提供的场景标签目录（非技术用户勾选用）。"""
    from ...role_preset_v2 import STANDARD_SCOPE_TAGS, SCOPE_TAG_LABELS_ZH
    return {
        "tags": [
            {"tag": t, "label_zh": SCOPE_TAG_LABELS_ZH.get(t, t)}
            for t in STANDARD_SCOPE_TAGS
        ],
    }


@router.put("/{role_id}/playbook")
async def update_playbook(
    role_id: str,
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
):
    """仅更新 playbook 块（非破坏式）。

    流程：
      1. 读取当前 preset 的完整 dict 形态（含 built-in + user 叠加结果）
      2. 替换其中的 `playbook` 键为请求体
      3. 写入用户目录 YAML（作为 override）
      4. Reload 并返回新的 preset
    """
    _require_admin(user)

    from ...role_preset_registry import get_registry
    from ...role_preset_v2 import Playbook
    reg = get_registry()
    preset = reg.get(role_id)
    if preset is None:
        raise HTTPException(status_code=404, detail=f"role not found: {role_id}")

    # 校验提交的 playbook 结构合法
    try:
        _ = Playbook.from_dict(body)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid playbook: {e}")

    full = preset.to_dict()
    full["playbook"] = body

    path = _user_yaml_path(role_id)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(full, f, allow_unicode=True, sort_keys=False)

    reg.load()
    updated = reg.get(role_id)
    if updated is None:
        raise HTTPException(status_code=500, detail="YAML saved but failed to load")
    return {"ok": True, "preset": updated.to_dict(), "path": str(path)}


@router.get("/{role_id}/kpi")
async def get_kpi(
    role_id: str,
    user: CurrentUser = Depends(get_current_user),
):
    from ...kpi_recorder import get_kpi_recorder
    from ...role_preset_registry import get_registry
    reg = get_registry()
    preset = reg.get(role_id)
    if preset is None:
        raise HTTPException(status_code=404, detail=f"role not found: {role_id}")
    rec = get_kpi_recorder()
    rollups = {}
    for kpi in preset.kpi_definitions:
        name = getattr(kpi, "key", "") if hasattr(kpi, "key") else (kpi.get("key") if isinstance(kpi, dict) else "")
        if name:
            rollups[name] = rec.rollup(role_id, name)
    recent = rec.list_by_role(role_id, limit=50)
    return {"role_id": role_id, "rollups": rollups, "recent": recent}
