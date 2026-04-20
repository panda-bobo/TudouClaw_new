"""
SkillForge — 自动化技能生成引擎
Automated Skill Generation Engine from Experience Library

监听经验库，发现重复模式，自动提议新技能。

架构：
    SkillForge (全局, per-role 可选)
        ├── 经验扫描 (Scanning) — 按场景/成功率/频率分组
        ├── 候选过滤 (Filtering) — 相似度 + 最小经验数 + 成功率检验
        ├── LLM 合成 (Synthesis) — 将经验组合成技能 SKILL.md
        ├── 清单生成 (Manifest Generation) — manifest.yaml + 元数据
        ├── 导出包装 (Packaging) — 目录结构 ready for import
        └── 人工审查 (Review Queue) — pending_skills/ 等待确认

输出目录结构：
    pending_skills/
      {skill_name}/
        manifest.yaml          # 技能清单
        SKILL.md              # 完整技能指导文档
        _meta.json            # 来源经验 ID、创建时间、置信度等

使用示例：
    forge = SkillForge(
        experience_data_dir="data/experience",
        output_dir="pending_skills",
        llm_call_fn=llm.chat_no_stream
    )

    # 扫描并生成候选
    candidates = forge.scan_for_candidates(role="coder")
    for draft in candidates:
        print(f"提议技能: {draft.name} (置信度 {draft.confidence:.1%})")

    # 审查、批准或拒绝
    forge.approve_draft(draft.id)
    skill_dir = forge.export_package(draft)
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Callable

try:
    import yaml
except ImportError:
    yaml = None

logger = logging.getLogger("tudou.skill_forge")

# ─────────────────────────────────────────────────────────────
# 配置常量
# ─────────────────────────────────────────────────────────────

from ..defaults import SKILLFORGE_MODEL as _SF_MODEL

MIN_EXPERIENCES_FOR_SKILL = 3          # 至少 3 个相似经验
MIN_SUCCESS_RATE = 0.75                # 最小成功率 75%
SCENE_SIMILARITY_THRESHOLD = 0.5       # Jaccard 相似度阈值
DEFAULT_LLM_MODEL = _SF_MODEL
DEFAULT_LLM_PROVIDER = "claude"

# ─────────────────────────────────────────────────────────────
# 数据模型
# ─────────────────────────────────────────────────────────────

@dataclass
class SkillDraft:
    """技能草稿 — 等待人工审查"""
    id: str = ""                           # 自动生成，如 SF-20250412-001
    name: str = ""                         # 技能名称，kebab-case
    description: str = ""                  # 一句话描述
    source_experiences: list[str] = field(default_factory=list)  # 源经验 ID 列表
    role: str = ""                         # 目标角色
    scene_pattern: str = ""                # 通用场景模式描述
    triggers: list[str] = field(default_factory=list)  # 触发关键词
    manifest_yaml: str = ""                # 生成的 manifest.yaml 内容
    skill_md: str = ""                     # 生成的 SKILL.md 内容
    confidence: float = 0.8                # 置信度 0-1
    created_at: float = field(default_factory=time.time)
    status: str = "draft"                  # "draft" | "exported" | "approved" | "rejected"
    runtime: str = "markdown"              # "markdown" | "python"
    code_files: dict[str, str] = field(default_factory=dict)  # filename → content (e.g. {"main.py": "..."})

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> SkillDraft:
        # Filter known fields to tolerate schema evolution
        import inspect
        valid = {f.name for f in __import__("dataclasses").fields(SkillDraft)}
        filtered = {k: v for k, v in d.items() if k in valid}
        return SkillDraft(**filtered)


@dataclass
class Experience:
    """经验数据模型（与 experience_library.py 保持一致）"""
    id: str = ""
    role: str = ""
    scene: str = ""
    core_knowledge: str = ""
    tags: list[str] = field(default_factory=list)
    success_count: int = 0
    fail_count: int = 0
    created_at: float = field(default_factory=time.time)
    last_used: float = 0.0

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        return self.success_count / total if total > 0 else 0.0

    @property
    def total_uses(self) -> int:
        return self.success_count + self.fail_count


# ─────────────────────────────────────────────────────────────
# 辅助函数
# ─────────────────────────────────────────────────────────────

def _jaccard_similarity(set_a: set, set_b: set) -> float:
    """Jaccard 相似度。"""
    if not set_a and not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union) if union else 0.0


def _tokenize_text(text: str) -> set[str]:
    """Simple tokenizer for similarity comparison."""
    tokens = set()
    for word in re.findall(r"[a-zA-Z0-9_]+", text.lower()):
        tokens.add(word)
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff':
            tokens.add(ch)
    return tokens


class SkillForge:
    """技能锻造引擎 — 监听经验库、自动提议新技能。"""

    def __init__(
        self,
        experience_data_dir: str,
        output_dir: str,
        llm_call_fn: Optional[Callable] = None,
        model: str = DEFAULT_LLM_MODEL,
        provider: str = DEFAULT_LLM_PROVIDER,
    ):
        """初始化 SkillForge。

        Args:
            experience_data_dir: 经验库数据目录路径（data/experience/）
            output_dir: 输出目录（pending_skills/）
            llm_call_fn: LLM 调用函数，签名: (messages: list[dict]) -> dict
            model: LLM 模型名
            provider: LLM 供应商名
        """
        self.experience_dir = Path(experience_data_dir)
        self.output_dir = Path(output_dir)
        self.llm_call_fn = llm_call_fn
        self.model = model
        self.provider = provider
        self._lock = threading.Lock()

        # 确保输出目录存在
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # 草稿内存缓存（持久化到 _drafts.json）
        self._drafts: dict[str, SkillDraft] = {}
        self._load_drafts()

        logger.info(
            f"SkillForge initialized: exp_dir={self.experience_dir}, "
            f"output_dir={self.output_dir}"
        )

    # ─────────────────────────────────────────────────────────
    # 公开 API
    # ─────────────────────────────────────────────────────────

    def scan_for_candidates(self, role: str = "") -> list[SkillDraft]:
        """扫描经验库，生成技能候选。

        Args:
            role: 限制扫描的角色（空字符串 = 全部角色）

        Returns:
            新生成的 SkillDraft 列表
        """
        logger.info(f"Scanning for skill candidates (role={role or 'all'})")

        # 加载经验
        experiences = self._load_experiences(role)
        logger.debug(f"Loaded {len(experiences)} experiences")

        if len(experiences) < MIN_EXPERIENCES_FOR_SKILL:
            logger.warning(
                f"Not enough experiences ({len(experiences)} < {MIN_EXPERIENCES_FOR_SKILL})"
            )
            return []

        # 分组相似经验
        groups = self._group_similar_experiences(experiences)
        logger.debug(f"Grouped into {len(groups)} groups")

        # 过滤 + 生成草稿
        candidates: list[SkillDraft] = []
        for group in groups:
            if not self._evaluate_group(group):
                continue
            try:
                draft = self.draft_skill(group)
                with self._lock:
                    self._drafts[draft.id] = draft
                candidates.append(draft)
            except Exception as e:
                logger.error(f"Failed to draft skill from group: {e}")

        # 保存草稿
        if candidates:
            with self._lock:
                self._save_drafts()

        logger.info(f"Generated {len(candidates)} skill candidates")
        return candidates

    def draft_skill(self, experiences: list[Experience]) -> SkillDraft:
        """从经验组合成技能草稿。

        Args:
            experiences: 至少 3 个相似经验

        Returns:
            SkillDraft 对象
        """
        if len(experiences) < MIN_EXPERIENCES_FOR_SKILL:
            raise ValueError(
                f"Need at least {MIN_EXPERIENCES_FOR_SKILL} experiences, "
                f"got {len(experiences)}"
            )

        # 基础信息
        role = experiences[0].role or "general"
        exp_ids = [exp.id for exp in experiences]

        # 提取通用场景、知识点、触发词
        scenes = [exp.scene for exp in experiences if exp.scene]
        knowledge = [exp.core_knowledge for exp in experiences if exp.core_knowledge]
        all_tags: list[str] = []
        for exp in experiences:
            all_tags.extend(exp.tags)

        # 计算平均成功率作为置信度
        avg_success_rate = sum(exp.success_rate for exp in experiences) / len(experiences)

        # 生成技能名称（snake_case）
        skill_name = self._generate_skill_name(role, experiences)

        # 生成描述
        description = self._generate_description(scenes, knowledge)

        # 提取触发关键词（从标签、场景、知识中）
        triggers = self._extract_triggers(scenes, knowledge, all_tags)

        # 生成 SKILL.md 内容
        skill_md = self._generate_skill_md(skill_name, experiences, scenes, knowledge)

        # 判断是否需要 Python 实现 + 生成代码文件
        code_files, runtime, depends_on, inputs = self._generate_code_files(
            skill_name, experiences, scenes, knowledge, all_tags
        )

        # 生成 manifest.yaml 内容
        manifest_yaml = self._generate_manifest(
            skill_name, description, triggers, role,
            runtime=runtime, depends_on=depends_on, inputs=inputs,
        )

        # 创建草稿
        draft = SkillDraft(
            id=self._generate_draft_id(),
            name=skill_name,
            description=description,
            source_experiences=exp_ids,
            role=role,
            scene_pattern=" / ".join(scenes[:3]) if scenes else "general",
            triggers=triggers,
            manifest_yaml=manifest_yaml,
            skill_md=skill_md,
            confidence=min(0.99, max(0.5, avg_success_rate)),
            status="draft",
            runtime=runtime,
            code_files=code_files,
        )

        logger.info(
            f"Drafted skill: {draft.name} (confidence={draft.confidence:.1%}, "
            f"sources={len(exp_ids)})"
        )

        return draft

    def export_package(self, draft: SkillDraft) -> str:
        """导出技能包为目录结构。

        创建结构：
            pending_skills/{skill_name}/
                manifest.yaml
                SKILL.md
                main.py          # (python runtime only)
                *.py             # (additional code files)
                _meta.json

        Args:
            draft: SkillDraft 对象

        Returns:
            导出目录路径
        """
        skill_dir = self.output_dir / draft.name
        skill_dir.mkdir(parents=True, exist_ok=True)

        # Validate manifest YAML before writing; inject source: agent
        manifest_to_write = draft.manifest_yaml
        if yaml is not None and draft.manifest_yaml:
            try:
                parsed = yaml.safe_load(draft.manifest_yaml)
                if not isinstance(parsed, dict):
                    raise ValueError(f"manifest must be a YAML dict, got {type(parsed).__name__}")
                missing = [f for f in ("name", "version", "runtime") if f not in parsed]
                if missing:
                    raise ValueError(f"manifest missing required fields: {missing}")
                # Tag source as "agent" for SkillForge-exported skills
                if "source" not in parsed:
                    parsed["source"] = "agent"
                    manifest_to_write = yaml.dump(parsed, allow_unicode=True, default_flow_style=False)
            except yaml.YAMLError as ye:
                raise ValueError(f"Invalid manifest YAML: {ye}")

        # 写 manifest.yaml
        manifest_path = skill_dir / "manifest.yaml"
        manifest_path.write_text(manifest_to_write, encoding="utf-8")
        logger.debug(f"Wrote manifest to {manifest_path}")

        # 写 SKILL.md
        skill_md_path = skill_dir / "SKILL.md"
        skill_md_path.write_text(draft.skill_md, encoding="utf-8")
        logger.debug(f"Wrote SKILL.md to {skill_md_path}")

        # Validate code files (warn but don't block for agent-submitted drafts)
        if draft.code_files:
            ok, err = self._validate_generated_code(draft.code_files)
            if not ok:
                logger.warning(f"Code validation issue in draft {draft.id}: {err} — writing files anyway")

        for filename, content in (draft.code_files or {}).items():
            safe_name = Path(filename).name
            if not safe_name.endswith(".py"):
                logger.warning(f"Skipping non-Python code file: {safe_name}")
                continue
            code_path = skill_dir / safe_name
            code_path.write_text(content, encoding="utf-8")
            logger.debug(f"Wrote code file: {code_path}")

        # 写 _meta.json
        meta = {
            "draft_id": draft.id,
            "skill_name": draft.name,
            "source_experiences": draft.source_experiences,
            "confidence": draft.confidence,
            "role": draft.role,
            "runtime": draft.runtime,
            "code_files": list((draft.code_files or {}).keys()),
            "created_at": draft.created_at,
            "created_at_iso": datetime.fromtimestamp(draft.created_at).isoformat(),
            "status": draft.status,
        }
        meta_path = skill_dir / "_meta.json"
        meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.debug(f"Wrote metadata to {meta_path}")

        # 更新草稿状态（仅当草稿还处于 draft 阶段时才设为 exported，
        # 已 approved 的不回退状态）
        if draft.status not in ("approved", "rejected"):
            draft.status = "exported"
        with self._lock:
            self._drafts[draft.id] = draft
            self._save_drafts()

        logger.info(f"Exported skill package to {skill_dir}")
        return str(skill_dir)

    def list_drafts(self) -> list[SkillDraft]:
        """列出所有草稿。"""
        return list(self._drafts.values())

    def approve_draft(self, draft_id: str) -> dict:
        """批准草稿并自动导出技能包。"""
        if draft_id not in self._drafts:
            return {"error": f"Draft not found: {draft_id}"}

        draft = self._drafts[draft_id]
        draft.status = "approved"
        with self._lock:
            self._save_drafts()

        # Auto-export the approved skill package
        try:
            export_dir = self.export_package(draft)
        except Exception as e:
            logger.error(f"Export failed for approved draft {draft_id}: {e}")
            return {"draft_id": draft_id, "status": "approved", "export_error": str(e)}

        logger.info(f"Approved and exported draft: {draft_id} ({draft.name})")
        return {"draft_id": draft_id, "status": "approved", "export_dir": export_dir}

    def reject_draft(self, draft_id: str) -> dict:
        """拒绝草稿。"""
        if draft_id not in self._drafts:
            return {"error": f"Draft not found: {draft_id}"}

        draft = self._drafts[draft_id]
        draft.status = "rejected"
        with self._lock:
            self._save_drafts()

        logger.info(f"Rejected draft: {draft_id} ({draft.name})")
        return {"draft_id": draft_id, "status": "rejected"}

    # ─────────────────────────────────────────────────────────
    # 内部方法
    # ─────────────────────────────────────────────────────────

    def _load_experiences(self, role: str = "") -> list[Experience]:
        """从文件加载经验。

        扫描 experience_dir/{role}/ 目录下所有 JSON 文件。
        """
        experiences: list[Experience] = []

        if not self.experience_dir.exists():
            logger.warning(f"Experience directory does not exist: {self.experience_dir}")
            return experiences

        # If role specified, scan only that sub-dir; otherwise scan all
        if role:
            scan_dirs = [self.experience_dir / role]
        else:
            scan_dirs = [d for d in self.experience_dir.iterdir() if d.is_dir()]
            # Also scan root-level files
            scan_dirs.append(self.experience_dir)

        for scan_dir in scan_dirs:
            if not scan_dir.exists():
                continue
            for fpath in scan_dir.glob("*.json"):
                if fpath.name.startswith("_"):
                    continue
                try:
                    data = json.loads(fpath.read_text(encoding="utf-8"))
                    if isinstance(data, list):
                        for item in data:
                            exp = self._parse_experience(item, role or scan_dir.name)
                            if exp:
                                experiences.append(exp)
                    elif isinstance(data, dict):
                        # Could be a single experience or a collection with entries
                        entries = data.get("entries", data.get("experiences", []))
                        if entries and isinstance(entries, list):
                            for item in entries:
                                exp = self._parse_experience(item, role or scan_dir.name)
                                if exp:
                                    experiences.append(exp)
                        else:
                            exp = self._parse_experience(data, role or scan_dir.name)
                            if exp:
                                experiences.append(exp)
                except Exception as e:
                    logger.debug(f"Skipping {fpath}: {e}")

        return experiences

    def _parse_experience(self, data: dict, role: str) -> Optional[Experience]:
        """Parse a dict into an Experience object."""
        if not isinstance(data, dict):
            return None

        exp_id = data.get("id", data.get("exp_id", ""))
        if not exp_id:
            return None

        return Experience(
            id=exp_id,
            role=data.get("role", role),
            scene=data.get("scene", data.get("scenario", "")),
            core_knowledge=data.get("core_knowledge", data.get("knowledge", "")),
            tags=data.get("tags", []),
            success_count=data.get("success_count", data.get("successes", 0)),
            fail_count=data.get("fail_count", data.get("failures", 0)),
            created_at=data.get("created_at", 0.0),
            last_used=data.get("last_used", 0.0),
        )

    def _group_similar_experiences(self, experiences: list[Experience]) -> list[list[Experience]]:
        """按场景相似度分组经验。"""
        if not experiences:
            return []

        # Token sets per experience
        token_sets = []
        for exp in experiences:
            text = f"{exp.scene} {exp.core_knowledge} {' '.join(exp.tags)}"
            token_sets.append(_tokenize_text(text))

        # Simple greedy grouping by Jaccard similarity
        assigned = [False] * len(experiences)
        groups: list[list[Experience]] = []

        for i in range(len(experiences)):
            if assigned[i]:
                continue
            group = [experiences[i]]
            assigned[i] = True

            for j in range(i + 1, len(experiences)):
                if assigned[j]:
                    continue
                sim = _jaccard_similarity(token_sets[i], token_sets[j])
                if sim >= SCENE_SIMILARITY_THRESHOLD:
                    group.append(experiences[j])
                    assigned[j] = True

            groups.append(group)

        return groups

    def _evaluate_group(self, group: list[Experience]) -> bool:
        """Evaluate whether a group qualifies for skill generation."""
        if len(group) < MIN_EXPERIENCES_FOR_SKILL:
            return False

        # Check average success rate
        avg_success = sum(exp.success_rate for exp in group) / len(group)
        if avg_success < MIN_SUCCESS_RATE:
            logger.debug(
                f"Group rejected: avg success rate {avg_success:.1%} < {MIN_SUCCESS_RATE:.1%}"
            )
            return False

        return True

    def _generate_skill_name(self, role: str, experiences: list[Experience]) -> str:
        """Generate a skill name from role and experience content."""
        # Collect common tags
        from collections import Counter
        tag_counter: Counter[str] = Counter()
        for exp in experiences:
            tag_counter.update(exp.tags)

        # Use most common tag as base
        if tag_counter:
            most_common_tag = tag_counter.most_common(1)[0][0]
            base = most_common_tag
        else:
            # Fallback: use first few words of scene
            scene_words = []
            for exp in experiences:
                if exp.scene:
                    scene_words.extend(re.findall(r"[a-zA-Z0-9]+", exp.scene.lower())[:3])
                    break
            base = "_".join(scene_words) if scene_words else "skill"

        # Clean and format
        name = re.sub(r"[^a-zA-Z0-9_]", "_", base.lower())
        name = re.sub(r"_+", "_", name).strip("_")

        # Add role prefix
        if role and role != "general":
            name = f"{role}_{name}"

        # Ensure uniqueness against existing drafts and directories
        candidate = name[:50]
        existing_dirs = (
            {d.name for d in self.output_dir.iterdir() if d.is_dir()}
            if self.output_dir.exists() else set()
        )
        existing_draft_names = {d.name for d in self._drafts.values()}
        if candidate in existing_dirs | existing_draft_names:
            candidate = f"{candidate[:43]}_{uuid.uuid4().hex[:6]}"

        return candidate

    def _generate_description(self, scenes: list[str], knowledge: list[str]) -> str:
        """Generate a one-line description from scenes and knowledge."""
        if scenes:
            # Use first scene as basis, truncate
            desc = scenes[0][:100]
            if len(scenes) > 1:
                desc += f" (and {len(scenes) - 1} related scenarios)"
            return desc
        if knowledge:
            return knowledge[0][:120]
        return "Auto-generated skill from experience patterns"

    def _extract_triggers(self, scenes: list[str], knowledge: list[str], tags: list[str]) -> list[str]:
        """Extract trigger keywords from scenes, knowledge, and tags."""
        from collections import Counter
        word_counter: Counter[str] = Counter()

        # Words from scenes
        for scene in scenes:
            for word in re.findall(r"[a-zA-Z0-9_]+", scene.lower()):
                if len(word) > 2:
                    word_counter[word] += 1

        # Words from knowledge
        for k in knowledge:
            for word in re.findall(r"[a-zA-Z0-9_]+", k.lower()):
                if len(word) > 2:
                    word_counter[word] += 1

        # Tags directly
        for tag in tags:
            clean_tag = tag.strip().lower()
            if clean_tag:
                word_counter[clean_tag] += 2  # Boost tags

        # Filter out stop words
        stop_words = {"the", "and", "for", "with", "this", "that", "from", "are", "was",
                       "has", "have", "but", "not", "can", "will", "should", "would"}
        triggers = [w for w, _ in word_counter.most_common(20) if w not in stop_words]

        return triggers[:10]

    def _generate_skill_md(
        self,
        skill_name: str,
        experiences: list[Experience],
        scenes: list[str],
        knowledge: list[str],
    ) -> str:
        """Generate SKILL.md content."""
        if self.llm_call_fn:
            return self._generate_skill_md_with_llm(skill_name, experiences, scenes, knowledge)
        return self._generate_skill_md_template(skill_name, experiences, scenes, knowledge)

    def _generate_skill_md_with_llm(
        self,
        skill_name: str,
        experiences: list[Experience],
        scenes: list[str],
        knowledge: list[str],
    ) -> str:
        """Use LLM to generate a high-quality SKILL.md."""
        scenes_text = "\n".join(f"- {s}" for s in scenes[:5])
        knowledge_text = "\n".join(f"- {k}" for k in knowledge[:5])
        exp_summary = "\n".join(
            f"- [{e.id}] role={e.role}, scene={e.scene[:60]}, "
            f"success_rate={e.success_rate:.0%}"
            for e in experiences[:5]
        )

        prompt = f"""Generate a SKILL.md file for skill "{skill_name}".

