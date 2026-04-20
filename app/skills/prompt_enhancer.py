"""
Prompt Enhancer — 提示词增强 (原 Skill System) + 发现匹配 + 注入标记 (P1)

参考 OpenSpace 的 Skill Engine 设计:
- 技能定义为 SKILL.md 文件 (YAML frontmatter + Markdown 指令)
- BM25 关键词匹配 + 任务相关性评估
- 技能注入到 Agent system prompt
- 执行后标记技能是否被有效使用

闭环: 技能目录扫描 → BM25 匹配任务 → 注入 prompt → 执行 → 分析判定 → 更新技能统计
"""
from __future__ import annotations
import math
import os
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..infra.logging import get_logger

logger = get_logger("tudou.prompt_enhancer")


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class PromptPack:
    """A registered skill from a SKILL.md file."""
    skill_id: str = ""
    name: str = ""
    description: str = ""
    category: str = "general"     # tool_guide, workflow, reference, general
    tags: list[str] = field(default_factory=list)
    path: str = ""                # filesystem path to SKILL.md
    content: str = ""             # full SKILL.md content (cached)
    is_active: bool = True
    # Quality metrics
    total_selections: int = 0     # times selected for injection
    total_applied: int = 0        # times actually used by agent
    total_completions: int = 0    # tasks completed when this skill was active
    total_fallbacks: int = 0      # times agent ignored this skill
    # Lineage
    version: int = 1
    parent_id: str = ""           # previous version's skill_id
    origin: str = "imported"      # imported, captured, fixed, derived
    # Timestamps
    first_seen: float = field(default_factory=time.time)
    last_updated: float = field(default_factory=time.time)
    last_selected: float = 0.0

    @property
    def effectiveness(self) -> float:
        """0-100%, how often this skill is actually used when selected."""
        if self.total_selections == 0:
            return 0.0
        return round(self.total_applied / self.total_selections * 100, 1)

    @property
    def completion_rate(self) -> float:
        """0-100%, task completion rate when this skill was active."""
        if self.total_applied == 0:
            return 0.0
        return round(self.total_completions / self.total_applied * 100, 1)

    def to_dict(self) -> dict:
        return {
            "skill_id": self.skill_id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "tags": self.tags,
            "path": self.path,
            "is_active": self.is_active,
            "total_selections": self.total_selections,
            "total_applied": self.total_applied,
            "total_completions": self.total_completions,
            "total_fallbacks": self.total_fallbacks,
            "effectiveness": self.effectiveness,
            "completion_rate": self.completion_rate,
            "version": self.version,
            "parent_id": self.parent_id,
            "origin": self.origin,
            "first_seen": self.first_seen,
            "last_updated": self.last_updated,
            "last_selected": self.last_selected,
        }

    @staticmethod
    def from_dict(d: dict) -> PromptPack:
        return PromptPack(
            skill_id=d.get("skill_id", ""),
            name=d.get("name", ""),
            description=d.get("description", ""),
            category=d.get("category", "general"),
            tags=d.get("tags", []),
            path=d.get("path", ""),
            is_active=d.get("is_active", True),
            total_selections=d.get("total_selections", 0),
            total_applied=d.get("total_applied", 0),
            total_completions=d.get("total_completions", 0),
            total_fallbacks=d.get("total_fallbacks", 0),
            version=d.get("version", 1),
            parent_id=d.get("parent_id", ""),
            origin=d.get("origin", "imported"),
            first_seen=d.get("first_seen", time.time()),
            last_updated=d.get("last_updated", time.time()),
            last_selected=d.get("last_selected", 0.0),
        )


# ---------------------------------------------------------------------------
# SKILL.md Parser
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_YAML_KV_RE = re.compile(r"^(\w+)\s*:\s*(.+)$", re.MULTILINE)
_YAML_LIST_RE = re.compile(r"^(\w+)\s*:\s*\n((?:\s*-\s*.+\n?)+)", re.MULTILINE)


