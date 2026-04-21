"""Skill-package tools — get_skill_guide, propose_skill, submit_skill.

All three interact with the Skill Registry / SkillForge subsystem:
reading an installed skill's SKILL.md, proposing drafts mined from the
Experience Library, or submitting a new skill package for admin review.
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from .. import sandbox as _sandbox
from ._common import _get_hub


# ── get_skill_guide ──────────────────────────────────────────────────

def _tool_get_skill_guide(**arguments) -> str:
    """Load the full SKILL.md guide + ancillary file list for a granted skill.

    Returns the complete instructions (with frontmatter stripped) so the
    agent can follow the step-by-step guide and run scripts from
    skill_dir.

    NOTE: accepts ``**kwargs`` because the registry dispatches via
    ``entry.handler(**arguments)``. Previously this was typed as
    ``arguments: dict`` which broke every call with
    "unexpected keyword argument 'name'".
    """
    name = (arguments.get("name") or "").strip()
    if not name:
        return "Error: name is required"

    try:
        reg = None
        # Try hub first (hot path in production).
        _llm_mod = sys.modules.get("app.llm")
        hub = getattr(_llm_mod, "_active_hub", None) if _llm_mod else None
        reg = getattr(hub, "skill_registry", None) if hub else None
        if reg is None:
            # Fallback: module-level singleton.
            from ..skills.engine import get_registry
            reg = get_registry()
        if reg is None:
            # Last resort: check if skill_store has a registry.
            try:
                from .. import skill_store as _ss
                store = _ss.get_store()
                if store and store._registry:
                    reg = store._registry
            except Exception:
                pass
        if reg is None:
            return "Error: skill registry not available (hub not started)"

        # Find the skill by name (fuzzy: accept name, id, or name@version).
        found = None
        for inst in reg.list_all():
            if inst.manifest.name == name or inst.id == name:
                found = inst
                break
            if name in inst.id:
                found = inst
        if found is None:
            available = [i.manifest.name for i in reg.list_all()]
            return f"Error: skill '{name}' not found. Available: {', '.join(available)}"

        # Determine effective skill_dir: prefer agent-local workspace copy.
        # The caller may pass agent_id so we can look up the agent's workspace.
        install_dir = found.install_dir
        agent_id = (arguments.get("agent_id") or "").strip()
        effective_dir = install_dir

        if agent_id:
            try:
                _llm_mod2 = sys.modules.get("app.llm")
                hub2 = getattr(_llm_mod2, "_active_hub", None) if _llm_mod2 else None
                agent_obj = hub2.get_agent(agent_id) if hub2 and hasattr(hub2, "get_agent") else None
                if agent_obj and hasattr(agent_obj, "get_skill_workspace_dir"):
                    local_dir = agent_obj.get_skill_workspace_dir(found.manifest.name)
                    if local_dir:
                        effective_dir = str(local_dir)
            except Exception:
                pass

        entry_file = found.manifest.entry or "SKILL.md"

        # Read the SKILL.md body (strip frontmatter) — prefer agent-local copy.
        md_path = Path(effective_dir) / entry_file
        if not md_path.exists():
            # Fallback to global install_dir if agent-local copy missing.
            md_path = Path(install_dir) / entry_file
        body = ""
        if md_path.exists():
            text = md_path.read_text(encoding="utf-8")
            fm = re.match(r"^---\s*\n.*?\n---\s*\n?", text, re.DOTALL)
            body = text[fm.end():] if fm else text

        # List ancillary files (scripts, references, etc.).
        files = []
        base = Path(effective_dir)
        if not base.is_dir():
            base = Path(install_dir)
        for fp in sorted(base.rglob("*")):
            if fp.is_file() and fp.name.lower() != "skill.md":
                try:
                    rel = str(fp.relative_to(base))
                except ValueError:
                    rel = fp.name
                files.append(rel)

        # Also list reference .md files whose content may be needed.
        ref_mds = []
        for fp in base.glob("*.md"):
            if fp.name.lower() != "skill.md":
                ref_mds.append(fp.name)

        result_parts = [
            f"## Skill: {found.manifest.name}",
            f"**skill_dir**: `{effective_dir}`",
            f"**runtime**: {found.manifest.runtime}",
            "",
            "运行脚本时先 cd 到 skill_dir:",
            "```bash",
            f"cd {effective_dir}",
            "```",
            "",
        ]
        if files:
            result_parts.append("**附属文件**: " + ", ".join(files))
            result_parts.append("")
        if ref_mds:
            result_parts.append("**参考文档** (需要时用 read_file 读取): "
                                + ", ".join(f"`{effective_dir}/{m}`" for m in ref_mds))
            result_parts.append("")
        result_parts.append("---")
        result_parts.append("")
        result_parts.append(body)
        return "\n".join(result_parts)

    except Exception as e:
        return f"Error loading skill guide: {e}"


# ── propose_skill ────────────────────────────────────────────────────

def _tool_propose_skill(role: str = "", topic: str = "", **ctx: Any) -> str:
    """Scan experience library and propose skill drafts via SkillForge.

    Returns a summary of generated drafts (pending admin approval).
    """
    try:
        from ..skills._skill_forge import get_skill_forge

        # Resolve role from caller agent if not specified.
        if not role:
            try:
                caller_id = ctx.get("_caller_agent_id", "") if isinstance(ctx, dict) else ""
                if caller_id:
                    hub = _get_hub()
                    agent = hub.get_agent(caller_id) if hub else None
                    if agent is not None:
                        role = (getattr(agent, "role", "") or "").strip()
            except Exception:
                pass

        forge = get_skill_forge()
        candidates = forge.scan_for_candidates(role=role or "")

        if not candidates:
            return (
                "未发现可以生成技能的经验模式。需要至少 3 个相似的高成功率经验。"
                "请继续积累经验（通过 save_experience 工具），之后再试。"
            )

        # Export packages for all candidates.
        results = []
        for draft in candidates:
            try:
                export_dir = forge.export_package(draft)
                results.append(
                    f"✓ 技能草稿: {draft.name} (ID: {draft.id})\n"
                    f"  描述: {draft.description}\n"
                    f"  置信度: {draft.confidence:.0%}\n"
                    f"  来源经验: {len(draft.source_experiences)} 条\n"
                    f"  导出目录: {export_dir}\n"
                    f"  状态: 等待管理员审批"
                )
            except Exception as e:
                results.append(f"✗ 草稿 {draft.name} 导出失败: {e}")

        summary = (
            f"已生成 {len(candidates)} 个技能草稿，等待管理员在 Portal 审批：\n\n"
            + "\n\n".join(results)
        )
        return summary

    except Exception as e:
        return f"Error proposing skill: {e}"


# ── submit_skill ─────────────────────────────────────────────────────

# Required manifest keys for a submitted skill package.
_MANIFEST_REQUIRED_KEYS = ("name", "version", "description", "runtime",
                           "author", "entry")

# Supported runtimes.
_RUNTIMES_ALLOWED = ("python", "shell", "markdown")


def _tool_submit_skill(dir_name: str, **ctx: Any) -> str:
    """Submit a skill package from the agent's workspace for admin approval.

    Reads manifest.yaml, SKILL.md, and *.py from ``{workspace}/{dir_name}``,
    validates required manifest fields, creates a SkillDraft, and saves
    it to the SkillForge review queue.
    """
    try:
        import yaml as _yaml
    except ImportError:
        return "Error: PyYAML not installed. Run pip install pyyaml."

    from ..skills._skill_forge import get_skill_forge, SkillDraft

    # Resolve workspace directory from sandbox policy (agent's working dir).
    pol = _sandbox.get_current_policy()
    workspace = str(pol.root) if getattr(pol, "root", None) else None

    # Fallback: try to find workspace from caller agent.
    if not workspace:
        try:
            caller_id = ctx.get("_caller_agent_id", "") if isinstance(ctx, dict) else ""
            if caller_id:
                hub = _get_hub()
                agent = hub.get_agent(caller_id) if hub else None
                if agent and hasattr(agent, "working_dir"):
                    workspace = str(agent.working_dir)
        except Exception:
            pass

    if not workspace:
        return "Error: Cannot determine workspace directory."

    skill_dir = os.path.join(workspace, dir_name)
    if not os.path.isdir(skill_dir):
        return f"Error: Directory not found: {skill_dir}"

    # Read manifest.yaml.
    manifest_path = os.path.join(skill_dir, "manifest.yaml")
    if not os.path.isfile(manifest_path):
        return (
            "Error: manifest.yaml not found in skill directory. "
            "Please create manifest.yaml with required fields: "
            + ", ".join(_MANIFEST_REQUIRED_KEYS)
        )

    manifest_yaml = open(manifest_path, "r", encoding="utf-8").read()
    try:
        m = _yaml.safe_load(manifest_yaml) or {}
    except Exception as e:
        return f"Error: Invalid YAML in manifest.yaml: {e}"

    # Validate required fields.
    missing = [f for f in _MANIFEST_REQUIRED_KEYS if not m.get(f)]
    if missing:
        return (
            f"Error: manifest.yaml missing required fields: {', '.join(missing)}. "
            "All of these are required: "
            + ", ".join(_MANIFEST_REQUIRED_KEYS)
        )

    rt = m.get("runtime", "")
    if rt not in _RUNTIMES_ALLOWED:
        return (f"Error: runtime must be one of {_RUNTIMES_ALLOWED}, "
                f"got '{rt}'")

    # Read SKILL.md.
    skill_md_path = os.path.join(skill_dir, "SKILL.md")
    if not os.path.isfile(skill_md_path):
        return (
            "Error: SKILL.md not found in skill directory. "
            "Please create SKILL.md documenting what the skill does and how to use it."
        )
    skill_md = open(skill_md_path, "r", encoding="utf-8").read()

    # Collect code files (*.py).
    code_files: dict[str, str] = {}
    for fn in os.listdir(skill_dir):
        fp = os.path.join(skill_dir, fn)
        if os.path.isfile(fp) and fn.endswith(".py"):
            try:
                code_files[fn] = open(fp, "r", encoding="utf-8").read()
            except Exception:
                pass

    # If runtime is python, entry file must exist.
    entry = m.get("entry", "")
    if rt == "python" and entry.endswith(".py") and entry not in code_files:
        return f"Error: Entry file '{entry}' not found in skill directory."

    # Build description string.
    desc = m.get("description", "")
    if isinstance(desc, dict):
        desc = desc.get("zh-CN") or desc.get("en") or str(desc)
    triggers = m.get("triggers", [])

    # Check for duplicate: same name + same version = reject.
    forge = get_skill_forge()
    skill_name = m["name"]
    skill_version = m.get("version", "")
    for existing in forge._drafts.values():
        if existing.name == skill_name and existing.status in ("draft", "exported", "approved"):
            existing_version = ""
            if existing.manifest_yaml:
                try:
                    em = _yaml.safe_load(existing.manifest_yaml) or {}
                    existing_version = em.get("version", "")
                except Exception:
                    pass
            if existing_version == skill_version:
                return (
                    f"Error: 技能 '{skill_name}' v{skill_version} 已存在"
                    f"（ID: {existing.id}, 状态: {existing.status}）。\n"
                    f"请修改 manifest.yaml 中的 version 字段后重新提交。"
                )

    draft_id = f"SF-{time.strftime('%Y%m%d')}-SUB-{os.urandom(3).hex()}"
    draft = SkillDraft(
        id=draft_id,
        name=m["name"],
        description=str(desc),
        source_experiences=[],
        role=ctx.get("_caller_role", "") if isinstance(ctx, dict) else "",
        scene_pattern="",
        triggers=triggers if isinstance(triggers, list) else [triggers],
        manifest_yaml=manifest_yaml,
        skill_md=skill_md,
        confidence=0.95,
        created_at=time.time(),
        status="exported",
        runtime=rt,
        code_files=code_files,
    )

    forge._drafts[draft_id] = draft
    forge._save_drafts()

    return (
        f"✓ 技能已提交审批！\n"
        f"  草稿 ID: {draft_id}\n"
        f"  名称: {m['name']}\n"
        f"  运行时: {rt}\n"
        f"  代码文件: {', '.join(code_files.keys()) or '(无)'}\n"
        f"  状态: 等待管理员在 Portal → 技能锻造 中审批\n\n"
        f"管理员审批通过后，技能将自动出现在 Skill Store 中。"
    )