Context:
- Scenes covered: {scenes_text}
- Core knowledge: {knowledge_text}
- Source experiences: {exp_summary}

The SKILL.md should include:
1. YAML frontmatter with name, description, category, and tags
2. Clear instructions for an AI agent
3. Step-by-step workflow
4. Important considerations and edge cases
5. Quality criteria

MANDATORY sandbox policy (must be obeyed by every skill that produces files):
- Any file that will be reported back as a deliverable / attachment / result
  MUST live under ${{AGENT_WORKSPACE}}. Paths outside that directory
  (e.g. ~/.agent-browser/tmp/, /tmp/, ~/Downloads/) are rejected by the
  TudouClaw deliverable endpoint with "403 path outside deliverable_dir".
- If the underlying tool writes to a fixed external path (common with
  CLIs like `npx agent-browser screenshot|pdf`, Playwright, screencapture),
  you MUST:
    1. Prefer an explicit output-path flag that writes directly into
       ${{AGENT_WORKSPACE}}/<subdir>/ (e.g. screenshots/, pdfs/, downloads/).
    2. If no such flag exists, immediately `cp` (never symlink) the file
       from the external path into ${{AGENT_WORKSPACE}}/<subdir>/ before
       reporting any path to the user.
- Include a Workflow step that calls this out explicitly, and reference
  the builtin skill `safe-artifact-paths` for the full rationale.

