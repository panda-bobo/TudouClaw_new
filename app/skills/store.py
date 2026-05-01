"""
TudouClaw Skill Store — Hub 级技能商店

设计目标
========

1. 开放标准兼容
   - 同时支持 TudouClaw 自有 `manifest.yaml` (强校验 + sandbox)
   - 和 Anthropic / OpenClaw 的 `SKILL.md` + YAML frontmatter 开放标准
   - 规范参考: https://agentskills.io/specification
     frontmatter 字段: name, description, metadata(languages,versions,source,tags)

2. 三层分离
   - Catalog  : 可见但未安装 (静态目录扫描而来的可选技能)
   - Installed: 通过 skill_registry 复制到 data/skills_installed/ 的实例
   - Granted  : 通过 skill_registry.grant(skill_id, agent_id) 授权给某 agent

3. 信任分级 (source tier)
   - official   : 库作者官方认证
   - maintainer : TudouClaw 团队维护
   - community  : 社区贡献
   - local      : 用户私有/内部
   用户可通过 config.allowed_sources 控制哪些源对 agent 可见。

4. Annotation-on-Fetch (灵感来自 context-hub / chub)
   - 每个 skill_id 可以附加本地注释 (agent 踩过的坑/教训)
   - 注释存在 data/skill_annotations/<safe_id>.json
   - SkillRegistry.build_prompt_block 注入 agent prompt 时会自动把对应
     skill 的 annotation 拼在描述后面,无需 agent 显式去查

5. 独立 agent 分发
   - 授权 (grant) 仍走 skill_registry.grant，skill 文件已经复制到 install_root
   - 对于跨进程 agent: 额外在 agent 的 working_dir/.claw/granted_skills/
     写一个 pointer json (id, install_dir, granted_at)，agent 本地的
     SkillRegistry 可以据此 lazy-load

所有面向用户字符串通过 i18n.t() 取。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    yaml = None

from ..i18n import t

logger = logging.getLogger("tudou.skill_store")


# ─────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────

SOURCE_TIERS = ("official", "maintainer", "community", "agent", "local")
DEFAULT_SOURCE = "community"

# Anthropic Agent Skills frontmatter delimiter
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?", re.DOTALL)


# ─────────────────────────────────────────────────────────────
# Catalog Entry (Hub-level view of available skills)
# ─────────────────────────────────────────────────────────────

@dataclass
class SkillCatalogEntry:
    """
    SkillStore 暴露给 Portal / agent 的 "可见/可安装" 描述。

    与 SkillInstall 的区别:
    - CatalogEntry 不要求 skill 已经被复制到 install_root
    - 不做 Python AST 校验 (只是"列出来")
    - 只有当用户点"安装"时才真正走 skill_registry.install_from_directory
    """
    id: str = ""                        # <author>/<name> 或 name@version
    name: str = ""                      # 短名
    description: str = ""               # 单语描述
    description_i18n: dict = field(default_factory=dict)
    author: str = ""                    # 作者前缀 (eg "openai", "tudou-builtin")
    version: str = "0.0.0"

    # 开放标准兼容字段
    spec: str = "tudou"                 # "tudou" = manifest.yaml, "agent-skills" = SKILL.md
    runtime: str = "python"             # python | shell | markdown
    entry: str = "main.py"              # 执行入口 (markdown runtime 指 SKILL.md)

    # 信任层级
    source: str = DEFAULT_SOURCE        # official | maintainer | community | local
    tags: list[str] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)   # ["python","javascript"]

    # 适用范围 —— 驱动 skill 商店 UI 里的"给谁用 / 什么时候用"提示。
    # applicable_roles: agent.profile.role 的短名列表 (eg ["coder", "researcher"])。
    #   空列表 = 通用，任何角色都适用。
    # scenarios: 自然语言描述的典型触发情境 (eg ["生产 bug 排查", "测试失败定位"])。
    #   UI 渲染为 chips；也可作为 skill 匹配器的弱信号。
    # 两者都是可选，不影响已注册 skill 的可用性。
    applicable_roles: list[str] = field(default_factory=list)
    scenarios: list[str] = field(default_factory=list)

    # 文件系统定位
    catalog_path: str = ""              # 目录绝对路径 (源)
    size_bytes: int = 0
    last_updated: float = 0.0

    # 状态
    installed: bool = False             # 是否已复制到 install_root
    installed_id: str = ""              # 对应 SkillInstall.id
    sensitive: bool = False

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "description_i18n": dict(self.description_i18n),
            "author": self.author,
            "version": self.version,
            "spec": self.spec,
            "runtime": self.runtime,
            "entry": self.entry,
            "source": self.source,
            "tags": list(self.tags),
            "languages": list(self.languages),
            "applicable_roles": list(self.applicable_roles),
            "scenarios": list(self.scenarios),
            "catalog_path": self.catalog_path,
            "size_bytes": self.size_bytes,
            "last_updated": self.last_updated,
            "installed": self.installed,
            "installed_id": self.installed_id,
            "sensitive": self.sensitive,
        }


# ─────────────────────────────────────────────────────────────
# Annotation (local note attached to a skill id)
# ─────────────────────────────────────────────────────────────

@dataclass
class SkillAnnotation:
    skill_id: str = ""
    notes: list[dict] = field(default_factory=list)   # [{text, author, created_at}]
    updated_at: float = field(default_factory=time.time)

    def add(self, text: str, author: str = "") -> None:
        self.notes.append({
            "text": text,
            "author": author,
            "created_at": time.time(),
        })
        self.updated_at = time.time()

    def to_dict(self) -> dict:
        return {
            "skill_id": self.skill_id,
            "notes": list(self.notes),
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "SkillAnnotation":
        return SkillAnnotation(
            skill_id=d.get("skill_id", ""),
            notes=list(d.get("notes", []) or []),
            updated_at=d.get("updated_at", time.time()),
        )


# ─────────────────────────────────────────────────────────────
# Spec readers (manifest.yaml AND SKILL.md)
# ─────────────────────────────────────────────────────────────

def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse Anthropic Agent Skills frontmatter. Returns (meta_dict, body)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm_text = m.group(1)
    body = text[m.end():]
    if yaml is not None:
        try:
            data = yaml.safe_load(fm_text) or {}
            if isinstance(data, dict):
                return data, body
        except Exception:
            pass
    # fallback: tiny key:value parser (no nested)
    data = {}
    for line in fm_text.splitlines():
        line = line.strip()
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        data[k.strip()] = v.strip()
    return data, body


def _safe_split_csv(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    return [s.strip() for s in str(value).split(",") if s.strip()]


def read_entry_from_skill_md(skill_md_path: str, catalog_root: str = "") -> SkillCatalogEntry | None:
    """Read an Anthropic-spec SKILL.md into a CatalogEntry.

    Tolerates SKILL.md WITHOUT YAML frontmatter — falls back to extracting
    name from the first H1 heading and description from the first
    paragraph. Anthropic's public spec doesn't strictly require
    frontmatter, and skills imported from ClawHub / GitHub often skip
    it. Without this fallback the catalog scanner silently dropped them
    (user report 2026-04-29: "Product Comparison & Review Copywriter
    安装成功后在 skill 商店看不到").
    """
    p = Path(skill_md_path)
    if not p.exists():
        return None
    try:
        text = p.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("read SKILL.md %s failed: %s", skill_md_path, e)
        return None

    meta, _body = _parse_frontmatter(text)
    if not meta:
        meta = {}

    metadata = meta.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    # Fallback name/description from the markdown body when frontmatter
    # didn't supply them. First H1 → name; first non-heading paragraph
    # below it → description.
    md_name = ""
    md_desc = ""
    if not meta.get("name") or not meta.get("description"):
        try:
            from .engine import _extract_skill_md_summary
            md_name, md_desc = _extract_skill_md_summary(str(p))
        except Exception:
            md_name, md_desc = ("", "")

    name = str(meta.get("name") or md_name or p.parent.name or "").strip()
    description = str(meta.get("description") or md_desc or "").strip()
    if not name:
        return None

    # author: prefer frontmatter.metadata.author, then path prefix (only when
    # the file sits at author/skill-name/FILE — i.e. at least 3 path segments)
    author = str(metadata.get("author") or meta.get("author") or "").strip()
    if not author and catalog_root:
        try:
            rel = p.relative_to(catalog_root)
            parts = rel.parts
            if len(parts) >= 3:
                author = parts[0]
        except Exception:
            pass
    if not author:
        author = "community"

    sid = f"{author}/{name}" if "/" not in name else name

    langs = _safe_split_csv(metadata.get("languages"))
    # tags can live at the TOP level of frontmatter (Anthropic's standard
    # SKILL.md shape — see narrator-ai-cli, most ClawHub skills) OR
    # nested under metadata.tags (older Tudou-flavored layout). Accept
    # either; merge if both are present so nothing is dropped.
    _top_tags = _safe_split_csv(meta.get("tags"))
    _meta_tags = _safe_split_csv(metadata.get("tags"))
    tags = list(dict.fromkeys((_top_tags or []) + (_meta_tags or [])))
    src = str(metadata.get("source") or DEFAULT_SOURCE)
    if src not in SOURCE_TIERS:
        src = DEFAULT_SOURCE
    version = str(metadata.get("versions") or metadata.get("version") or "1.0.0").split(",")[0].strip()

    # applicable_roles + scenarios can live at the top level of frontmatter
    # (where we've been putting them) or nested under metadata.* — accept
    # either. Both are optional; defaults to [] = universal applicability.
    roles_raw = meta.get("applicable_roles")
    if roles_raw is None:
        roles_raw = metadata.get("applicable_roles")
    applicable_roles = _safe_split_csv(roles_raw)
    scenarios_raw = meta.get("scenarios")
    if scenarios_raw is None:
        scenarios_raw = metadata.get("scenarios")
    scenarios = _safe_split_csv(scenarios_raw)

    size = 0
    try:
        for fp in p.parent.rglob("*"):
            if fp.is_file():
                size += fp.stat().st_size
    except Exception:
        pass

    return SkillCatalogEntry(
        id=sid,
        name=name,
        description=description,
        author=author,
        version=version,
        spec="agent-skills",
        runtime="markdown",          # md skill = guidance-only, no executable entry
        entry="SKILL.md",
        source=src,
        tags=tags,
        languages=langs,
        applicable_roles=applicable_roles,
        scenarios=scenarios,
        catalog_path=str(p.parent),
        size_bytes=size,
        last_updated=p.stat().st_mtime,
        sensitive=bool(metadata.get("sensitive", False)),
    )


def read_entry_from_manifest_yaml(manifest_path: str, catalog_root: str = "") -> SkillCatalogEntry | None:
    """Read a TudouClaw manifest.yaml into a CatalogEntry (without AST-validating code)."""
    if yaml is None:
        return None
    p = Path(manifest_path)
    if not p.exists():
        return None
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as e:
        logger.warning("read manifest.yaml %s failed: %s", manifest_path, e)
        return None
    if not isinstance(data, dict):
        return None

    name = str(data.get("name") or p.parent.name or "").strip()
    if not name:
        return None
    version = str(data.get("version") or "0.0.0")

    raw_desc = data.get("description")
    if isinstance(raw_desc, dict):
        desc_i18n = {k: str(v) for k, v in raw_desc.items()}
        desc_str = desc_i18n.get("zh-CN") or desc_i18n.get("en") or next(iter(desc_i18n.values()), "")
    else:
        desc_i18n = {}
        desc_str = str(raw_desc or "")

    # author: prefer manifest.author, then path prefix (only when nested >= 3 deep),
    # else fall back to tudou-builtin
    author = str(data.get("author") or "").strip()
    if not author and catalog_root:
        try:
            rel = p.relative_to(catalog_root)
            parts = rel.parts
            if len(parts) >= 3:
                author = parts[0]
        except Exception:
            pass
    if not author:
        author = "tudou-builtin"

    sid = f"{author}/{name}"

    meta = data.get("metadata") or {}
    src = str((meta.get("source") if isinstance(meta, dict) else None) or data.get("source") or DEFAULT_SOURCE)
    if src not in SOURCE_TIERS:
        src = DEFAULT_SOURCE

    tags = _safe_split_csv(data.get("triggers"))
    if isinstance(meta, dict):
        tags += _safe_split_csv(meta.get("tags"))

    hint = data.get("hint") or {}
    sensitive = bool(isinstance(hint, dict) and hint.get("sensitive"))

    # applicable_roles + scenarios — accept at top level of manifest.yaml
    # OR nested under metadata.* for symmetry with SKILL.md frontmatter.
    roles_raw = data.get("applicable_roles")
    if roles_raw is None and isinstance(meta, dict):
        roles_raw = meta.get("applicable_roles")
    applicable_roles = _safe_split_csv(roles_raw)
    scenarios_raw = data.get("scenarios")
    if scenarios_raw is None and isinstance(meta, dict):
        scenarios_raw = meta.get("scenarios")
    scenarios = _safe_split_csv(scenarios_raw)

    size = 0
    try:
        for fp in p.parent.rglob("*"):
            if fp.is_file():
                size += fp.stat().st_size
    except Exception:
        pass

    return SkillCatalogEntry(
        id=sid,
        name=name,
        description=desc_str,
        description_i18n=desc_i18n,
        author=author,
        version=version,
        spec="tudou",
        runtime=str(data.get("runtime") or "python"),
        entry=str(data.get("entry") or "main.py"),
        source=src,
        tags=tags,
        applicable_roles=applicable_roles,
        scenarios=scenarios,
        catalog_path=str(p.parent),
        size_bytes=size,
        last_updated=p.stat().st_mtime,
        sensitive=sensitive,
    )


# ─────────────────────────────────────────────────────────────
# SkillStore (the hub-level catalog + annotation layer)
# ─────────────────────────────────────────────────────────────

class SkillStore:
    """
    技能商店 (Hub 级)。

    职责
    ----
    - 扫描一组 catalog 目录,产出 CatalogEntry 列表 (含未安装的)
    - 代理 skill_registry: 把 catalog entry "安装"成 SkillInstall
    - 管理 annotations (本地笔记, 按 skill_id)
    - 为独立 agent 写出 pointer 文件 (pid 跨进程发现)

    与 app.skills.SkillRegistry 的关系
    -------------------------------
    - Store 是 "商店" (目录层)
    - Registry 是 "已安装层" (hub 维护)
    - install_entry(entry) 会把 catalog 目录复制到 registry.install_root,
      由 registry 做 AST 校验/依赖检查/sha256/grant 等。
    """

    def __init__(self, catalog_dirs: list[str], annotations_dir: str,
                 registry: Any | None = None,
                 allowed_sources: list[str] | None = None):
        self.catalog_dirs = [os.path.abspath(d) for d in (catalog_dirs or [])]
        self.annotations_dir = Path(annotations_dir)
        self.annotations_dir.mkdir(parents=True, exist_ok=True)
        self._registry = registry
        self._allowed_sources = set(allowed_sources or SOURCE_TIERS)
        self._lock = threading.RLock()
        self._entries: dict[str, SkillCatalogEntry] = {}
        self._annotations: dict[str, SkillAnnotation] = {}
        self._load_annotations()

    # ── Registry wiring ──

    def attach_registry(self, registry: Any) -> None:
        self._registry = registry

    def allowed_sources(self) -> list[str]:
        return sorted(self._allowed_sources)

    def set_allowed_sources(self, sources: list[str]) -> None:
        s = {x for x in sources if x in SOURCE_TIERS}
        if s:
            self._allowed_sources = s

    # ── Catalog scan ──

    def scan(self) -> int:
        """
        Walk each catalog dir. Recognize either:
          - manifest.yaml (TudouClaw spec)
          - SKILL.md      (Anthropic Agent Skills spec)
        Return count of entries discovered (total, not delta).
        """
        with self._lock:
            found: dict[str, SkillCatalogEntry] = {}
            for root in self.catalog_dirs:
                if not os.path.isdir(root):
                    continue
                for dirpath, dirnames, filenames in os.walk(root):
                    # skip hidden + install roots
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                    lower = {f.lower(): f for f in filenames}
                    entry: SkillCatalogEntry | None = None
                    if "manifest.yaml" in lower:
                        entry = read_entry_from_manifest_yaml(
                            os.path.join(dirpath, lower["manifest.yaml"]),
                            catalog_root=root,
                        )
                    elif "skill.md" in lower:
                        entry = read_entry_from_skill_md(
                            os.path.join(dirpath, lower["skill.md"]),
                            catalog_root=root,
                        )
                    if entry is None:
                        continue
                    # Honor soft-disable marker file. Entry stays in catalog
                    # but `metadata.disabled = True` so the UI can filter
                    # it out of the default list.
                    try:
                        if os.path.isfile(os.path.join(dirpath, ".disabled")):
                            md = entry.metadata or {}
                            md["disabled"] = True
                            entry.metadata = md
                    except OSError:
                        pass
                    # dedupe: author/name, later catalog dirs override earlier
                    found[entry.id] = entry
            # ── Deduplicate by skill name ──
            # If the same skill name appears under multiple catalog dirs
            # (e.g. official/docx AND imported/docx), keep only one.
            # Priority: installed > community/local > official.
            self._mark_installed(found)
            by_name: dict[str, list[str]] = {}
            for eid, entry in found.items():
                by_name.setdefault(entry.name, []).append(eid)
            for name, eids in by_name.items():
                if len(eids) <= 1:
                    continue
                # Pick best: prefer installed, then community/local over official
                def _priority(eid):
                    e = found[eid]
                    score = 0
                    if e.installed:
                        score += 100
                    if e.source in ("community", "local"):
                        score += 10
                    return score
                eids_sorted = sorted(eids, key=_priority, reverse=True)
                # Keep only the best one, remove the rest
                for eid_to_remove in eids_sorted[1:]:
                    del found[eid_to_remove]
                    logger.debug("SkillStore dedup: removed %s (kept %s)",
                                 eid_to_remove, eids_sorted[0])

            self._entries = found
            logger.info("SkillStore scanned %d catalog dirs, found %d entries",
                        len(self.catalog_dirs), len(found))
            return len(found)

    def _mark_installed(self, entries: dict[str, SkillCatalogEntry]) -> None:
        if self._registry is None:
            return
        try:
            installed = {i.manifest.name: i for i in self._registry.list_all()}
        except Exception:
            installed = {}
        for entry in entries.values():
            inst = installed.get(entry.name)
            if inst is not None:
                entry.installed = True
                entry.installed_id = inst.id

    # ── Listing / search ──

    def list_catalog(self, source_filter: str = "", tag: str = "",
                     query: str = "", include_disallowed: bool = False) -> list[SkillCatalogEntry]:
        with self._lock:
            out = []
            q = (query or "").strip().lower()
            for entry in self._entries.values():
                if not include_disallowed and entry.source not in self._allowed_sources:
                    continue
                if source_filter and entry.source != source_filter:
                    continue
                # Tag filter — case-insensitive equality. SKILL.md authors
                # frequently mix cases (``Video-Narration`` vs the chip
                # the user clicked, ``video-narration``); strict equality
                # would silently drop matches.
                if tag:
                    tl = tag.lower()
                    if not any(str(t).lower() == tl for t in entry.tags):
                        continue
                if q:
                    hay = f"{entry.id} {entry.name} {entry.description} {' '.join(entry.tags)}".lower()
                    if q not in hay:
                        continue
                out.append(entry)
            out.sort(key=lambda e: (e.source != "official", e.source != "maintainer", e.name))
            return out

    def get_entry(self, entry_id: str) -> SkillCatalogEntry | None:
        with self._lock:
            return self._entries.get(entry_id)

    def stats(self) -> dict:
        with self._lock:
            total = len(self._entries)
            by_src: dict[str, int] = {}
            installed_count = 0
            for e in self._entries.values():
                by_src[e.source] = by_src.get(e.source, 0) + 1
                if e.installed:
                    installed_count += 1
            return {
                "total": total,
                "installed": installed_count,
                "by_source": by_src,
                "allowed_sources": self.allowed_sources(),
                "catalog_dirs": list(self.catalog_dirs),
            }

    # ── Install / uninstall (proxy to registry) ──

    def install_entry(self, entry_id: str, installed_by: str = "") -> dict:
        """
        Materialize a catalog entry into an installed skill (via the
        injected registry). Returns a small status dict.
        """
        entry = self.get_entry(entry_id)
        if entry is None:
            raise KeyError(f"catalog entry not found: {entry_id}")
        if self._registry is None:
            raise RuntimeError("SkillStore has no registry attached; cannot install")

        if entry.runtime == "markdown":
            # Pure-guidance skill: no executable side. We still "install" by
            # copying the folder to a stable location and registering a
            # lightweight SkillInstall so UI + grant/annotate work uniformly.
            install_dir = Path(self._registry.install_root) / f"md_{entry.author}_{entry.name}"
            if install_dir.exists():
                shutil.rmtree(install_dir)
            shutil.copytree(entry.catalog_path, install_dir)
            # Fabricate a minimal manifest so registry._save + list_all stay consistent.
            from . import engine as _sk
            manifest = _sk.SkillManifest(
                id=f"{entry.name}@{entry.version}",
                name=entry.name,
                version=entry.version,
                description=entry.description,
                description_i18n=entry.description_i18n,
                author=entry.author,
                runtime="markdown",
                entry="SKILL.md",
                triggers=list(entry.tags),
                raw={"source": entry.source, "spec": "agent-skills"},
            )
            inst = _sk.SkillInstall(
                id=manifest.id,
                manifest=manifest,
                install_dir=str(install_dir),
                status=_sk.SkillStatus.READY,
                installed_by=installed_by,
            )
            with self._registry._lock:
                self._registry._installs[inst.id] = inst
                self._registry._save()
            entry.installed = True
            entry.installed_id = inst.id
            return {"ok": True, "id": inst.id, "runtime": "markdown"}

        # executable skill: delegate full install
        try:
            inst = self._registry.install_from_directory(entry.catalog_path, installed_by=installed_by)
        except ValueError:
            # already installed: find it and return
            for existing in self._registry.list_all():
                if existing.manifest.name == entry.name:
                    entry.installed = True
                    entry.installed_id = existing.id
                    return {"ok": True, "id": existing.id, "already": True,
                            "runtime": existing.manifest.runtime}
            raise
        entry.installed = True
        entry.installed_id = inst.id
        return {"ok": True, "id": inst.id, "runtime": inst.manifest.runtime}

    def uninstall_entry(self, entry_id: str) -> bool:
        entry = self.get_entry(entry_id)
        if entry is None or not entry.installed or self._registry is None:
            return False
        ok = self._registry.uninstall(entry.installed_id)
        if ok:
            entry.installed = False
            entry.installed_id = ""
        return ok

    def disable_entry(self, entry_id: str, disabled: bool = True) -> bool:
        """Soft-disable a catalog entry. The skill stays on disk and can
        be re-enabled later, but the UI hides it from the default list and
        agents can no longer be granted it.

        Persisted in the catalog entry annotation file so the flag survives
        restarts. To restore, call with disabled=False.
        """
        entry = self.get_entry(entry_id)
        if entry is None:
            return False
        # entry.metadata is the place to stash UI-level flags; rescan
        # preserves it via _meta.json (or annotation store, depending on
        # source). Simplest reliable approach: write a sibling marker
        # file `.disabled` next to manifest.yaml / SKILL.md.
        try:
            from pathlib import Path
            base = Path(entry.catalog_path)
            marker = base / ".disabled"
            if disabled:
                marker.touch()
            elif marker.exists():
                marker.unlink()
            # Reflect in in-memory entry too so list endpoints see it.
            entry.metadata = entry.metadata or {}
            entry.metadata["disabled"] = bool(disabled)
            return True
        except Exception:
            return False

    def delete_catalog_entry(self, entry_id: str) -> bool:
        """Hard-delete: remove the catalog directory from disk entirely.
        If installed, uninstall first. Cannot be undone — caller MUST
        confirm with the user. Returns True on success.
        """
        with self._lock:
            entry = self._entries.get(entry_id)
        if entry is None:
            return False
        # Uninstall first if currently installed.
        # SkillCatalogEntry attribute is ``installed`` (bool) and
        # ``installed_id`` (str). Fall back via getattr for defensive
        # tolerance of older pickled entries that may lack the field.
        if getattr(entry, "installed", False) and self._registry is not None:
            iid = getattr(entry, "installed_id", "") or ""
            if iid:
                try:
                    self._registry.uninstall(iid)
                except Exception as _ue:
                    logger.warning("uninstall before delete failed: %s", _ue)
        # Remove the catalog directory
        try:
            from pathlib import Path
            import shutil
            p = Path(entry.catalog_path)
            if p.exists() and p.is_dir():
                # Sanity check: refuse to delete obviously dangerous paths
                # (only allow under known catalog roots)
                ok_root = any(
                    str(p).startswith(str(r))
                    for r in self.catalog_dirs
                )
                if not ok_root:
                    logger.warning("delete refused: %s outside catalog roots", p)
                    return False
                shutil.rmtree(p)
            # Drop from in-memory dict.
            # BUG FIX: ``_entries`` is a dict[str, SkillCatalogEntry]
            # (see line 435). Old code did `[e for e in self._entries
            # if e.id != entry_id]` which iterates KEYS (strings) and
            # called `.id` on them → AttributeError → caught silently
            # → returned False → UI showed "删除失败" with no clue why.
            with self._lock:
                self._entries.pop(entry_id, None)
            return True
        except Exception as e:
            logger.warning("delete_catalog_entry %s failed: %s", entry_id, e)
            return False

    # ── Grant/revoke (with independent-agent pointer file) ──

    def grant(self, installed_id: str, agent_id: str,
              agent_working_dir: str = "") -> bool:
        """
        Authorize a skill to an agent. On top of the registry.grant call,
        also write a pointer file into the agent's working dir so that
        an independent agent process can discover its granted skills
        without re-loading the hub registry.
        """
        if self._registry is None:
            return False
        self._registry.grant(installed_id, agent_id)

        if agent_working_dir:
            try:
                inst = self._registry.get(installed_id)
                pdir = Path(agent_working_dir) / ".claw" / "granted_skills"
                pdir.mkdir(parents=True, exist_ok=True)
                pfile = pdir / f"{installed_id.replace('/', '__').replace('@', '_at_')}.json"
                pfile.write_text(json.dumps({
                    "id": installed_id,
                    "name": inst.manifest.name if inst else "",
                    "install_dir": inst.install_dir if inst else "",
                    "runtime": inst.manifest.runtime if inst else "",
                    "granted_at": time.time(),
                    "agent_id": agent_id,
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as e:
                logger.warning("grant: pointer write failed: %s", e)
        return True

    def revoke(self, installed_id: str, agent_id: str,
               agent_working_dir: str = "") -> bool:
        if self._registry is None:
            return False
        ok = self._registry.revoke(installed_id, agent_id)
        if ok and agent_working_dir:
            try:
                pfile = (Path(agent_working_dir) / ".claw" / "granted_skills"
                         / f"{installed_id.replace('/', '__').replace('@', '_at_')}.json")
                if pfile.exists():
                    pfile.unlink()
            except Exception:
                pass
        return ok

    # ── Annotations ──

    def _annotation_file(self, skill_id: str) -> Path:
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", skill_id)
        return self.annotations_dir / f"{safe}.json"

    def _load_annotations(self) -> None:
        if not self.annotations_dir.exists():
            return
        for fp in self.annotations_dir.glob("*.json"):
            try:
                data = json.loads(fp.read_text(encoding="utf-8"))
                ann = SkillAnnotation.from_dict(data)
                if ann.skill_id:
                    self._annotations[ann.skill_id] = ann
            except Exception as e:
                logger.debug("skip annotation %s: %s", fp, e)

    def annotate(self, skill_id: str, text: str, author: str = "") -> dict:
        text = (text or "").strip()
        if not text:
            raise ValueError("annotation text must not be empty")
        with self._lock:
            ann = self._annotations.get(skill_id) or SkillAnnotation(skill_id=skill_id)
            ann.add(text, author=author)
            self._annotations[skill_id] = ann
            self._annotation_file(skill_id).write_text(
                json.dumps(ann.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8")
            return ann.to_dict()

    def clear_annotation(self, skill_id: str) -> bool:
        with self._lock:
            self._annotations.pop(skill_id, None)
            fp = self._annotation_file(skill_id)
            if fp.exists():
                fp.unlink()
                return True
            return False

    def get_annotation(self, skill_id: str) -> SkillAnnotation | None:
        return self._annotations.get(skill_id)

    def list_annotations(self) -> list[dict]:
        with self._lock:
            return [a.to_dict() for a in self._annotations.values()]

    def build_annotation_block(self, skill_id: str, locale: str = "zh-CN") -> str:
        """
        Produce a small text block to append after a skill's description
        in the agent prompt (annotation-on-fetch, chub-style).
        """
        ann = self._annotations.get(skill_id)
        if not ann or not ann.notes:
            return ""
        lines = ["  💡 本地笔记 (annotations):"] if locale.startswith("zh") else ["  💡 Local notes:"]
        for n in ann.notes[-3:]:  # only last 3 most recent
            txt = str(n.get("text", "")).strip()
            if not txt:
                continue
            lines.append(f"    - {txt}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
# Module-level singleton
# ─────────────────────────────────────────────────────────────

_GLOBAL_STORE: SkillStore | None = None


def init_store(catalog_dirs: list[str], annotations_dir: str,
               registry: Any | None = None,
               allowed_sources: list[str] | None = None) -> SkillStore:
    global _GLOBAL_STORE
    _GLOBAL_STORE = SkillStore(
        catalog_dirs=catalog_dirs,
        annotations_dir=annotations_dir,
        registry=registry,
        allowed_sources=allowed_sources,
    )
    _GLOBAL_STORE.scan()
    return _GLOBAL_STORE


def get_store() -> SkillStore | None:
    return _GLOBAL_STORE


# ─────────────────────────────────────────────────────────────
# Anthropic Agent Skills ingestion (import from ~/.claude/skills)
# ─────────────────────────────────────────────────────────────

def import_agent_skill(src_path: str, catalog_dir: str,
                        tier: str = "community",
                        overwrite: bool = True) -> dict:
    """
    Copy an Anthropic Claude Code skill folder (SKILL.md + ancillary
    files) into TudouClaw's user-level catalog dir so the normal
    SkillStore.scan() picks it up as a CatalogEntry.

    Parameters
    ----------
    src_path: path to an existing Anthropic skill directory. Must contain
        a top-level SKILL.md with YAML frontmatter.
    catalog_dir: TudouClaw's user-level catalog root. Typically
        ``<data_dir>/skill_catalog`` — the same path hub.py passes to
        init_store().
    tier: source tier to tag the imported skill with. One of
        official/maintainer/community/local.
    overwrite: if a folder with the same name already exists in the
        catalog dir, overwrite it. If False and the target exists, the
        import is a no-op and returns ``already_present=True``.

    Returns
    -------
    dict with fields:
        ok, source_path, target_path, name, description, tier,
        already_present, skipped_reason, sha256
    """
    src = Path(src_path).expanduser().resolve()
    if not src.exists() or not src.is_dir():
        return {"ok": False, "error": f"src_path not a directory: {src}"}

    skill_md = src / "SKILL.md"
    if not skill_md.exists():
        # tolerate case variations
        alt = [p for p in src.iterdir() if p.name.lower() == "skill.md"]
        if not alt:
            return {"ok": False, "error": f"no SKILL.md in {src}"}
        skill_md = alt[0]

    # Parse frontmatter to extract name (used for target folder name).
    try:
        text = skill_md.read_text(encoding="utf-8")
    except Exception as e:
        return {"ok": False, "error": f"read SKILL.md failed: {e}"}

    meta, _body = _parse_frontmatter(text)
    name = str(meta.get("name") or src.name or "").strip()
    description = str(meta.get("description") or "").strip()
    if not name:
        return {"ok": False, "error": "SKILL.md frontmatter missing 'name'"}

    if tier not in SOURCE_TIERS:
        tier = DEFAULT_SOURCE

    catalog_root = Path(catalog_dir).expanduser().resolve()
    catalog_root.mkdir(parents=True, exist_ok=True)

    # Target folder: <catalog_root>/imported/<name>
    target = catalog_root / "imported" / name
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        if not overwrite:
            return {
                "ok": True,
                "already_present": True,
                "skipped_reason": "target exists and overwrite=False",
                "source_path": str(src),
                "target_path": str(target),
                "name": name,
                "description": description,
                "tier": tier,
            }
        shutil.rmtree(target)

    shutil.copytree(src, target)

    # Make all copied files writable (upstream sources may be read-only).
    for _f in target.rglob("*"):
        try:
            _f.chmod(0o644 if _f.is_file() else 0o755)
        except Exception:
            pass

    # Inject a `metadata.source` marker so scan() tags the tier
    # correctly even without touching the SKILL.md body. We do this by
    # writing a sibling `.tudou_import.json` file — the catalog reader
    # itself reads the frontmatter, so we also patch the frontmatter
    # in-place with a minimal metadata block if one is missing.
    target_md = target / "SKILL.md"
    try:
        _ensure_source_tier_in_frontmatter(target_md, tier)
    except Exception as e:
        logger.warning("patch frontmatter failed for %s: %s", target_md, e)

    import_record = {
        "source_path": str(src),
        "target_path": str(target),
        "name": name,
        "description": description,
        "tier": tier,
        "imported_at": time.time(),
        "spec": "agent-skills",
    }
    try:
        (target / ".tudou_import.json").write_text(
            json.dumps(import_record, ensure_ascii=False, indent=2),
            encoding="utf-8")
    except Exception:
        pass

    # Compute a quick integrity hash over SKILL.md for later upgrade
    # detection.
    try:
        import hashlib
        sha = hashlib.sha256(target_md.read_bytes()).hexdigest()
    except Exception:
        sha = ""

    logger.info("imported agent-skill %s from %s -> %s", name, src, target)
    return {
        "ok": True,
        "already_present": False,
        "source_path": str(src),
        "target_path": str(target),
        "name": name,
        "description": description,
        "tier": tier,
        "sha256": sha,
    }


def _ensure_source_tier_in_frontmatter(skill_md: Path, tier: str) -> None:
    """
    Patch a SKILL.md so its YAML frontmatter carries
    ``metadata.source: <tier>``. Leaves the body untouched. If there is
    no frontmatter at all, wraps the file in a minimal one.
    """
    if yaml is None:
        return
    # copytree preserved source permissions; some upstream skill files are
    # read-only. Make sure we can write the patched frontmatter back.
    try:
        skill_md.chmod(0o644)
    except Exception:
        pass
    text = skill_md.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if m:
        fm_text = m.group(1)
        body = text[m.end():]
        data = yaml.safe_load(fm_text) or {}
        if not isinstance(data, dict):
            data = {}
    else:
        data = {}
        body = text

    md = data.get("metadata") or {}
    if not isinstance(md, dict):
        md = {}
    if md.get("source") != tier:
        md["source"] = tier
        data["metadata"] = md
        new_fm = yaml.safe_dump(data, allow_unicode=True, sort_keys=False).strip()
        new_text = f"---\n{new_fm}\n---\n{body.lstrip()}"
        skill_md.write_text(new_text, encoding="utf-8")


def import_anthropic_skills_bulk(src_root: str, catalog_dir: str,
                                  include: list[str] | None = None,
                                  tier: str = "community",
                                  overwrite: bool = True) -> list[dict]:
    """
    Walk ``src_root`` for first-level subdirectories each containing a
    SKILL.md, import each one. Optional ``include`` filters by exact
    folder name.
    """
    root = Path(src_root).expanduser().resolve()
    results: list[dict] = []
    if not root.exists():
        return [{"ok": False, "error": f"src_root not found: {root}"}]
    for child in sorted(root.iterdir()):
        if not child.is_dir():
            continue
        if include and child.name not in include:
            continue
        if not (child / "SKILL.md").exists() and not (child / "skill.md").exists():
            continue
        results.append(import_agent_skill(
            str(child), catalog_dir, tier=tier, overwrite=overwrite))
    return results


# ─────────────────────────────────────────────────────────────
# Remote URL scanning and import
# ─────────────────────────────────────────────────────────────

import tempfile

def scan_remote_url(url: str) -> dict:
    """
    Fetch a remote URL (GitHub repo, zip, tar.gz) and scan it for
    skill packages (SKILL.md or manifest.yaml).

    Supported URL patterns:
      - GitHub repo:  https://github.com/user/repo
      - GitHub folder: https://github.com/user/repo/tree/main/skills/pdf
      - Zip file:     https://example.com/skill.zip
      - Tar.gz file:  https://example.com/skill.tar.gz

    Returns a dict with:
      ok, skills: [{name, description, path, spec, has_entry, files}], temp_dir
    """
    import urllib.request
    import zipfile
    import tarfile

    url = (url or "").strip()
    if not url:
        return {"ok": False, "error": "URL is required"}

    tmp_dir = tempfile.mkdtemp(prefix="tudou_skill_scan_")

    try:
        # ── Resolve GitHub URLs ──
        download_url = url
        is_github = "github.com" in url

        if is_github:
            download_url = _resolve_github_download_url(url)

        # ── Download ──
        logger.info("scan_remote_url: downloading %s", download_url)
        headers = {"User-Agent": "TudouClaw-SkillStore/1.0"}
        req = urllib.request.Request(download_url, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=60)
            data = resp.read()
        except Exception as e:
            return {"ok": False, "error": f"Download failed: {e}", "temp_dir": tmp_dir}

        # ── Extract ──
        extract_dir = os.path.join(tmp_dir, "extracted")
        os.makedirs(extract_dir, exist_ok=True)

        content_type = resp.headers.get("Content-Type", "")
        is_zip = (download_url.endswith(".zip") or "zip" in content_type
                  or data[:4] == b"PK\x03\x04")
        is_tar = (download_url.endswith((".tar.gz", ".tgz"))
                  or "tar" in content_type or "gzip" in content_type)

        if is_zip:
            archive_path = os.path.join(tmp_dir, "download.zip")
            with open(archive_path, "wb") as f:
                f.write(data)
            with zipfile.ZipFile(archive_path) as zf:
                zf.extractall(extract_dir)
        elif is_tar:
            archive_path = os.path.join(tmp_dir, "download.tar.gz")
            with open(archive_path, "wb") as f:
                f.write(data)
            with tarfile.open(archive_path, "r:*") as tf:
                tf.extractall(extract_dir)
        else:
            # Could be a single SKILL.md content or unrecognized format
            # Try treating as zip first, then tar
            archive_path = os.path.join(tmp_dir, "download.bin")
            with open(archive_path, "wb") as f:
                f.write(data)
            extracted = False
            try:
                with zipfile.ZipFile(archive_path) as zf:
                    zf.extractall(extract_dir)
                    extracted = True
            except zipfile.BadZipFile:
                pass
            if not extracted:
                try:
                    with tarfile.open(archive_path, "r:*") as tf:
                        tf.extractall(extract_dir)
                        extracted = True
                except Exception:
                    pass
            if not extracted:
                return {"ok": False,
                        "error": "Could not extract archive. "
                                 "Supported formats: .zip, .tar.gz, GitHub repo URL",
                        "temp_dir": tmp_dir}

        # ── Scan for skills ──
        skills_found = _scan_directory_for_skills(extract_dir)

        if not skills_found:
            return {"ok": False,
                    "error": "No skill packages found (no SKILL.md or manifest.yaml)",
                    "temp_dir": tmp_dir,
                    "scanned_dirs": _list_dirs(extract_dir)}

        return {
            "ok": True,
            "url": url,
            "skills": skills_found,
            "skill_count": len(skills_found),
            "temp_dir": tmp_dir,
        }

    except Exception as e:
        logger.error("scan_remote_url failed: %s", e)
        return {"ok": False, "error": str(e), "temp_dir": tmp_dir}


def import_from_scan_result(temp_dir: str, skill_names: list[str],
                            catalog_dir: str, tier: str = "community") -> list[dict]:
    """
    After a successful scan_remote_url, import selected skills from
    the temp directory into the catalog.

    Parameters
    ----------
    temp_dir: the temp_dir returned by scan_remote_url
    skill_names: list of skill names to import (from scan results)
    catalog_dir: target catalog directory
    tier: source tier to assign
    """
    extract_dir = os.path.join(temp_dir, "extracted")
    if not os.path.isdir(extract_dir):
        return [{"ok": False, "error": "temp_dir invalid or expired"}]

    # Re-scan to find skill paths
    skills = _scan_directory_for_skills(extract_dir)
    name_to_path = {s["name"]: s["path"] for s in skills}

    results = []
    for name in skill_names:
        skill_path = name_to_path.get(name)
        if not skill_path:
            results.append({"ok": False, "name": name,
                            "error": f"skill '{name}' not found in scan results"})
            continue
        result = import_agent_skill(skill_path, catalog_dir, tier=tier, overwrite=True)
        results.append(result)

    # Cleanup temp dir
    try:
        shutil.rmtree(temp_dir)
    except Exception:
        pass

    return results


def cleanup_scan_temp(temp_dir: str) -> None:
    """Remove a temp directory from a previous scan."""
    if temp_dir and os.path.isdir(temp_dir) and "tudou_skill_scan_" in temp_dir:
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass


def _resolve_github_download_url(url: str) -> str:
    """Convert a GitHub URL into a downloadable zip URL."""
    import re as _re

    url = url.rstrip("/")
    # Remove .git suffix
    if url.endswith(".git"):
        url = url[:-4]

    # Pattern: github.com/user/repo/tree/branch/path/to/folder
    m = _re.match(
        r"https?://github\.com/([^/]+)/([^/]+)/tree/([^/]+)(?:/(.+))?", url)
    if m:
        user, repo, branch = m.group(1), m.group(2), m.group(3)
        # Download the whole repo zip (GitHub doesn't support folder-level download easily)
        return f"https://github.com/{user}/{repo}/archive/refs/heads/{branch}.zip"

    # Pattern: github.com/user/repo (default branch)
    m = _re.match(r"https?://github\.com/([^/]+)/([^/]+)/?$", url)
    if m:
        user, repo = m.group(1), m.group(2)
        # Try main, then master
        return f"https://github.com/{user}/{repo}/archive/refs/heads/main.zip"

    # Already a direct download URL
    return url


def _scan_directory_for_skills(root_dir: str) -> list[dict]:
    """Recursively scan a directory for skill packages."""
    skills = []
    root = Path(root_dir)

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d != "__pycache__"
                       and d != "node_modules"]
        lower_map = {f.lower(): f for f in filenames}

        skill_file = None
        spec = None
        if "skill.md" in lower_map:
            skill_file = os.path.join(dirpath, lower_map["skill.md"])
            spec = "agent-skills"
        elif "manifest.yaml" in lower_map:
            skill_file = os.path.join(dirpath, lower_map["manifest.yaml"])
            spec = "tudou"

        if skill_file is None:
            continue

        # Parse metadata
        try:
            if spec == "agent-skills":
                entry = read_entry_from_skill_md(skill_file, catalog_root=root_dir)
            else:
                entry = read_entry_from_manifest_yaml(skill_file, catalog_root=root_dir)

            if entry is None:
                continue

            # List files in this skill directory
            skill_dir = os.path.dirname(skill_file)
            file_list = []
            for f in sorted(os.listdir(skill_dir)):
                fp = os.path.join(skill_dir, f)
                if os.path.isfile(fp):
                    file_list.append({
                        "name": f,
                        "size": os.path.getsize(fp),
                    })

            skills.append({
                "name": entry.name,
                "description": entry.description[:200],
                "author": entry.author,
                "version": entry.version,
                "spec": spec,
                "runtime": entry.runtime,
                "tags": entry.tags,
                "path": skill_dir,
                "files": file_list,
                "file_count": len(file_list),
            })
        except Exception as e:
            logger.debug("scan skill at %s failed: %s", dirpath, e)

    return skills


def _list_dirs(root: str, max_depth: int = 3) -> list[str]:
    """List directory structure for debugging."""
    result = []
    root_path = Path(root)
    for dirpath, dirnames, filenames in os.walk(root):
        depth = len(Path(dirpath).relative_to(root_path).parts)
        if depth > max_depth:
            dirnames.clear()
            continue
        rel = os.path.relpath(dirpath, root)
        for f in filenames[:10]:
            result.append(os.path.join(rel, f))
    return result[:50]


# ─────────────────────────────────────────────────────────────
# CLI entry: python -m app.skill_store import-anthropic ...
# ─────────────────────────────────────────────────────────────

def _cli(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(
        prog="python -m app.skill_store",
        description="TudouClaw Skill Store helpers")
    sub = p.add_subparsers(dest="cmd", required=True)

    q = sub.add_parser("import-anthropic",
                       help="Import one or more Anthropic Claude Code skills")
    q.add_argument("--src", required=True,
                   help="source path: either a single skill dir, or a "
                        "parent dir containing several skill dirs")
    q.add_argument("--catalog-dir", required=True,
                   help="target catalog dir (usually <data>/skill_catalog)")
    q.add_argument("--tier", default="community",
                   choices=list(SOURCE_TIERS))
    q.add_argument("--include", default="",
                   help="comma-separated folder names to include (bulk mode)")
    q.add_argument("--no-overwrite", action="store_true",
                   help="skip if target already exists")
    q.add_argument("--install", action="store_true",
                   help="after copy, run SkillStore.scan() and install each "
                        "imported entry into the hub registry")
    q.add_argument("--install-root", default="",
                   help="install_root for the SkillRegistry when --install "
                        "is set (defaults to <catalog_dir>/../skills_installed)")
    q.add_argument("--installed-by", default="cli")

    r = sub.add_parser("scan", help="Scan a catalog dir and list entries")
    r.add_argument("--catalog-dir", required=True)

    args = p.parse_args(argv)

    if args.cmd == "import-anthropic":
        src = Path(args.src).expanduser().resolve()
        single = (src / "SKILL.md").exists() or (src / "skill.md").exists()
        overwrite = not args.no_overwrite

        if single:
            res = [import_agent_skill(str(src), args.catalog_dir,
                                       tier=args.tier, overwrite=overwrite)]
        else:
            include = [x.strip() for x in args.include.split(",") if x.strip()]
            res = import_anthropic_skills_bulk(
                str(src), args.catalog_dir,
                include=include or None,
                tier=args.tier, overwrite=overwrite)

        for r_ in res:
            tag = "OK" if r_.get("ok") else "FAIL"
            print(f"[{tag}] {r_.get('name','?')}: {r_.get('target_path') or r_.get('error')}")

        if args.install and any(r_.get("ok") for r_ in res):
            from . import engine as _eng
            install_root = args.install_root or os.path.join(
                os.path.dirname(os.path.abspath(args.catalog_dir)),
                "skills_installed")
            persist_path = os.path.join(install_root, "..", "skills.json")
            registry = _eng.SkillRegistry(
                install_root=install_root,
                persist_path=os.path.abspath(persist_path),
            )
            store = SkillStore(
                catalog_dirs=[args.catalog_dir],
                annotations_dir=os.path.join(
                    os.path.dirname(os.path.abspath(args.catalog_dir)),
                    "skill_annotations"),
                registry=registry,
            )
            store.scan()
            imported_names = {r_["name"] for r_ in res if r_.get("ok")}
            for entry in store.list_catalog(include_disallowed=True):
                if entry.name not in imported_names:
                    continue
                try:
                    out = store.install_entry(entry.id, installed_by=args.installed_by)
                    print(f"  ↳ installed {entry.name}: {out}")
                except Exception as e:
                    print(f"  ↳ install {entry.name} FAILED: {e}")
        return 0

    if args.cmd == "scan":
        store = SkillStore(
            catalog_dirs=[args.catalog_dir],
            annotations_dir=os.path.join(
                os.path.dirname(os.path.abspath(args.catalog_dir)),
                "skill_annotations"),
        )
        n = store.scan()
        print(f"scanned {n} entries in {args.catalog_dir}:")
        for e in store.list_catalog(include_disallowed=True):
            print(f"  - {e.id}  [{e.spec}/{e.runtime}/{e.source}]  {e.name}")
            if e.description:
                print(f"      {e.description[:100]}")
        return 0

    return 1


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