def parse_skill_md(path: str) -> PromptPack | None:
    """
    Parse a SKILL.md file into a PromptPack.

    Format:
        ---
        name: My Skill Name
        description: What this skill does
        category: workflow
        tags: python, automation, data
        ---

        # Instructions
        ...markdown body...
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        logger.warning("parse_skill_md: cannot read %s: %s", path, e)
        return None

    if not content.strip():
        return None

    record = PromptPack(path=path, content=content)

    # Parse YAML frontmatter
    fm_match = _FRONTMATTER_RE.match(content)
    if fm_match:
        fm_text = fm_match.group(1)

        # Parse key-value pairs
        for m in _YAML_KV_RE.finditer(fm_text):
            key, val = m.group(1).strip(), m.group(2).strip()
            if key == "name":
                record.name = val
            elif key == "description":
                record.description = val
            elif key == "category":
                record.category = val
            elif key == "tags":
                record.tags = [t.strip() for t in val.split(",") if t.strip()]

        # Parse list-style tags
        for m in _YAML_LIST_RE.finditer(fm_text):
            key = m.group(1).strip()
            items_text = m.group(2)
            items = [line.strip().lstrip("- ").strip()
                     for line in items_text.split("\n") if line.strip()]
            if key == "tags" and items:
                record.tags = items
    else:
        # No frontmatter — try to extract name from first heading
        heading_match = re.match(r"^#\s+(.+)$", content, re.MULTILINE)
        if heading_match:
            record.name = heading_match.group(1).strip()

    # Fallback name from filename
    if not record.name:
        record.name = Path(path).parent.name or Path(path).stem

    # Read or create .skill_id sidecar
    skill_id_path = os.path.join(os.path.dirname(path), ".skill_id")
    if os.path.exists(skill_id_path):
        try:
            record.skill_id = open(skill_id_path).read().strip()
        except Exception:
            pass

    if not record.skill_id:
        safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", record.name.lower())[:30]
        record.skill_id = f"{safe_name}__imp_{uuid.uuid4().hex[:8]}"
        try:
            os.makedirs(os.path.dirname(skill_id_path), exist_ok=True)
            with open(skill_id_path, "w") as f:
                f.write(record.skill_id)
        except Exception:
            pass

    return record


# ---------------------------------------------------------------------------
# BM25 Ranker
# ---------------------------------------------------------------------------

_BM25_STOPWORDS = frozenset({
    # URL/web noise — these appear in almost every skill doc and user message
    "http", "https", "www", "com", "org", "net", "cn", "io", "html", "htm",
    "url", "api", "json", "xml", "css", "js",
    # Common filler
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "to", "of",
    "and", "or", "in", "on", "at", "for", "with", "from", "by", "it", "this",
    "that", "as", "not", "but", "if", "do", "does", "did", "will", "can",
    # Chinese filler
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "个", "上", "也", "很", "到", "说", "要", "去", "你", "会", "着",
    "把", "那", "得", "过", "下", "么", "能", "好", "出", "来",
})


def _tokenize(text: str) -> list[str]:
    """CJK-aware tokenizer: ASCII words + CJK bigrams for better precision."""
    tokens = []
    # Split ASCII words (skip stopwords and single chars)
    for word in re.findall(r"[a-zA-Z0-9_]+", text.lower()):
        if word not in _BM25_STOPWORDS and len(word) > 1:
            tokens.append(word)
    # Extract CJK characters, then generate bigrams for better matching.
    # Single CJK chars are too ambiguous (e.g. "内" matches everything with "内容").
    # Bigrams like "内容", "制作", "报告" are much more precise.
    cjk_chars = [ch for ch in text if '\u4e00' <= ch <= '\u9fff' and ch not in _BM25_STOPWORDS]
    if len(cjk_chars) >= 2:
        for i in range(len(cjk_chars) - 1):
            bigram = cjk_chars[i] + cjk_chars[i + 1]
            tokens.append(bigram)
    elif len(cjk_chars) == 1:
        tokens.append(cjk_chars[0])
    return tokens


class BM25Ranker:
    """
    Simple BM25 implementation for skill matching.
    Operates on skill name + description + content.
    """

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._corpus: list[tuple[str, list[str]]] = []  # [(skill_id, tokens), ...]
        self._doc_freqs: Counter = Counter()
        self._avg_dl: float = 0.0
        self._n_docs: int = 0

    def index(self, skills: list[PromptPack]):
        """Build index from skill records."""
        self._corpus = []
        self._doc_freqs = Counter()

        for skill in skills:
            text = f"{skill.name} {skill.description} {' '.join(skill.tags)} {skill.content[:3000]}"
            tokens = _tokenize(text)
            self._corpus.append((skill.skill_id, tokens))
            # Count unique terms per doc
            unique_terms = set(tokens)
            for term in unique_terms:
                self._doc_freqs[term] += 1

        self._n_docs = len(self._corpus)
        total_len = sum(len(tokens) for _, tokens in self._corpus)
        self._avg_dl = total_len / self._n_docs if self._n_docs > 0 else 1.0

    def query(self, query_text: str, top_k: int = 5) -> list[tuple[str, float]]:
        """
        Return top-k (skill_id, score) pairs ranked by BM25 relevance.
        """
        if not self._corpus:
            return []

        q_tokens = _tokenize(query_text)
        if not q_tokens:
            return []

        scores: dict[str, float] = {}
        N = self._n_docs

        for skill_id, doc_tokens in self._corpus:
            score = 0.0
            dl = len(doc_tokens)
            tf_counter = Counter(doc_tokens)

            for qt in q_tokens:
                tf = tf_counter.get(qt, 0)
                if tf == 0:
                    continue
                df = self._doc_freqs.get(qt, 0)
                if df == 0:
                    continue

                # IDF
                idf = math.log((N - df + 0.5) / (df + 0.5) + 1.0)
                # TF normalization
                tf_norm = (tf * (self.k1 + 1)) / (tf + self.k1 * (1 - self.b + self.b * dl / self._avg_dl))
                score += idf * tf_norm

            if score > 0:
                scores[skill_id] = score

        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return ranked[:top_k]


# ---------------------------------------------------------------------------
# Skill Store (persistence)
# ---------------------------------------------------------------------------

class PromptPackStore:
    """
    Manages skill records: scanning directories, persistence, CRUD.
    """

    def __init__(self, persist_path: str = ""):
        self._skills: dict[str, PromptPack] = {}  # skill_id → record
        self._persist_path = persist_path
        self._scan_dirs: list[str] = []

    @property
    def skills(self) -> dict[str, PromptPack]:
        return self._skills

    def add_scan_dir(self, path: str):
        """Add a directory to scan for SKILL.md files."""
        if path and path not in self._scan_dirs:
            self._scan_dirs.append(path)

    def scan(self) -> int:
        """
        Scan all registered directories for SKILL.md files.
        Returns number of new skills discovered.
        """
        new_count = 0
        for scan_dir in self._scan_dirs:
            if not os.path.isdir(scan_dir):
                continue
            # Walk directory tree looking for SKILL.md files
            for root, dirs, files in os.walk(scan_dir):
                for fname in files:
                    if fname.upper() == "SKILL.MD":
                        fpath = os.path.join(root, fname)
                        record = parse_skill_md(fpath)
                        if record and record.skill_id:
                            if record.skill_id not in self._skills:
                                self._skills[record.skill_id] = record
                                new_count += 1
                            else:
                                # Update content if file changed
                                existing = self._skills[record.skill_id]
                                if record.content != existing.content:
                                    existing.content = record.content
                                    existing.name = record.name
                                    existing.description = record.description
                                    existing.tags = record.tags
                                    existing.last_updated = time.time()

        if new_count > 0:
            logger.info("PromptPackStore: scanned %d dirs, discovered %d new skills (total: %d)",
                        len(self._scan_dirs), new_count, len(self._skills))
        return new_count

    def get(self, skill_id: str) -> PromptPack | None:
        return self._skills.get(skill_id)

    def get_active(self) -> list[PromptPack]:
        return [s for s in self._skills.values() if s.is_active]

    def add_skill(self, record: PromptPack) -> str:
        """Add or update a skill record. Returns skill_id."""
        if not record.skill_id:
            safe_name = re.sub(r"[^a-zA-Z0-9_]", "_", record.name.lower())[:30]
            record.skill_id = f"{safe_name}__imp_{uuid.uuid4().hex[:8]}"
        self._skills[record.skill_id] = record
        return record.skill_id

    def remove_skill(self, skill_id: str) -> bool:
        if skill_id in self._skills:
            self._skills[skill_id].is_active = False
            return True
        return False

    def get_stats(self) -> dict:
        active = self.get_active()
        return {
            "total": len(self._skills),
            "active": len(active),
            "categories": Counter(s.category for s in active),
            "scan_dirs": self._scan_dirs,
        }

    def to_dict(self) -> dict:
        return {
            "skills": {k: v.to_dict() for k, v in self._skills.items()},
            "scan_dirs": self._scan_dirs,
        }

    @staticmethod
    def from_dict(d: dict) -> PromptPackStore:
        store = PromptPackStore()
        store._scan_dirs = d.get("scan_dirs", [])
        for k, v in d.get("skills", {}).items():
            store._skills[k] = PromptPack.from_dict(v)
        return store


# ---------------------------------------------------------------------------
# Skill Registry (discovery + selection)
# ---------------------------------------------------------------------------

class PromptPackRegistry:
    """
    High-level skill management: discovery, BM25 matching, injection context building.
    """

    def __init__(self, store: PromptPackStore | None = None):
        self.store = store or PromptPackStore()
        self._ranker = BM25Ranker()
        self._indexed = False

    def ensure_indexed(self):
        """Rebuild BM25 index if needed."""
        active = self.store.get_active()
        if not self._indexed or len(active) != self._ranker._n_docs:
            self._ranker.index(active)
            self._indexed = True

    def discover(self, scan_dirs: list[str] | None = None) -> int:
        """Scan directories and index new skills."""
        if scan_dirs:
            for d in scan_dirs:
                self.store.add_scan_dir(d)
        new_count = self.store.scan()
        if new_count > 0:
            self._indexed = False
            self.ensure_indexed()
        return new_count

    def match_skills(self, task_text: str, top_k: int = 3,
                     agent_skills: list[str] | None = None) -> list[PromptPack]:
        """
        Find skills matching a task description.

        Args:
            task_text: The task/query to match against
            top_k: Maximum number of skills to return
            agent_skills: Optional list of skill_ids bound to the agent (prioritized)

        Returns:
            Ranked list of matching PromptPacks
        """
        self.ensure_indexed()

        # BM25 ranking
        ranked = self._ranker.query(task_text, top_k=top_k * 2)

        # Boost agent-bound skills
        if agent_skills:
            boosted = []
            for skill_id, score in ranked:
                if skill_id in agent_skills:
                    boosted.append((skill_id, score * 1.5))  # 50% boost
                else:
                    boosted.append((skill_id, score))
            ranked = sorted(boosted, key=lambda x: x[1], reverse=True)

        # Minimum relevance threshold: filter out low-confidence matches.
        # This prevents injecting unrelated skills when the user query
        # only accidentally shares a common keyword with a skill doc.
        # With bigram tokenizer: "打开http://example.com"→2.8, "做Excel表格"→2.1, "写PPTX"→7.5
        # Use relative threshold: at least 25% of top score, with floor of 2.0
        top_score = ranked[0][1] if ranked else 0
        MIN_SCORE = max(2.0, top_score * 0.25)
        results = []
        for skill_id, score in ranked[:top_k]:
            if score < MIN_SCORE:
                continue
            record = self.store.get(skill_id)
            if record and record.is_active:
                results.append(record)

        return results

    def build_context_injection(self, skill_ids: list[str],
                                max_chars: int = 20000) -> str:
        """
        Build a formatted context string to inject into agent system prompt.

        Format:
            ## 可用技能参考 (Skills)

            ### [Skill Name]
            (skill content, truncated to fit)

            ---
        """
        if not skill_ids:
            return ""

        parts = ["## 可用技能参考 (Skills)\n"]
        parts.append("以下是与当前任务相关的技能指南。请参考这些指南来完成任务，")
        parts.append("但如果指南不适用，可以忽略并使用自己的判断。\n")

        total_chars = sum(len(p) for p in parts)

        for skill_id in skill_ids:
            record = self.store.get(skill_id)
            if not record:
                continue

            # Track selection
            record.total_selections += 1
            record.last_selected = time.time()

            # Build skill block
            header = f"\n### {record.name}"
            if record.description:
                header += f"\n> {record.description}"
            header += f"\n> 技能ID: `{record.skill_id}` | 分类: {record.category}\n\n"

            # Truncate content to fit budget
            content = record.content
            # Strip frontmatter for injection
            fm_match = _FRONTMATTER_RE.match(content)
            if fm_match:
                content = content[fm_match.end():]

            remaining = max_chars - total_chars - len(header) - 50
            if remaining <= 0:
                break
            if len(content) > remaining:
                content = content[:remaining] + "\n...(截断)"

            block = header + content + "\n\n---\n"
            parts.append(block)
            total_chars += len(block)

        return "".join(parts)

    def mark_skill_applied(self, skill_id: str, applied: bool = True,
                           task_completed: bool = False):
        """
        After execution, mark whether a skill was actually used.
        Called from ExecutionAnalyzer integration.
        """
        record = self.store.get(skill_id)
        if not record:
            return
        if applied:
            record.total_applied += 1
            if task_completed:
                record.total_completions += 1
        else:
            record.total_fallbacks += 1

    def get_agent_skills(self, agent_skill_ids: list[str]) -> list[PromptPack]:
        """Get skill records for skills bound to an agent."""
        return [self.store.get(sid) for sid in agent_skill_ids
                if self.store.get(sid)]

    def to_dict(self) -> dict:
        return self.store.to_dict()

    @staticmethod
    def from_dict(d: dict) -> PromptPackRegistry:
        store = PromptPackStore.from_dict(d)
        return PromptPackRegistry(store=store)


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------

_global_registry: PromptPackRegistry | None = None


def get_prompt_pack_registry() -> PromptPackRegistry:
    """Get or create the global PromptPackRegistry singleton."""
    global _global_registry
    if _global_registry is None:
        _global_registry = PromptPackRegistry()
    return _global_registry


def init_prompt_pack_registry(data_dir: str = "", extra_scan_dirs: list[str] | None = None) -> PromptPackRegistry:
    """Initialize the global skill registry with default scan dirs and persistence.

    Called once at portal startup. Discovers skills from:
    - {data_dir}/skills/
    - ~/.tudou_claw/skills/
    - Any extra_scan_dirs provided
    - Each agent's working_dir/.claw/skills/ (if agents are loaded)
    """
    import os

    persist_path = os.path.join(data_dir, "skill_registry.json") if data_dir else ""

    # Try to load persisted state
    store = None
    if persist_path and os.path.exists(persist_path):
        try:
            import json
            with open(persist_path) as f:
                store = PromptPackStore.from_dict(json.load(f))
            logger.info("Loaded %d skills from %s", len(store.get_active()), persist_path)
        except Exception as e:
            logger.warning("Failed to load skill registry: %s", e)

    if store is None:
        store = PromptPackStore(persist_path=persist_path)
    else:
        store._persist_path = persist_path

    # Add default scan directories
    default_dirs = []
    if data_dir:
        default_dirs.append(os.path.join(data_dir, "skills"))
    home = os.path.expanduser("~")
    default_dirs.append(os.path.join(home, ".tudou_claw", "skills"))
    # Shipped builtin packs (sibling of this file: app/skills/builtin/)
    _this_dir = os.path.dirname(os.path.abspath(__file__))
    _builtin_dir = os.path.join(_this_dir, "builtin")
    if os.path.isdir(_builtin_dir):
        default_dirs.append(_builtin_dir)
    # Current project's .claw/skills
    cwd = os.getcwd()
    default_dirs.append(os.path.join(cwd, ".claw", "skills"))
    default_dirs.append(os.path.join(cwd, "skills"))

    if extra_scan_dirs:
        default_dirs.extend(extra_scan_dirs)

    for d in default_dirs:
        if os.path.isdir(d):
            store.add_scan_dir(d)

    registry = PromptPackRegistry(store=store)

    # Auto-discover
    new_count = registry.discover()
    if new_count > 0:
        logger.info("Discovered %d new skills from %d directories",
                     new_count, len(store._scan_dirs))
        # Persist
        if persist_path:
            try:
                import json
                os.makedirs(os.path.dirname(persist_path), exist_ok=True)
                with open(persist_path, "w") as f:
                    json.dump(store.to_dict(), f, ensure_ascii=False, indent=2)
            except Exception:
                pass

    set_prompt_pack_registry(registry)
    logger.info("Skill registry initialized: %d skills, %d scan dirs",
                len(store.get_active()), len(store._scan_dirs))
    return registry


def set_prompt_pack_registry(registry: PromptPackRegistry):
    """Replace the global PromptPackRegistry (for testing or custom configs)."""
    global _global_registry
    _global_registry = registry