Output the complete SKILL.md content (markdown with YAML frontmatter)."""

        try:
            messages = [{"role": "user", "content": prompt}]
            response = self.llm_call_fn(messages)
            if isinstance(response, dict):
                content = response.get("content", response.get("text", ""))
            elif isinstance(response, str):
                content = response
            else:
                content = str(response)
            if content.strip():
                return content.strip()
        except Exception as e:
            logger.warning(f"LLM skill generation failed, falling back to template: {e}")

        return self._generate_skill_md_template(skill_name, experiences, scenes, knowledge)

    def _generate_skill_md_template(
        self,
        skill_name: str,
        experiences: list[Experience],
        scenes: list[str],
        knowledge: list[str],
    ) -> str:
        """Generate a template-based SKILL.md (no LLM needed)."""
        tags = set()
        for exp in experiences:
            tags.update(exp.tags)
        tags_str = ", ".join(sorted(tags)[:10])

        # Description
        desc = self._generate_description(scenes, knowledge)

        # Build SKILL.md
        lines = [
            "---",
            f"name: {skill_name}",
            f"description: {desc}",
            "category: workflow",
            f"tags: {tags_str}",
            "---",
            "",
            f"# {skill_name}",
            "",
            f"> {desc}",
            "",
            "## Applicable Scenarios",
            "",
        ]

        for i, scene in enumerate(scenes[:5], 1):
            lines.append(f"{i}. {scene}")

        lines.extend([
            "",
            "## Core Knowledge",
            "",
        ])

        for i, k in enumerate(knowledge[:5], 1):
            lines.append(f"{i}. {k}")

        lines.extend([
            "",
            "## Workflow",
            "",
            "1. Identify the scenario and gather context",
            "2. Apply the relevant knowledge from the guidelines above",
            "3. Verify the output meets quality criteria",
            "4. Document any deviations or new learnings",
            "",
            "## Quality Criteria",
            "",
            "- Solution addresses the identified scenario",
            "- Core knowledge principles are correctly applied",
            "- Output is validated and tested where applicable",
            "",
            "## Source Experiences",
            "",
        ])

        for exp in experiences[:10]:
            rate_str = f"{exp.success_rate:.0%}" if exp.total_uses > 0 else "N/A"
            lines.append(
                f"- `{exp.id}` (role: {exp.role}, success: {rate_str}, "
                f"uses: {exp.total_uses})"
            )

        lines.append("")
        return "\n".join(lines)

    # ─────────────────────────────────────────────────────────
    # Python 代码生成
    # ─────────────────────────────────────────────────────────

    # 经验中包含这些关键词时倾向生成 Python 技能而非纯 Markdown 指南
    _CODE_HINT_KEYWORDS: set[str] = {
        "pptx", "docx", "xlsx", "pdf", "csv", "json",
        "api", "http", "mcp", "email", "screenshot",
        "文件", "生成", "创建", "导出", "转换", "下载",
        "file", "generate", "create", "export", "convert",
    }

    def _needs_python_runtime(
        self,
        experiences: list[Experience],
        scenes: list[str],
        knowledge: list[str],
        tags: list[str],
    ) -> bool:
        """Heuristic: does this experience cluster imply executable code?"""
        combined = " ".join(scenes + knowledge + tags).lower()
        hits = sum(1 for kw in self._CODE_HINT_KEYWORDS if kw in combined)
        return hits >= 2

    def _generate_code_files(
        self,
        skill_name: str,
        experiences: list[Experience],
        scenes: list[str],
        knowledge: list[str],
        tags: list[str],
    ) -> tuple[dict[str, str], str, list[dict], list[dict]]:
        """Decide runtime and optionally generate Python code files.

        Returns:
            (code_files, runtime, depends_on, inputs)
            - code_files: {"main.py": "...", ...} or empty dict
            - runtime: "python" | "markdown"
            - depends_on: [{"id": ..., "tools": [...], "optional": bool}, ...]
            - inputs: [{"name": ..., "type": ..., "required": bool, "description": ...}]
        """
        if not self._needs_python_runtime(experiences, scenes, knowledge, tags):
            return {}, "markdown", [], []

        # Try LLM generation first; fall back to template
        if self.llm_call_fn:
            result = self._generate_code_with_llm(skill_name, experiences, scenes, knowledge)
            if result:
                return result

        return self._generate_code_template(skill_name, experiences, scenes, knowledge)

    def _generate_code_with_llm(
        self,
        skill_name: str,
        experiences: list[Experience],
        scenes: list[str],
        knowledge: list[str],
    ) -> tuple[dict[str, str], str, list[dict], list[dict]] | None:
        """Use LLM to generate main.py and dependency declarations.

        Returns (code_files, runtime, depends_on, inputs) or None on failure.
        """
        scenes_text = "\n".join(f"- {s}" for s in scenes[:5])
        knowledge_text = "\n".join(f"- {k}" for k in knowledge[:5])

        prompt = f"""Generate a Python skill implementation for "{skill_name}".

This skill will run inside a TudouClaw sandbox with a SkillContext (ctx).
Available ctx APIs:
  - ctx.mcp(mcp_id).tool_name(**kwargs)  — call an MCP tool
  - ctx.llm(prompt, system="...")         — call LLM
  - ctx.log(msg)                          — log a message
  - ctx.env(key)                          — read allowed env var

Context from accumulated experiences:
- Scenes: {scenes_text}
- Knowledge: {knowledge_text}

Requirements:
1. Write a main.py with a `run(ctx, **kwargs)` function
2. Only import from safe standard library (json, re, math, datetime, os.path, etc.)
3. Do NOT import requests, urllib.request, subprocess, shutil, or any IO module
4. All external IO must go through ctx.mcp() or ctx.llm()
5. If MCP tools are needed, list them in depends_on

Respond in JSON format:
{{
  "main_py": "... full Python source code ...",
  "depends_on_mcp": [
    {{"id": "mcp_server_name", "tools": ["tool1", "tool2"], "optional": false}}
  ],
  "inputs": [
    {{"name": "param_name", "type": "string", "required": true, "description": "..."}}
  ]
}}

If this skill is purely guidance-based and does NOT need Python code,
return: {{"skip": true}}"""

        try:
            code_files, depends_on, inputs = self._call_llm_for_code(prompt)
            if code_files is None:
                return None

            # Validate generated code against sandbox rules
            ok, err = self._validate_generated_code(code_files)
            if not ok:
                logger.warning("LLM-generated code failed validation: %s — retrying", err)
                retry_prompt = (
                    f"{prompt}\n\nIMPORTANT: Your previous attempt was rejected "
                    f"by the sandbox validator:\n{err}\n"
                    f"Fix the code and respond again in the same JSON format."
                )
                code_files, depends_on, inputs = self._call_llm_for_code(retry_prompt)
                if code_files is None:
                    return None
                ok2, err2 = self._validate_generated_code(code_files)
                if not ok2:
                    logger.warning("LLM retry also failed validation: %s — falling back to template", err2)
                    return None

            return code_files, "python", depends_on, inputs

        except Exception as e:
            logger.warning(f"LLM code generation failed: {e}")
            return None

    def _call_llm_for_code(
        self, prompt: str
    ) -> tuple[dict[str, str] | None, list[dict], list[dict]]:
        """Send prompt to LLM and parse the JSON response.

        Returns (code_files_or_None, depends_on, inputs).
        """
        messages = [{"role": "user", "content": prompt}]
        response = self.llm_call_fn(messages)
        raw = response.get("content", "") if isinstance(response, dict) else str(response)

        json_match = re.search(r'\{[\s\S]*\}', raw)
        if not json_match:
            return None, [], []
        data = json.loads(json_match.group())

        if data.get("skip"):
            return None, [], []

        main_py = data.get("main_py", "")
        if not main_py or "def run" not in main_py:
            return None, [], []

        code_files = {"main.py": main_py}
        depends_on = data.get("depends_on_mcp", [])
        inputs = data.get("inputs", [])
        return code_files, depends_on, inputs

    def _generate_code_template(
        self,
        skill_name: str,
        experiences: list[Experience],
        scenes: list[str],
        knowledge: list[str],
    ) -> tuple[dict[str, str], str, list[dict], list[dict]]:
        """Generate a minimal template main.py when LLM is unavailable."""
        knowledge_lines = "\n".join(f"#   - {k[:80]}" for k in knowledge[:5])
        scenes_lines = "\n".join(f"#   - {s[:80]}" for s in scenes[:5])

        main_py = f'''"""
{skill_name} — auto-generated skill from experience library.

Scenes:
{scenes_lines}

Knowledge:
{knowledge_lines}
"""


def run(ctx, **kwargs):
    """Main entry point. Customize this implementation.

    Available ctx APIs:
      ctx.mcp(mcp_id).tool_name(**kw)  — call MCP tool
      ctx.llm(prompt, system="...")     — call LLM
      ctx.log(msg)                      — log message
    """
    ctx.log("Running {skill_name}")

    # TODO: Replace with actual implementation.
    # Use ctx.mcp() for file generation, ctx.llm() for content generation.
    result = ctx.llm(
        f"Based on this knowledge, help the user: {{kwargs}}",
        system="You are a helpful assistant specialized in {skill_name}."
    )
    return {{"output": result}}
'''
        return {"main.py": main_py}, "python", [], []

    # ─────────────────────────────────────────────────────────
    # Code validation (sandbox safety)
    # ─────────────────────────────────────────────────────────

    @staticmethod
    def _validate_generated_code(code_files: dict[str, str]) -> tuple[bool, str]:
        """Validate generated Python files against sandbox rules.

        Uses the same AST checker that runs at skill invocation time
        (engine.validate_python_skill). Returns (ok, error_message).
        """
        if not code_files:
            return True, ""
        try:
            from .engine import validate_python_skill, CodeValidationError
        except ImportError:
            logger.debug("engine.validate_python_skill not available; skipping validation")
            return True, ""

        for filename, content in code_files.items():
            if not filename.endswith(".py"):
                continue
            tmp = None
            try:
                tmp = tempfile.NamedTemporaryFile(
                    suffix=".py", mode="w", delete=False, encoding="utf-8"
                )
                tmp.write(content)
                tmp.flush()
                tmp.close()
                validate_python_skill(tmp.name)
            except CodeValidationError as e:
                return False, f"{filename}: {e}"
            except Exception as e:
                return False, f"{filename}: unexpected validation error: {e}"
            finally:
                if tmp and os.path.exists(tmp.name):
                    os.unlink(tmp.name)
        return True, ""

    # ─────────────────────────────────────────────────────────
    # Manifest 生成
    # ─────────────────────────────────────────────────────────

    def _generate_manifest(
        self,
        skill_name: str,
        description: str,
        triggers: list[str],
        role: str,
        runtime: str = "markdown",
        depends_on: list[dict] | None = None,
        inputs: list[dict] | None = None,
    ) -> str:
        """Generate manifest.yaml content."""
        triggers_yaml = "\n".join(f"  - {t}" for t in triggers[:8])

        entry_line = 'entry: main.py' if runtime == "python" else 'entry: SKILL.md'

        # Escape description for YAML safety
        safe_desc = description.replace('"', '\\"')

        manifest = f"""name: {skill_name}
description: "{safe_desc}"
version: "1.0.0"
runtime: {runtime}
{entry_line}
category: workflow
origin: skill_forge

triggers:
{triggers_yaml}

roles:
  - {role}
"""

        # Add depends_on for python skills
        if runtime == "python" and depends_on:
            manifest += "\ndepends_on:\n  mcp:\n"
            for dep in depends_on:
                mcp_id = dep.get("id", "")
                tools = dep.get("tools", [])
                optional = dep.get("optional", False)
                manifest += f"    - id: {mcp_id}\n"
                if tools:
                    manifest += "      tools:\n"
                    for tool in tools:
                        manifest += f"        - {tool}\n"
                manifest += f"      optional: {'true' if optional else 'false'}\n"

        # Add inputs for python skills
        if runtime == "python" and inputs:
            manifest += "\ninputs:\n"
            for inp in inputs:
                manifest += f"  - name: {inp.get('name', '')}\n"
                manifest += f"    type: {inp.get('type', 'string')}\n"
                manifest += f"    required: {'true' if inp.get('required') else 'false'}\n"
                if inp.get("description"):
                    manifest += f"    description: {inp['description']}\n"

        return manifest

    def _generate_draft_id(self) -> str:
        """Generate a unique draft ID: SF-YYYYMMDD-XXX."""
        date_str = datetime.now().strftime("%Y%m%d")
        suffix = uuid.uuid4().hex[:6]
        return f"SF-{date_str}-{suffix}"

    def _load_drafts(self) -> None:
        """Load drafts from _drafts.json."""
        drafts_file = self.output_dir / "_drafts.json"
        if drafts_file.exists():
            try:
                data = json.loads(drafts_file.read_text(encoding="utf-8"))
                for d in data:
                    draft = SkillDraft.from_dict(d)
                    if draft.id:
                        self._drafts[draft.id] = draft
                logger.debug(f"Loaded {len(self._drafts)} drafts from {drafts_file}")
            except Exception as e:
                logger.warning(f"Failed to load drafts: {e}")

    def _save_drafts(self) -> None:
        """Persist drafts to _drafts.json."""
        drafts_file = self.output_dir / "_drafts.json"
        try:
            data = [d.to_dict() for d in self._drafts.values()]
            drafts_file.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            logger.debug(f"Saved {len(self._drafts)} drafts to {drafts_file}")
        except Exception as e:
            logger.warning(f"Failed to save drafts: {e}")


# ─────────────────────────────────────────────────────────────
# Global singleton
# ─────────────────────────────────────────────────────────────

_global_forge: SkillForge | None = None
_forge_lock = __import__("threading").Lock()


def get_skill_forge(data_dir: str = "") -> SkillForge:
    """Get or create the global SkillForge singleton.

    Args:
        data_dir: TudouClaw data root (e.g. ~/.tudou_claw).
                  If empty, uses TUDOU_CLAW_DATA_DIR env or DEFAULT_DATA_DIR.
    """
    global _global_forge
    if _global_forge is not None:
        return _global_forge
    with _forge_lock:
        if _global_forge is not None:
            return _global_forge
        if not data_dir:
            data_dir = os.environ.get("TUDOU_CLAW_DATA_DIR", "")
        if not data_dir:
            from .. import DEFAULT_DATA_DIR
            data_dir = DEFAULT_DATA_DIR
        experience_dir = os.path.join(data_dir, "experience")
        output_dir = os.path.join(data_dir, "pending_skills")
        _global_forge = SkillForge(
            experience_data_dir=experience_dir,
            output_dir=output_dir,
        )
        logger.info("Global SkillForge initialized: %s", output_dir)
        return _global_forge
