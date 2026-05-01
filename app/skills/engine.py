"""
TudouClaw Skill 系统。

设计原则:
- Skill 是 agent 面向的能力包，MCP 是底层通道（通过 depends_on.mcp 声明）
- Skill 之间无依赖（不构成依赖图）
- Skill 严格派: 内部只允许纯计算 + 通过 ctx 调用声明过的 MCP/LLM
- 所有 IO 必须经过 MCP，由 import 静态校验和 runtime ctx 双重保证
- 默认无运行时审批；admin 可配置 escalate 规则升级到 approval queue
- 所有面向用户字符串通过 i18n.t() 取，不硬编码

核心数据结构:
- SkillManifest: 静态描述（从 manifest.yaml 解析）
- SkillInstall: 已安装到节点的 skill 实例（带状态、依赖检查结果）
- SkillGrant: agent → skill 的授权关系（写在 agent.granted_skills）
- SkillRegistry: 全局注册表（持久化在 data/skills.json）

文件布局约定:
    skills/<skill_id>/
        manifest.yaml    # 必须
        main.py          # python runtime
        README.md        # 可选
        assets/          # 可选
"""
from __future__ import annotations

import ast
import hashlib
import json
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Callable

try:
    import yaml
except ImportError:
    yaml = None

from ..i18n import t

logger = logging.getLogger("tudou.skills")


# ─────────────────────────────────────────────────────────────
# 常量与配置（外置到 config 文件友好）
# ─────────────────────────────────────────────────────────────

SUPPORTED_RUNTIMES = ("python", "shell", "markdown")
# markdown runtime = Anthropic Agent Skills spec (SKILL.md + frontmatter).
# These skills are "guidance-only": the SKILL.md body is injected into the
# agent's system prompt and the agent uses its regular tools (Bash, MCP,
# etc.) to follow the instructions. They are NOT invoked through
# SkillRuntime.run — see _run_markdown below for the stub.
REQUIRED_MANIFEST_FIELDS = ("name", "version", "description", "runtime")

# 严格派禁止 import 的模块（IO / 网络 / 进程 / 文件系统）
# 说明：
#   - 禁联网：只禁有网络能力的具体子模块（如 urllib.request / urllib.error），
#     不禁笼统的顶层包。例如 urllib.parse / urllib.robotparser 是纯字符串解析，
#     安全、常用，必须放行。
#   - 禁命令：subprocess / os.system / pty 等能 fork 出外部进程的
#   - 禁二进制注入：ctypes / cffi
#   - 禁不可信反序列化：pickle / marshal / shelve
#   - 禁文件系统遍历：shutil / glob / tempfile
# 注意：模块匹配时同时按 "top-level 名" 和 "完整点号名" 两种方式判断。
FORBIDDEN_IMPORTS = {
    # ----- 顶层模块（整包禁） -----
    "socket", "urllib2", "urllib3", "requests", "httpx", "aiohttp",
    "ftplib", "telnetlib", "smtplib", "imaplib", "poplib",
    "subprocess", "multiprocessing", "popen", "pty",
    "ctypes", "cffi",
    "shutil", "glob", "tempfile",
    "sqlite3", "pymysql", "psycopg2",
    "pickle", "marshal", "shelve",
    # ----- 具体子模块（点号完整名禁） -----
    # urllib 的联网子模块要禁，但 urllib.parse / urllib.robotparser 放行
    "urllib.request", "urllib.error",
    # http 标准库里会建连接的子模块要禁（http.HTTPStatus 等纯枚举不会命中，
    # 因为我们只会在 import/from-import 时匹配）
    "http.client", "http.server", "http.cookiejar",
    # os 下面能直接建进程 / 写 env 的子 API（import os 本身允许，但只能
    # 用来读 os.path / os.sep 之类的纯计算子模块；凭据与 env 必须经
    # ctx.env() 拿，见 _check_no_env_access）
    "os.system", "os.popen",
}

# skill 代码里禁止直接访问的 os 属性/函数。必须走 ctx.env() 从主进程
# 显式下发的 allowed_env_keys 里拿。这样每个 skill 能看到的环境变量
# 是装机时白名单里的那一小撮，不能顺手读 ANTHROPIC_API_KEY。
FORBIDDEN_OS_ENV_ATTRS = {
    "environ", "environb", "getenv", "getenvb",
    "putenv", "unsetenv",
}

# 允许的标准库（白名单优先于黑名单）
ALLOWED_IMPORTS = {
    "json", "re", "math", "datetime", "time", "calendar",
    "collections", "itertools", "functools", "operator",
    "string", "textwrap", "unicodedata", "html", "xml.etree.ElementTree",
    "base64", "hashlib", "hmac", "secrets", "uuid",
    "typing", "dataclasses", "enum", "abc",
    "decimal", "fractions", "statistics",
    "copy", "pprint", "logging",
}


# ─────────────────────────────────────────────────────────────
# 数据模型
# ─────────────────────────────────────────────────────────────

@dataclass
class MCPDependency:
    id: str = ""           # MCP server id
    tools: list[str] = field(default_factory=list)  # 用到的工具名（最小权限）
    optional: bool = False

    def to_dict(self) -> dict:
        return {"id": self.id, "tools": list(self.tools), "optional": self.optional}

    @staticmethod
    def from_dict(d: dict) -> "MCPDependency":
        return MCPDependency(
            id=d.get("id", ""),
            tools=list(d.get("tools", []) or []),
            optional=bool(d.get("optional", False)),
        )


@dataclass
class LLMDependency:
    capability: str = "text_generation"
    preferred_model: str = ""
    optional: bool = True

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "LLMDependency":
        return LLMDependency(
            capability=d.get("capability", "text_generation"),
            preferred_model=d.get("preferred_model", ""),
            optional=bool(d.get("optional", True)),
        )


@dataclass
class SkillInput:
    name: str = ""
    type: str = "string"
    required: bool = False
    default: Any = None
    description: str = ""
    enum: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "SkillInput":
        return SkillInput(
            name=d.get("name", ""),
            type=d.get("type", "string"),
            required=bool(d.get("required", False)),
            default=d.get("default"),
            description=d.get("description", ""),
            enum=list(d.get("enum", []) or []),
        )


@dataclass
class SkillManifest:
    """从 manifest.yaml 解析得到的静态描述。"""
    id: str = ""              # 通常 = name + "@" + version
    name: str = ""
    version: str = "0.0.0"
    description: str = ""     # 可以是字符串，或 dict { zh-CN: ..., en: ... }
    description_i18n: dict = field(default_factory=dict)
    author: str = ""
    runtime: str = "python"
    entry: str = "main.py"
    sha256: str = ""

    depends_on_mcp: list[MCPDependency] = field(default_factory=list)
    depends_on_llm: list[LLMDependency] = field(default_factory=list)

    inputs: list[SkillInput] = field(default_factory=list)
    outputs: list[dict] = field(default_factory=list)

    triggers: list[str] = field(default_factory=list)
    sensitive: bool = False
    sensitive_reason: str = ""

    # ── 安装时声明的可用环境变量 key 白名单 ──
    # 主进程在调用 skill 时，只会把这几个 key 的值装进 ctx.env() 可读的 dict。
    # skill 代码里任何直接 os.environ / os.getenv 访问都会被静态校验器拒绝。
    allowed_env_keys: list[str] = field(default_factory=list)

    # ── HTTP 出站白名单（host 列表或精确 URL 前缀） ──
    # 只有命中白名单的 host/URL 才会被 ctx.http 允许发出。空列表 = 禁用 ctx.http。
    allowed_http_hosts: list[str] = field(default_factory=list)

    raw: dict = field(default_factory=dict)  # 原始 manifest，便于扩展字段

    def get_description(self, locale: str = "zh-CN") -> str:
        if self.description_i18n and locale in self.description_i18n:
            return self.description_i18n[locale]
        if self.description_i18n and "zh-CN" in self.description_i18n:
            return self.description_i18n["zh-CN"]
        return self.description or ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "description_i18n": dict(self.description_i18n),
            "author": self.author,
            "runtime": self.runtime,
            "entry": self.entry,
            "sha256": self.sha256,
            "depends_on_mcp": [d.to_dict() for d in self.depends_on_mcp],
            "depends_on_llm": [d.to_dict() for d in self.depends_on_llm],
            "inputs": [i.to_dict() for i in self.inputs],
            "outputs": list(self.outputs),
            "triggers": list(self.triggers),
            "sensitive": self.sensitive,
            "sensitive_reason": self.sensitive_reason,
            "allowed_env_keys": list(self.allowed_env_keys),
            "allowed_http_hosts": list(self.allowed_http_hosts),
        }


class SkillStatus:
    READY = "ready"
    NEEDS_DEPENDENCIES = "needs_dependencies"
    ERROR = "error"
    DISABLED = "disabled"


@dataclass
class SkillInstall:
    """已安装到节点上的 skill 实例。"""
    id: str = ""              # = manifest.id
    manifest: SkillManifest = field(default_factory=SkillManifest)
    install_dir: str = ""     # 文件系统路径
    status: str = SkillStatus.READY
    status_reason: str = ""
    installed_at: float = field(default_factory=time.time)
    installed_by: str = ""
    enabled: bool = True
    granted_to: list[str] = field(default_factory=list)  # agent_id 列表
    invocation_count: int = 0
    last_invoked_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "manifest": self.manifest.to_dict(),
            "install_dir": self.install_dir,
            "status": self.status,
            "status_reason": self.status_reason,
            "installed_at": self.installed_at,
            "installed_by": self.installed_by,
            "enabled": self.enabled,
            "granted_to": list(self.granted_to),
            "invocation_count": self.invocation_count,
            "last_invoked_at": self.last_invoked_at,
        }


# ─────────────────────────────────────────────────────────────
# Manifest 解析与校验
# ─────────────────────────────────────────────────────────────

class ManifestError(Exception):
    pass


def _parse_description(raw_desc) -> tuple[str, dict]:
    """description 字段可以是 str 或 dict { locale: text }。"""
    if isinstance(raw_desc, dict):
        i18n = {k: str(v) for k, v in raw_desc.items()}
        primary = i18n.get("zh-CN") or next(iter(i18n.values()), "")
        return primary, i18n
    return str(raw_desc or ""), {}


def parse_manifest(manifest_dict: dict, install_dir: str = "") -> SkillManifest:
    """从 dict 构造 SkillManifest，做完整校验。"""
    if not isinstance(manifest_dict, dict):
        raise ManifestError(t("skills.errors.manifest_invalid", reason="not a dict"))

    for fname in REQUIRED_MANIFEST_FIELDS:
        if not manifest_dict.get(fname):
            raise ManifestError(t("skills.errors.manifest_missing_field", field=fname))

    runtime = manifest_dict.get("runtime", "python")
    if runtime not in SUPPORTED_RUNTIMES:
        raise ManifestError(
            t("skills.errors.manifest_unsupported_runtime", runtime=runtime))

    desc_str, desc_i18n = _parse_description(manifest_dict.get("description"))

    deps = manifest_dict.get("depends_on", {}) or {}
    if isinstance(deps, str):
        deps = {}
    raw_mcp = deps.get("mcp", []) or []
    if isinstance(raw_mcp, str):
        raw_mcp = [raw_mcp]
    mcp_deps = []
    for d in raw_mcp:
        if isinstance(d, str):
            mcp_deps.append(MCPDependency(id=d))
        elif isinstance(d, dict):
            mcp_deps.append(MCPDependency.from_dict(d))
    raw_llm = deps.get("llm", []) or []
    if isinstance(raw_llm, str):
        raw_llm = [raw_llm]
    llm_deps = []
    for d in raw_llm:
        if isinstance(d, str):
            llm_deps.append(LLMDependency(capability=d))
        elif isinstance(d, dict):
            llm_deps.append(LLMDependency.from_dict(d))
    # 显式拒绝 skill 间依赖（即使 manifest 声明了也忽略并警告）
    if deps.get("skills"):
        logger.warning("Skill manifest declared depends_on.skills which is not allowed; ignoring")

    raw_inputs = manifest_dict.get("inputs", []) or []
    inputs = []
    for i in raw_inputs:
        if isinstance(i, dict):
            inputs.append(SkillInput.from_dict(i))
        elif isinstance(i, str):
            inputs.append(SkillInput(name=i))
    outputs = list(manifest_dict.get("outputs", []) or [])
    triggers = list(manifest_dict.get("triggers", []) or [])

    hint = manifest_dict.get("hint", {}) or {}
    if isinstance(hint, str):
        hint = {}

    # env_vars 支持两种写法：
    #   env_vars: [VOLC_ACCESSKEY, VOLC_SECRETKEY]
    #   env_vars: [{name: VOLC_ACCESSKEY, required: true}, ...]
    raw_env = manifest_dict.get("env_vars") or manifest_dict.get("allowed_env_keys") or []
    allowed_env: list[str] = []
    for item in raw_env:
        if isinstance(item, str):
            if item.strip():
                allowed_env.append(item.strip())
        elif isinstance(item, dict):
            n = item.get("name") or item.get("key")
            if n and str(n).strip():
                allowed_env.append(str(n).strip())

    # http 出站白名单：allowed_http_hosts: [visual.volcengineapi.com, ...]
    raw_hosts = manifest_dict.get("allowed_http_hosts") or []
    allowed_hosts = [str(h).strip() for h in raw_hosts if str(h).strip()]

    name = manifest_dict["name"]
    version = manifest_dict.get("version", "0.0.0")
    sid = manifest_dict.get("id") or f"{name}@{version}"

    sha = ""
    if install_dir:
        sha = _compute_dir_hash(install_dir)

    return SkillManifest(
        id=sid,
        name=name,
        version=version,
        description=desc_str,
        description_i18n=desc_i18n,
        author=manifest_dict.get("author", ""),
        runtime=runtime,
        entry=manifest_dict.get("entry", "main.py"),
        sha256=sha,
        depends_on_mcp=mcp_deps,
        depends_on_llm=llm_deps,
        inputs=inputs,
        outputs=outputs,
        triggers=triggers,
        sensitive=bool(hint.get("sensitive", False)),
        sensitive_reason=hint.get("reason", ""),
        allowed_env_keys=allowed_env,
        allowed_http_hosts=allowed_hosts,
        raw=manifest_dict,
    )


def parse_manifest_file(manifest_path: str) -> SkillManifest:
    if yaml is None:
        raise ManifestError("PyYAML not installed")
    p = Path(manifest_path)
    if not p.exists():
        raise ManifestError(t("skills.errors.manifest_invalid",
                              reason=f"file not found: {manifest_path}"))
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return parse_manifest(data, install_dir=str(p.parent))


# ─────────────────────────────────────────────────────────────
# SKILL.md frontmatter → synthesized manifest
# ─────────────────────────────────────────────────────────────
# Anthropic Agent Skills spec (https://agentskills.io) skills only carry
# SKILL.md + YAML frontmatter — no separate manifest.yaml. To install
# such skills (e.g. fetched from ClawHub), we synthesize a minimal
# manifest dict from the frontmatter so the rest of the install pipeline
# (leak check, dependency check, copy-to-install_root) keeps working.

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _read_skill_md_frontmatter(skill_md_path: str) -> dict:
    """Parse YAML frontmatter from SKILL.md. Returns {} when absent or
    malformed. Body is discarded — install only needs the metadata."""
    if yaml is None:
        return {}
    p = Path(skill_md_path)
    if not p.exists():
        return {}
    try:
        text = p.read_text(encoding="utf-8")
    except Exception:
        return {}
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    try:
        data = yaml.safe_load(m.group(1)) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _extract_skill_md_summary(skill_md_path: str) -> tuple[str, str]:
    """Best-effort extraction of (name, description) from a SKILL.md
    that has NO YAML frontmatter — used as a fallback so external
    skills (Anthropic spec, ClawHub imports) without frontmatter
    still register cleanly.

    Heuristics:
      - name: first H1 heading (``# Foo``) → otherwise empty (caller
        falls back to the install dir name).
      - description: first non-empty / non-heading paragraph below
        the H1, capped at 240 chars. If absent, return "" — caller
        still bails on empty description, but the error message will
        explain "add a `# Title` line and a one-line description".
    """
    p = Path(skill_md_path)
    if not p.exists():
        return ("", "")
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ("", "")
    name = ""
    desc = ""
    lines = text.splitlines()
    # First H1
    for ln in lines:
        s = ln.strip()
        if s.startswith("# ") and not s.startswith("##"):
            name = s.lstrip("#").strip()
            break
    # First plain paragraph after the H1
    saw_title = False
    buf: list[str] = []
    for ln in lines:
        s = ln.rstrip()
        if not saw_title:
            if s.strip().startswith("# ") and not s.strip().startswith("##"):
                saw_title = True
            continue
        # Skip subsequent headings / blank lines until we hit prose
        if not s.strip():
            if buf:
                break
            continue
        if s.lstrip().startswith("#"):
            if buf:
                break
            continue
        buf.append(s.strip())
        if len(" ".join(buf)) >= 240:
            break
    if buf:
        desc = " ".join(buf)[:240]
    return (name, desc)


def synthesize_manifest_from_skill_md(
    skill_dir: str,
    *,
    fallback_author: str = "community",
) -> SkillManifest:
    """Build a SkillManifest for an Anthropic-spec skill (SKILL.md only,
    no manifest.yaml).

    Mapping:
      name        ← frontmatter.slug | frontmatter.name | dir name
      version     ← frontmatter.version | metadata.version | "1.0.0"
      description ← frontmatter.description (str or i18n dict)
      runtime     ← "markdown" (always — these are instruction-only skills)
      entry       ← "SKILL.md"
      author      ← metadata.author | frontmatter.author | fallback_author

    Raises ``ManifestError`` if SKILL.md missing or has no name/description.
    """
    src = Path(skill_dir)
    skill_md = src / "SKILL.md"
    if not skill_md.exists():
        # tolerate case variation
        alt = [p for p in src.iterdir() if p.is_file() and p.name.lower() == "skill.md"]
        if not alt:
            raise ManifestError(
                f"no manifest.yaml and no SKILL.md in {src}"
            )
        skill_md = alt[0]

    fm = _read_skill_md_frontmatter(str(skill_md))
    # No frontmatter? Fall back to extracting (name, description) from
    # the first H1 + first paragraph in the markdown body. Many
    # community skills (esp. Anthropic-spec / ClawHub imports) are
    # plain markdown without frontmatter — supporting that path means
    # one less "fix your skill" round-trip for the user.
    md_name, md_desc = ("", "")
    if not fm:
        md_name, md_desc = _extract_skill_md_summary(str(skill_md))

    metadata = fm.get("metadata") if isinstance(fm.get("metadata"), dict) else {}

    # Prefer slug for the install id (matches dir-name-safe convention).
    # Fall back to raw name (may contain spaces/uppercase — install_root
    # path sanitizer handles it via .replace), then to body H1, then dir.
    name = str(
        fm.get("slug") or fm.get("name") or md_name or src.name or ""
    ).strip()
    if not name:
        raise ManifestError(
            f"SKILL.md in {src} has no name — add YAML frontmatter "
            f"`name: my-skill` or a `# My Skill` heading at the top."
        )

    description = fm.get("description") or md_desc or ""
    if not description:
        raise ManifestError(
            f"SKILL.md in {src} has no description — add YAML frontmatter "
            f"`description: ...` or a paragraph after the `# Title` line."
        )

    version = str(
        fm.get("version") or metadata.get("version") or "1.0.0"
    ).strip()

    author = str(
        metadata.get("author") or fm.get("author") or fallback_author
    ).strip() or fallback_author

    synthesized = {
        "name": name,
        "version": version,
        "description": description,  # may be str or dict; parse_manifest handles both
        "runtime": "markdown",
        "entry": skill_md.name,
        "author": author,
        # Preserve original frontmatter so downstream can introspect e.g.
        # frontmatter.metadata.applicable_roles without re-reading the file.
        "_synthesized_from": "SKILL.md",
        "_frontmatter": fm,
    }
    return parse_manifest(synthesized, install_dir=str(src))


def _compute_dir_hash(dir_path: str) -> str:
    """计算目录内所有文件的 sha256（用于完整性校验）。"""
    h = hashlib.sha256()
    p = Path(dir_path)
    if not p.exists():
        return ""
    for fp in sorted(p.rglob("*")):
        if fp.is_file() and not fp.name.startswith("."):
            h.update(fp.name.encode())
            try:
                h.update(fp.read_bytes())
            except Exception:
                pass
    return h.hexdigest()


# ─────────────────────────────────────────────────────────────
# 静态代码校验（严格派沙箱第一层）
# ─────────────────────────────────────────────────────────────

class CodeValidationError(Exception):
    pass


def validate_python_skill(entry_path: str) -> None:
    """
    用 AST 检查 python skill 代码:

    - 禁止 import FORBIDDEN_IMPORTS 中的模块（顶层或完整点号名匹配）
    - ``from X import Y`` 会把 ``X.Y`` 也走一遍黑名单（堵
      ``from urllib import request`` 这种后门）
    - 必须有 def run(ctx, ...) 函数
    - 不允许 exec / eval / compile / __import__ / 裸 open()
    - 不允许硬编码敏感凭据（AK/SK/token/password 等）——必须通过
      ``os.environ`` 或者 ``ctx.env(...)`` 注入，见
      ``_check_no_hardcoded_credentials``
    """
    p = Path(entry_path)
    if not p.exists():
        raise CodeValidationError(f"entry file not found: {entry_path}")

    src = p.read_text(encoding="utf-8")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        raise CodeValidationError(f"syntax error: {e}")

    has_run = False
    # 跟踪 `from os import environ as E` / `import os as o` 的别名，
    # 下面检查属性访问时要把别名也当成 os 来校验。
    os_aliases: set[str] = {"os"}
    from_os_banned_names: set[str] = set()

    for node in ast.walk(tree):
        # 检查 import
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_import_name(alias.name)
                # 记录 import os as xxx
                if alias.name == "os" and alias.asname:
                    os_aliases.add(alias.asname)
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            _check_import_name(mod)
            # 也要检查 from X import Y 形式里每个 Y——它等价于 import X.Y
            for alias in node.names:
                if alias.name == "*":
                    continue
                if mod:
                    _check_import_name(f"{mod}.{alias.name}")
                else:
                    _check_import_name(alias.name)
                # from os import environ / getenv 要在属性级别同样拦下
                if mod == "os" and alias.name in FORBIDDEN_OS_ENV_ATTRS:
                    from_os_banned_names.add(alias.asname or alias.name)
        # 禁止 exec/eval/compile/__import__
        elif isinstance(node, ast.Call):
            fn = _call_func_name(node.func)
            if fn in ("exec", "eval", "compile", "__import__", "open"):
                raise CodeValidationError(
                    t("skills.errors.forbidden_io") + f" ({fn})")
            # 直接调用 getenv("X") / environ() 之类
            if isinstance(node.func, ast.Name) and node.func.id in from_os_banned_names:
                raise CodeValidationError(
                    t("skills.errors.forbidden_env_access",
                      name=node.func.id, line=getattr(node, "lineno", 0)))
        # os.environ / os.getenv 属性访问 (含 os.environ["X"] / os.getenv("X"))
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in os_aliases \
                    and node.attr in FORBIDDEN_OS_ENV_ATTRS:
                raise CodeValidationError(
                    t("skills.errors.forbidden_env_access",
                      name=f"{node.value.id}.{node.attr}",
                      line=getattr(node, "lineno", 0)))
        # 裸用 from_os_banned_names 里的名字（比如 environ["X"]）
        elif isinstance(node, ast.Name) and node.id in from_os_banned_names:
            # 只在它是 Load 上下文的真正引用时报错（赋值除外）
            if isinstance(getattr(node, "ctx", None), ast.Load):
                raise CodeValidationError(
                    t("skills.errors.forbidden_env_access",
                      name=node.id, line=getattr(node, "lineno", 0)))
        # 检查 run 函数
        if isinstance(node, ast.FunctionDef) and node.name == "run":
            has_run = True

    if not has_run:
        raise CodeValidationError("skill must define def run(ctx, ...)")

    # 凭据硬编码扫描（独立一次遍历，便于维护）
    _check_no_hardcoded_credentials(tree)


def _check_import_name(name: str) -> None:
    if not name:
        return
    top = name.split(".")[0]
    if top in FORBIDDEN_IMPORTS or name in FORBIDDEN_IMPORTS:
        raise CodeValidationError(
            t("skills.errors.forbidden_import", module=name))
    # 严格白名单模式（注释掉以保留灵活性，可通过 config 切换）
    # if top not in ALLOWED_IMPORTS:
    #     raise CodeValidationError(
    #         t("skills.errors.forbidden_import", module=name))


def _call_func_name(node) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


# ─────────────────────────────────────────────────────────────
# 硬编码凭据扫描
# ─────────────────────────────────────────────────────────────

# 名字匹配常见的凭据 key 模式，大小写不敏感。命中这些名字、且对应值是
# 非空字符串字面量时，禁止通过并要求走 env 注入。
#
# 匹配规则：只要名字里 **包含** 这些关键词之一即可命中。例如
# ``MY_API_KEY`` / ``aws_secret_access_key`` / ``bearer_token`` 都算。
_CREDENTIAL_KEYWORDS = (
    "password", "passwd", "pwd",
    "secret", "secret_key", "client_secret", "private_key",
    "api_key", "apikey", "api-key",
    "access_key", "accesskey", "access-key",
    "token", "auth_token", "bearer",
    "ak", "sk",   # 阿里云/火山/华为云常用缩写
    "session_token", "refresh_token",
    "credential", "credentials",
)

# 短字符串（长度 < 8）视为占位符/空值，不触发凭据告警；这避免把
# ``password = ""`` 这种空占位当成泄漏。
_CREDENTIAL_MIN_LEN = 8

# ``ak``/``sk`` 这种超短关键词只允许精确匹配，不做子串匹配——否则会误
# 伤 ``stack``、``mask`` 等正常变量名。
_CREDENTIAL_EXACT_ONLY = {"ak", "sk"}


def _is_credential_name(name: str) -> bool:
    """判断一个标识符名字是否看起来像凭据字段。"""
    if not name:
        return False
    low = name.lower()
    # 精确匹配优先（防止 mask/stack 被误判）
    if low in _CREDENTIAL_EXACT_ONLY:
        return True
    for kw in _CREDENTIAL_KEYWORDS:
        if kw in _CREDENTIAL_EXACT_ONLY:
            continue
        if kw in low:
            return True
    return False


def _is_nonempty_str_constant(node) -> bool:
    """AST 节点是否是长度 >= _CREDENTIAL_MIN_LEN 的字符串字面量。

    允许空字符串、短占位符（< 8 字符）、以及明显的 env 引用（如
    ``os.environ.get(...)``、``ctx.env(...)``）通过——后者会被识别
    成 ``ast.Call`` 而非 ``ast.Constant``，本函数直接返回 False。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return len(node.value.strip()) >= _CREDENTIAL_MIN_LEN
    return False


def _check_no_hardcoded_credentials(tree: ast.AST) -> None:
    """
    扫 AST，拒绝把明文凭据写进代码。

    命中三类写法都会报错：

    1. ``API_KEY = "sk-abcd1234"``              → ast.Assign + Name target
    2. ``client = Sendgrid(api_key="sk-xxx")``  → ast.Call keyword
    3. ``config = {"access_key": "AKIA..."}``   → ast.Dict 字符串 key

    正确的写法是通过 ``os.environ.get("API_KEY")`` 或者
    ``ctx.env("API_KEY")`` 从运行环境读取，由主进程在启动 worker /
    skill 时注入。
    """
    violations: list[tuple[str, int]] = []

    for node in ast.walk(tree):
        # 1) 直接赋值：X = "secret"
        if isinstance(node, ast.Assign) and _is_nonempty_str_constant(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name) and _is_credential_name(target.id):
                    violations.append((target.id, getattr(node, "lineno", 0)))
                elif isinstance(target, ast.Attribute) and _is_credential_name(target.attr):
                    violations.append((target.attr, getattr(node, "lineno", 0)))
        # 1b) 类型注解赋值：X: str = "secret"
        elif isinstance(node, ast.AnnAssign) and node.value is not None \
                and _is_nonempty_str_constant(node.value):
            target = node.target
            if isinstance(target, ast.Name) and _is_credential_name(target.id):
                violations.append((target.id, getattr(node, "lineno", 0)))
            elif isinstance(target, ast.Attribute) and _is_credential_name(target.attr):
                violations.append((target.attr, getattr(node, "lineno", 0)))
        # 2) 函数调用 kwargs：f(api_key="sk-xxx")
        elif isinstance(node, ast.Call):
            for kw in node.keywords or []:
                if kw.arg and _is_credential_name(kw.arg) \
                        and _is_nonempty_str_constant(kw.value):
                    violations.append((kw.arg, getattr(node, "lineno", 0)))
        # 3) Dict 字面量：{"api_key": "sk-xxx"}
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and isinstance(k.value, str) \
                        and _is_credential_name(k.value) \
                        and _is_nonempty_str_constant(v):
                    violations.append((k.value, getattr(node, "lineno", 0)))

    if violations:
        # 只抛第一个命中，减少刷屏
        name, line = violations[0]
        raise CodeValidationError(
            t("skills.errors.hardcoded_credential", name=name, line=line))


# ─────────────────────────────────────────────────────────────
# Skill 调用上下文 (ctx)
# ─────────────────────────────────────────────────────────────

class SkillContext:
    """
    传入 skill.run(ctx, ...) 的受控上下文对象。
    暴露的能力:
        ctx.mcp(id)       -> MCPProxy（调用已声明的 MCP）
        ctx.llm           -> LLMProxy（调用 LLM）
        ctx.log(msg)      -> 日志
        ctx.env(key)      -> 从 manifest.allowed_env_keys 白名单里取 env 值
        ctx.http          -> HttpProxy（受 manifest.allowed_http_hosts 限制）
    """
    def __init__(self, skill_id: str, agent_id: str, allowed_mcps: dict,
                 mcp_invoker: Callable, llm_invoker: Callable,
                 logger_fn: Callable, escalation_check: Callable | None = None,
                 env_values: dict | None = None,
                 http_invoker: Callable | None = None,
                 allowed_http_hosts: list[str] | None = None):
        self.skill_id = skill_id
        self.agent_id = agent_id
        self._allowed = allowed_mcps  # {mcp_id: [tool_names]}
        self._mcp_invoker = mcp_invoker
        self._llm_invoker = llm_invoker
        self._logger = logger_fn
        self._escalate = escalation_check
        # 主进程预先按 manifest.allowed_env_keys 筛过的 env 子集（冻结拷贝）
        self._env_values: dict = dict(env_values or {})
        self._http_invoker = http_invoker
        self._allowed_http_hosts: list[str] = list(allowed_http_hosts or [])

    def mcp(self, mcp_id: str) -> "MCPProxy":
        if mcp_id not in self._allowed:
            raise PermissionError(
                f"Skill {self.skill_id} not allowed to call MCP {mcp_id}")
        return MCPProxy(mcp_id, self._allowed[mcp_id],
                        self._mcp_invoker, self.skill_id, self.agent_id,
                        self._escalate)

    @property
    def llm(self) -> "LLMProxy":
        return LLMProxy(self._llm_invoker, self.skill_id, self.agent_id)

    def log(self, msg: str) -> None:
        self._logger(f"[skill={self.skill_id}] {msg}")

    def env(self, key: str, default: str = "") -> str:
        """
        从 manifest 声明的 allowed_env_keys 里取环境变量。
        白名单之外的 key 会抛 PermissionError——这是故意的，让 skill 作者
        意识到必须在 manifest.env_vars 里先声明，避免无声读到敏感变量。
        """
        if not isinstance(key, str) or not key:
            raise ValueError("ctx.env(key): key must be a non-empty string")
        if key not in self._env_values:
            # 白名单之外，拒绝
            raise PermissionError(
                f"Skill {self.skill_id} did not declare env var '{key}' in "
                f"manifest.env_vars; add it there to use ctx.env('{key}')")
        val = self._env_values.get(key, default)
        return val if val is not None else default

    @property
    def http(self) -> "HttpProxy":
        if self._http_invoker is None or not self._allowed_http_hosts:
            # 没注入 invoker / 没声明 host 就等于完全禁用
            return HttpProxy(None, [], self.skill_id, self.agent_id, self._logger)
        return HttpProxy(self._http_invoker, self._allowed_http_hosts,
                         self.skill_id, self.agent_id, self._logger)


class MCPProxy:
    def __init__(self, mcp_id: str, allowed_tools: list, invoker: Callable,
                 skill_id: str, agent_id: str, escalate: Callable | None):
        self._id = mcp_id
        self._tools = set(allowed_tools)
        self._invoker = invoker
        self._skill = skill_id
        self._agent = agent_id
        self._escalate = escalate

    def __getattr__(self, tool_name: str):
        if tool_name not in self._tools:
            raise PermissionError(
                f"Skill {self._skill} not allowed to call {self._id}.{tool_name}")
        def _call(**kwargs):
            # 运行时审批检查
            if self._escalate:
                decision = self._escalate(
                    skill_id=self._skill, agent_id=self._agent,
                    mcp_id=self._id, tool=tool_name, args=kwargs)
                if decision == "deny":
                    raise PermissionError(t("skills.errors.approval_denied"))
                if decision == "pending":
                    raise RuntimeError(t("skills.errors.approval_pending"))
            return self._invoker(self._id, tool_name, kwargs)
        return _call


class LLMProxy:
    def __init__(self, invoker: Callable, skill_id: str, agent_id: str):
        self._invoker = invoker
        self._skill = skill_id
        self._agent = agent_id

    def generate(self, prompt: str, model: str = "", **kwargs) -> str:
        return self._invoker(prompt=prompt, model=model,
                             skill_id=self._skill,
                             agent_id=self._agent, **kwargs)


class HttpProxy:
    """
    受控 HTTP 客户端。所有调用由主进程 http_invoker 实际发出，
    skill 不直接持有 socket。每次调用都会：

    1. 解析目标 URL 的 host
    2. 比对 manifest.allowed_http_hosts —— 精确 host 匹配，或 URL 前缀匹配
    3. 命中才转发给主进程 invoker，由主进程再做一次复核 + 审计日志

    两层校验的意义：skill 端的这层是"告警友好"（给 skill 作者看错误信息），
    主进程那层是"安全兜底"（即便 skill 侧被绕过，host 列表依然是主进程说了算）。
    """
    def __init__(self, invoker: Callable | None, allowed_hosts: list[str],
                 skill_id: str, agent_id: str, logger_fn: Callable):
        self._invoker = invoker
        self._allowed_hosts = list(allowed_hosts or [])
        self._skill = skill_id
        self._agent = agent_id
        self._log = logger_fn

    def _check(self, url: str) -> None:
        if self._invoker is None or not self._allowed_hosts:
            raise PermissionError(
                f"Skill {self._skill} has no HTTP capability "
                f"(declare allowed_http_hosts in manifest)")
        if not isinstance(url, str) or not url:
            raise ValueError("ctx.http: url must be a non-empty string")
        # 简单 host 解析（不 import urllib.parse 以免和校验器风格冲突）
        try:
            # scheme://host[:port]/path...
            scheme_end = url.find("://")
            rest = url[scheme_end + 3:] if scheme_end >= 0 else url
            host = rest.split("/", 1)[0].split("?", 1)[0]
            host_only = host.split(":", 1)[0]
        except Exception:
            raise ValueError(f"ctx.http: cannot parse host from url: {url}")

        for allowed in self._allowed_hosts:
            # 精确 host 匹配
            if allowed == host_only or allowed == host:
                return
            # 前缀匹配：如 "https://api.example.com/v1" 允许同前缀的 URL
            if allowed.startswith("http://") or allowed.startswith("https://"):
                if url.startswith(allowed):
                    return
        raise PermissionError(
            f"Skill {self._skill} not allowed to reach host '{host_only}'; "
            f"allowed hosts: {self._allowed_hosts}")

    def get(self, url: str, *, headers: dict | None = None,
            timeout: float = 30.0) -> dict:
        self._check(url)
        self._log(f"[skill={self._skill}] http.get {url}")
        return self._invoker(method="GET", url=url,
                             headers=dict(headers or {}),
                             body=None, timeout=timeout,
                             skill_id=self._skill, agent_id=self._agent)

    def post(self, url: str, *, body: Any = None,
             headers: dict | None = None, timeout: float = 30.0) -> dict:
        self._check(url)
        self._log(f"[skill={self._skill}] http.post {url}")
        return self._invoker(method="POST", url=url,
                             headers=dict(headers or {}),
                             body=body, timeout=timeout,
                             skill_id=self._skill, agent_id=self._agent)


# ─────────────────────────────────────────────────────────────
# Skill 执行器
# ─────────────────────────────────────────────────────────────

class SkillRunner:
    """
    执行已安装的 skill。

    当前实现: 同进程 import + ctx 注入 + 静态校验。
    后续可升级为 subprocess + import hook 强隔离。
    """
    def __init__(self, mcp_invoker: Callable, llm_invoker: Callable,
                 logger_fn: Callable | None = None,
                 escalation_check: Callable | None = None,
                 env_resolver: Callable | None = None,
                 http_invoker: Callable | None = None):
        self._mcp_invoker = mcp_invoker
        self._llm_invoker = llm_invoker
        self._logger = logger_fn or (lambda m: logger.info(m))
        self._escalate = escalation_check
        # env_resolver(skill_id, agent_id, allowed_keys) -> dict[key, value]
        # 主进程负责按 skill 声明的白名单把 env 子集装进来。默认实现
        # 直接 os.environ 查，生产环境 hub 应当用 agent profile 的 env 覆盖。
        self._env_resolver = env_resolver or _default_env_resolver
        # http_invoker(method, url, headers, body, timeout, skill_id, agent_id) -> dict
        self._http_invoker = http_invoker
        self._cache: dict[str, Any] = {}

    def run(self, install: SkillInstall, agent_id: str,
            inputs: dict) -> Any:
        manifest = install.manifest
        if install.status != SkillStatus.READY:
            raise RuntimeError(
                t("skills.errors.runtime_error",
                  error=f"status={install.status}: {install.status_reason}"))

        if manifest.runtime == "python":
            return self._run_python(install, agent_id, inputs)
        if manifest.runtime == "shell":
            raise NotImplementedError("shell runtime not implemented yet")
        if manifest.runtime == "markdown":
            return self._run_markdown(install, agent_id, inputs)
        raise RuntimeError(
            t("skills.errors.manifest_unsupported_runtime",
              runtime=manifest.runtime))

    def _run_markdown(self, install: SkillInstall, agent_id: str,
                       inputs: dict) -> dict:
        """
        "Run" a markdown / Anthropic Agent Skills entry.

        These skills are prompt-injected, not executed. This method exists
        so that callers who ask for the skill body (e.g. the capability
        panel preview, or a "describe this skill" endpoint) get back a
        predictable dict without raising. Actual execution happens when
        the agent reads the injected instructions and uses its own tools.
        """
        entry_path = Path(install.install_dir) / (install.manifest.entry or "SKILL.md")
        try:
            body = entry_path.read_text(encoding="utf-8")
        except Exception as e:
            body = ""
            logger.warning("markdown skill %s: read SKILL.md failed: %s",
                           install.id, e)
        install.invocation_count += 1
        install.last_invoked_at = time.time()
        return {
            "ok": True,
            "runtime": "markdown",
            "mode": "guidance",
            "skill_id": install.id,
            "skill_name": install.manifest.name,
            "body": body,
            "install_dir": install.install_dir,
            "message": "markdown skills are prompt-injected; agent uses its "
                       "regular tools to follow SKILL.md instructions.",
        }

    def _run_python(self, install: SkillInstall, agent_id: str,
                     inputs: dict) -> Any:
        manifest = install.manifest
        entry = Path(install.install_dir) / manifest.entry
        validate_python_skill(str(entry))  # 每次执行前校验

        allowed_mcps = {dep.id: list(dep.tools)
                        for dep in manifest.depends_on_mcp}

        # 按 manifest 白名单从运行环境筛出允许的 env 子集。
        try:
            env_values = self._env_resolver(
                install.id, agent_id, list(manifest.allowed_env_keys))
        except Exception as e:
            logger.warning("env_resolver failed for %s: %s", install.id, e)
            env_values = {}

        ctx = SkillContext(
            skill_id=install.id, agent_id=agent_id,
            allowed_mcps=allowed_mcps,
            mcp_invoker=self._mcp_invoker,
            llm_invoker=self._llm_invoker,
            logger_fn=self._logger,
            escalation_check=self._escalate,
            env_values=env_values,
            http_invoker=self._http_invoker,
            allowed_http_hosts=list(manifest.allowed_http_hosts),
        )

        # 在受限的 globals 中加载模块
        module_globals: dict = {
            "__name__": f"skill_{install.id.replace('@','_').replace('.','_')}",
            "__builtins__": _safe_builtins(),
        }
        try:
            with open(entry, "r", encoding="utf-8") as f:
                code = compile(f.read(), str(entry), "exec")
            exec(code, module_globals)
        except Exception as e:
            raise RuntimeError(t("skills.errors.runtime_error", error=str(e)))

        run_fn = module_globals.get("run")
        if not callable(run_fn):
            raise RuntimeError("skill missing run() function")

        try:
            result = run_fn(ctx, **inputs)
        except Exception as e:
            raise RuntimeError(t("skills.errors.runtime_error", error=str(e)))

        install.invocation_count += 1
        install.last_invoked_at = time.time()
        return result


def _safe_builtins() -> dict:
    """构造受限的 builtins，剥离 IO 类函数。"""
    import builtins as _b
    blocked = {"open", "input", "exec", "eval", "compile", "__import__",
               "exit", "quit"}
    return {k: getattr(_b, k) for k in dir(_b)
            if not k.startswith("_") and k not in blocked}


def _default_env_resolver(skill_id: str, agent_id: str,
                           allowed_keys: list[str]) -> dict:
    """
    默认 env 解析器：按白名单从 os.environ 抄出对应 key。

    生产环境应当由 hub 注入一个自定义 resolver，从 agent profile
    或 secret store 里取值，而不是复用主进程的环境变量——那样会
    让所有 agent 共享同一套凭据。
    """
    result: dict = {}
    for k in allowed_keys or []:
        v = os.environ.get(k)
        if v is not None:
            result[k] = v
    return result


def make_stdlib_http_invoker():
    """
    返回一个最小的 http_invoker 实现（主进程侧）。使用 stdlib urllib，
    所以 skill 自己即使被禁 urllib.request 也没关系——真正的网络调用
    在主进程里执行，skill 看到的只是 JSON 结果。

    生产环境建议替换为带审计、限流、代理、TLS pinning 的实现。
    """
    import urllib.request  # noqa: E402 — intentional: main process only
    import urllib.error    # noqa: E402

    def _invoke(*, method: str, url: str, headers: dict, body,
                 timeout: float, skill_id: str, agent_id: str) -> dict:
        logger.info("[http] skill=%s agent=%s %s %s",
                    skill_id, agent_id, method, url)
        data_bytes: bytes | None = None
        if body is not None:
            if isinstance(body, (bytes, bytearray)):
                data_bytes = bytes(body)
            elif isinstance(body, str):
                data_bytes = body.encode("utf-8")
            else:
                # JSON default
                data_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data_bytes,
                                      headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                status = resp.status
                resp_headers = dict(resp.headers.items())
        except urllib.error.HTTPError as e:
            return {"ok": False, "status": e.code,
                    "error": str(e), "body": e.read().decode("utf-8", "replace")[:2000]}
        except Exception as e:
            return {"ok": False, "status": 0, "error": str(e), "body": ""}

        text = raw.decode("utf-8", errors="replace")
        parsed: Any = None
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        return {
            "ok": 200 <= status < 300,
            "status": status,
            "headers": resp_headers,
            "json": parsed,
            "text": text if parsed is None else "",
        }

    return _invoke


# ─────────────────────────────────────────────────────────────
# Skill Registry
# ─────────────────────────────────────────────────────────────

class SkillRegistry:
    """
    全局 skill 注册表。

    职责:
    - 维护 installed skills (id -> SkillInstall)
    - 持久化到 data/skills.json
    - 安装/卸载/grant/revoke
    - 依赖检查 (调用 hub.mcp_manager 验证 MCP 就绪)
    - 调用入口 (invoke)
    """

    def __init__(self, install_root: str, persist_path: str,
                 mcp_check: Callable | None = None,
                 mcp_invoker: Callable | None = None,
                 llm_invoker: Callable | None = None,
                 logger_fn: Callable | None = None,
                 escalation_check: Callable | None = None,
                 env_resolver: Callable | None = None,
                 http_invoker: Callable | None = None):
        self.install_root = Path(install_root)
        self.install_root.mkdir(parents=True, exist_ok=True)
        self.persist_path = Path(persist_path)
        self.persist_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._installs: dict[str, SkillInstall] = {}
        self._mcp_check = mcp_check or (lambda mcp_id, tools: (True, ""))
        self._mcp_invoker = mcp_invoker or self._default_mcp_invoker
        self._llm_invoker = llm_invoker or self._default_llm_invoker
        self._logger = logger_fn or (lambda m: logger.info(m))
        self._escalate = escalation_check
        self._env_resolver = env_resolver or _default_env_resolver
        # 默认 http_invoker 用 stdlib urllib 实现；hub 可以替换成带审计/代理的版本
        self._http_invoker = http_invoker or make_stdlib_http_invoker()
        self._runner = SkillRunner(
            mcp_invoker=self._mcp_invoker,
            llm_invoker=self._llm_invoker,
            logger_fn=self._logger,
            escalation_check=self._escalate,
            env_resolver=self._env_resolver,
            http_invoker=self._http_invoker,
        )
        self._load()

    # ── 持久化 ──

    def _load(self) -> None:
        if not self.persist_path.exists():
            return
        try:
            data = json.loads(self.persist_path.read_text(encoding="utf-8"))
            for sid, item in (data.get("installs") or {}).items():
                manifest_dict = item.get("manifest", {})
                try:
                    m = parse_manifest(manifest_dict.get("raw") or manifest_dict)
                except ManifestError:
                    # 老数据兼容
                    m = SkillManifest(
                        id=manifest_dict.get("id", sid),
                        name=manifest_dict.get("name", ""),
                        version=manifest_dict.get("version", "0.0.0"),
                        description=manifest_dict.get("description", ""),
                        runtime=manifest_dict.get("runtime", "python"),
                    )
                inst = SkillInstall(
                    id=item.get("id", sid),
                    manifest=m,
                    install_dir=item.get("install_dir", ""),
                    status=item.get("status", SkillStatus.READY),
                    status_reason=item.get("status_reason", ""),
                    installed_at=item.get("installed_at", time.time()),
                    installed_by=item.get("installed_by", ""),
                    enabled=item.get("enabled", True),
                    granted_to=list(item.get("granted_to", []) or []),
                    invocation_count=item.get("invocation_count", 0),
                    last_invoked_at=item.get("last_invoked_at", 0.0),
                )
                self._installs[sid] = inst
        except Exception as e:
            logger.error("Failed to load skills registry: %s", e)

    def _save(self) -> None:
        with self._lock:
            data = {
                "installs": {sid: inst.to_dict()
                              for sid, inst in self._installs.items()}
            }
            tmp = self.persist_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                            encoding="utf-8")
            tmp.replace(self.persist_path)

    # ── 默认 invokers (placeholder) ──

    def _default_mcp_invoker(self, mcp_id: str, tool: str, args: dict) -> dict:
        logger.info("[stub mcp] %s.%s(%s)", mcp_id, tool, args)
        return {"ok": True, "stub": True, "mcp": mcp_id, "tool": tool}

    def _default_llm_invoker(self, prompt: str, model: str = "", **kw) -> str:
        logger.info("[stub llm] generate prompt=%s", prompt[:60])
        return f"[stub llm response to: {prompt[:40]}]"

    # ── 安装 / 卸载 ──

    def install_from_directory(self, src_dir: str,
                                 installed_by: str = "") -> SkillInstall:
        src = Path(src_dir)
        manifest_path = src / "manifest.yaml"
        if manifest_path.exists():
            manifest = parse_manifest_file(str(manifest_path))
        else:
            # Anthropic Agent Skills spec: SKILL.md only, no manifest.yaml.
            # Synthesize a minimal manifest from frontmatter so the rest of
            # the install pipeline (leak check, dependency check, copy,
            # registry write) works unchanged.
            manifest = synthesize_manifest_from_skill_md(str(src))

        # ── Leak check on prompt-visible content ───────────────
        # manifest.description and SKILL.md / README.md content get
        # auto-injected into the system prompts of every granted agent.
        # A hardcoded API key / .env path / internal IP in any of these
        # leaks to every agent + the LLM provider they call. Block install
        # when the visible surface contains anything suspicious.
        try:
            from ..v2.core.guardrails import wiki_leak_guardrail
            scan_parts: list[str] = [manifest.description or ""]
            for md_name in ("SKILL.md", "README.md"):
                md_path = src / md_name
                if md_path.exists():
                    try:
                        scan_parts.append(
                            md_path.read_text(encoding="utf-8", errors="ignore")
                        )
                    except Exception:
                        pass
            scan_blob = "\n\n".join(p for p in scan_parts if p.strip())
            if scan_blob:
                out = wiki_leak_guardrail.run(scan_blob)
                if out.tripwire_triggered:
                    leak_report = out.output_info or {}
                    leaks = leak_report.get("leaks", [])
                    samples = ", ".join(
                        f"{l['type']}={l['value'][:30]}" for l in leaks[:3]
                    )
                    raise ManifestError(
                        f"skill install rejected — manifest description / "
                        f"SKILL.md / README.md contains {len(leaks)} "
                        f"potential leak(s) ({samples}). Replace hardcoded "
                        f"secrets/paths with env-var references and retry."
                    )
        except ManifestError:
            raise
        except Exception as _le:
            # Fail-open: leak detection MUST NOT break legitimate installs.
            self._logger(f"leak_check on skill install skipped: {_le}")

        # Detect agent-submitted skills (source: agent in manifest)
        _is_agent_skill = (manifest.raw or {}).get("source") == "agent"

        # 校验代码
        validation_warning = ""
        if manifest.runtime == "python":
            entry_path = src / manifest.entry
            try:
                validate_python_skill(str(entry_path))
            except CodeValidationError as e:
                if _is_agent_skill:
                    # Agent-submitted: warn but allow install
                    validation_warning = str(e)
                    self._logger(f"⚠ Agent skill {manifest.name} validation: {e}")
                else:
                    raise

        with self._lock:
            if manifest.id in self._installs:
                raise ValueError(t("skills.errors.already_installed",
                                    id=manifest.id))

            # 复制到 install_root
            target = self.install_root / manifest.id.replace("@", "_").replace("/", "_")
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(src, target)
            manifest.sha256 = _compute_dir_hash(str(target))

            inst = SkillInstall(
                id=manifest.id,
                manifest=manifest,
                install_dir=str(target),
                installed_by=installed_by,
            )
            self._check_dependencies(inst)
            if validation_warning:
                inst.status = SkillStatus.READY
                inst.status_reason = f"code validation: {validation_warning}"
            self._installs[manifest.id] = inst
            self._save()

        self._logger(t("skills.install_success", name=manifest.name))
        return inst

    # ── Auto-install version policy ──
    #
    # ``install_from_directory`` is strict: same manifest.id → hard
    # error. That's correct for user-initiated installs, but wrong for
    # builtin skill auto-install on hub boot, where the codebase's
    # bundled copy must be the authoritative version.
    #
    # This method implements the bundled-upgrade policy:
    #
    #   1. Parse the incoming manifest.
    #   2. If an install with the exact same manifest.id already
    #      exists, treat it as idempotent and return the existing
    #      SkillInstall. (Same version of same skill — nothing to do.)
    #   3. Otherwise look at any install that shares manifest.name
    #      (different version of the same skill). Compare versions
    #      with packaging semantics:
    #        - new > old  → uninstall old, install new, return new
    #        - new <= old → keep old, return old (no warning, this is
    #                       expected when the bundled copy is older
    #                       than a user-installed one)
    #        - not comparable → keep old, log a warning explaining
    #                           why we didn't touch it
    #   4. If nothing shares the name, fall through to the strict
    #      installer.
    #
    # Callers that just want the old behaviour can keep using
    # ``install_from_directory`` directly.
    def install_or_upgrade_from_directory(
        self,
        src_dir: str,
        installed_by: str = "",
        *,
        policy: str = "upgrade",
    ) -> "SkillInstall":
        src = Path(src_dir)
        manifest_path = src / "manifest.yaml"
        if manifest_path.exists():
            incoming = parse_manifest_file(str(manifest_path))
        else:
            incoming = synthesize_manifest_from_skill_md(str(src))

        with self._lock:
            # Same id already installed → idempotent no-op.
            existing_same_id = self._installs.get(incoming.id)
            if existing_same_id is not None:
                return existing_same_id

            # Same name, different version → policy decides.
            same_name = [
                i for i in self._installs.values()
                if i.manifest.name == incoming.name
            ]

        if not same_name:
            return self.install_from_directory(src_dir, installed_by)

        if policy != "upgrade":
            # Future: "reject" / "pin". For now every caller passes
            # "upgrade", so any other value is treated as "no-op".
            self._logger(
                f"[skill] {incoming.name}: policy={policy}, keeping "
                f"existing {same_name[0].id}")
            return same_name[0]

        # Pick the "best" existing candidate (highest version) as the
        # one to compare against.
        def _vkey(inst):
            try:
                from packaging.version import Version
                return (0, Version(inst.manifest.version or "0"))
            except Exception:
                return (1, inst.manifest.version or "")

        same_name.sort(key=_vkey, reverse=True)
        incumbent = same_name[0]

        try:
            from packaging.version import Version, InvalidVersion
            v_new = Version(incoming.version or "0")
            v_old = Version(incumbent.manifest.version or "0")
            comparable = True
        except Exception:
            v_new = v_old = None
            comparable = False

        if not comparable:
            self._logger(
                f"[skill] {incoming.name}: cannot compare versions "
                f"(incoming={incoming.version!r} existing="
                f"{incumbent.manifest.version!r}); keeping "
                f"{incumbent.id}")
            return incumbent

        if v_new <= v_old:
            # Existing wins — nothing to do. Silent on equal so boot
            # stays quiet; info on downgrade so operators can notice.
            if v_new < v_old:
                self._logger(
                    f"[skill] {incoming.name}: bundled version "
                    f"{incoming.version} older than installed "
                    f"{incumbent.manifest.version}, keeping installed")
            return incumbent

        # Upgrade path: remove ALL older installs sharing this name,
        # then install the new one.
        for stale in same_name:
            try:
                self.uninstall(stale.id)
            except Exception as _e:  # best-effort; keep going
                logger.warning(
                    "skill upgrade: failed to uninstall %s: %s",
                    stale.id, _e)
        new_inst = self.install_from_directory(src_dir, installed_by)
        self._logger(
            f"[skill] {incoming.name} upgraded "
            f"{incumbent.manifest.version} → {incoming.version}")
        return new_inst

    def uninstall(self, skill_id: str) -> bool:
        with self._lock:
            inst = self._installs.pop(skill_id, None)
            if not inst:
                return False
            try:
                if inst.install_dir and Path(inst.install_dir).exists():
                    shutil.rmtree(inst.install_dir)
            except Exception as e:
                logger.warning("Failed to remove skill dir: %s", e)
            self._save()
        self._logger(t("skills.uninstall_success", name=inst.manifest.name))
        return True

    # ── 依赖检查 ──

    def _check_dependencies(self, inst: SkillInstall) -> None:
        missing = []
        for dep in inst.manifest.depends_on_mcp:
            if dep.optional:
                continue
            ok, reason = self._mcp_check(dep.id, dep.tools)
            if not ok:
                missing.append(f"{dep.id} ({reason})")
        if missing:
            inst.status = SkillStatus.NEEDS_DEPENDENCIES
            inst.status_reason = "; ".join(missing)
        else:
            inst.status = SkillStatus.READY
            inst.status_reason = ""

    def recheck_all(self) -> None:
        with self._lock:
            for inst in self._installs.values():
                self._check_dependencies(inst)
            self._save()

    # ── Grant / Revoke ──

    def grant(self, skill_id: str, agent_id: str) -> bool:
        """Grant skill to agent + auto-sync latest files into agent workspace.

        Every grant is idempotent and a no-op at the grant-record level if
        already granted, BUT it always re-runs the workspace sync so the
        agent's local copy of skill files (SKILL.md, .py helpers, assets)
        stays aligned with the source install_dir. This means re-granting
        an already-granted skill acts as a "refresh / pull latest" button.
        """
        with self._lock:
            inst = self._installs.get(skill_id)
            if not inst:
                raise KeyError(t("skills.errors.not_found", id=skill_id))
            if agent_id not in inst.granted_to:
                inst.granted_to.append(agent_id)
            self._save()
        # Sync files to agent's workspace. Best-effort — if sync fails
        # (permissions, missing agent object, I/O), the grant record
        # still stands and bash PYTHONPATH fallback keeps imports working.
        self._sync_to_agent_workspace(inst, agent_id)
        self._logger(t("skills.granted_success", skill=inst.manifest.name,
                       agent=agent_id))
        return True

    def _sync_to_agent_workspace(self, inst, agent_id: str) -> None:
        """Copy skill install_dir into <agent_workspace>/skills/<name>/.

        Resolves the live agent object via the active Hub and calls its
        sync_skill_to_workspace method. Silent no-op if we can't find
        the agent (registry running before hub is up, or agent was
        deleted between grant & sync). All errors are logged at debug
        and swallowed — grant semantics don't depend on sync success.
        """
        try:
            import sys as _sys
            llm_mod = _sys.modules.get("app.llm")
            hub = getattr(llm_mod, "_active_hub", None) if llm_mod else None
            if hub is None or not hasattr(hub, "get_agent"):
                return
            agent = hub.get_agent(agent_id)
            if agent is None or not hasattr(agent, "sync_skill_to_workspace"):
                return
            result = agent.sync_skill_to_workspace(inst)
            if not result.get("ok"):
                logger.debug(
                    "skill sync %s → %s failed: %s",
                    getattr(inst.manifest, "name", "?"), agent_id,
                    result.get("error", "?"))
        except Exception as e:
            logger.debug(
                "skill sync %s → %s exception: %s",
                getattr(getattr(inst, "manifest", None), "name", "?"),
                agent_id, e)

    def resync_all_granted_skills(self) -> dict:
        """Walk every (skill, agent) grant and re-sync the skill into the
        agent's workspace. Safe to call at hub startup — no-op for grants
        whose agent is already up-to-date (shutil.copytree over existing
        tree is how sync_skill_to_workspace works: it `rmtree` + fresh copy).

        Returns summary {total: N, synced: N, errors: [...]}.
        """
        total = 0
        synced = 0
        errors: list[str] = []
        with self._lock:
            grants = [(inst, aid) for inst in self._installs.values()
                      for aid in list(inst.granted_to)]
        for inst, aid in grants:
            total += 1
            try:
                self._sync_to_agent_workspace(inst, aid)
                synced += 1
            except Exception as e:  # noqa: BLE001
                errors.append(
                    f"{getattr(inst.manifest, 'name', '?')} → {aid}: {e}")
        if total:
            self._logger(
                f"skill resync: {synced}/{total} granted skills synced "
                f"to agent workspaces ({len(errors)} errors)")
        return {"total": total, "synced": synced, "errors": errors}

    def revoke(self, skill_id: str, agent_id: str) -> bool:
        with self._lock:
            inst = self._installs.get(skill_id)
            if not inst:
                return False
            if agent_id in inst.granted_to:
                inst.granted_to.remove(agent_id)
            self._save()
        self._logger(t("skills.revoked_success", skill=inst.manifest.name,
                       agent=agent_id))
        return True

    def list_for_agent(self, agent_id: str) -> list[SkillInstall]:
        with self._lock:
            return [i for i in self._installs.values()
                    if agent_id in i.granted_to and i.enabled]

    def list_all(self) -> list[SkillInstall]:
        with self._lock:
            return list(self._installs.values())

    def get(self, skill_id: str) -> SkillInstall | None:
        return self._installs.get(skill_id)

    # ── 调用 ──

    def invoke(self, skill_id: str, agent_id: str, inputs: dict) -> Any:
        inst = self._installs.get(skill_id)
        if not inst:
            raise KeyError(t("skills.errors.not_found", id=skill_id))
        if agent_id not in inst.granted_to:
            raise PermissionError(t("skills.errors.not_granted",
                                     agent_id=agent_id, skill_id=skill_id))
        if not inst.enabled:
            raise RuntimeError(t("skills.status.disabled"))
        result = self._runner.run(inst, agent_id, inputs)
        with self._lock:
            self._save()
        return result

    # ── 配置 invokers (hub 启动时注入真实实现) ──

    def configure_invokers(self, mcp_invoker: Callable | None = None,
                            llm_invoker: Callable | None = None,
                            mcp_check: Callable | None = None,
                            escalation_check: Callable | None = None,
                            logger_fn: Callable | None = None) -> None:
        if mcp_invoker:
            self._mcp_invoker = mcp_invoker
        if llm_invoker:
            self._llm_invoker = llm_invoker
        if mcp_check:
            self._mcp_check = mcp_check
        if escalation_check:
            self._escalate = escalation_check
        if logger_fn:
            self._logger = logger_fn
        # 重建 runner
        self._runner = SkillRunner(
            mcp_invoker=self._mcp_invoker,
            llm_invoker=self._llm_invoker,
            logger_fn=self._logger,
            escalation_check=self._escalate,
        )

    # ── Prompt 注入辅助 ──

    def build_prompt_block(self, agent_id: str, locale: str = "zh-CN",
                           agent_workspace: str = "") -> str:
        """生成注入 agent system prompt 的技能描述块。

        只注入**摘要** (名字 + 一句话描述 + skill_dir)，不灌全文，节省 token。

        对于 ``runtime=markdown`` 的技能，额外告知 agent：
          - ``skill_dir`` — 技能包在 agent workspace 下的本地目录
          - 提示 agent 可调用 ``get_skill_guide(name)`` 工具拉取完整指南

        如果 skill_store 可用，会把该 skill 的 annotations (本地笔记) 自动
        附加在描述后面 (annotation-on-fetch)。

        Args:
            agent_id: The agent to build the prompt for.
            locale: Output locale.
            agent_workspace: If provided, prefer agent-local skill path
                ``{agent_workspace}/skills/{name}/`` over global install_dir.
        """
        granted = self.list_for_agent(agent_id)
        if not granted:
            return ""
        # Lazy import to avoid circular
        store = None
        try:
            from .store import get_store as _get_store
            store = _get_store()
        except Exception:
            store = None

        lines = [t("skills.prompt_injection_header", locale=locale) + ":"]
        has_markdown = False

        for inst in granted:
            m = inst.manifest
            triggers = ", ".join(m.triggers) if m.triggers else "-"

            # Determine skill_dir: prefer agent-local copy, fall back to global
            if agent_workspace:
                local_dir = Path(agent_workspace) / "skills" / m.name
                skill_dir = str(local_dir) if local_dir.is_dir() else inst.install_dir
            else:
                skill_dir = inst.install_dir

            # For markdown skills, append skill_dir so agent knows where scripts live
            if m.runtime == "markdown":
                has_markdown = True
                lines.append(t("skills.prompt_injection_item", locale=locale,
                                name=m.name,
                                description=m.get_description(locale),
                                triggers=triggers))
                lines.append(f"    skill_dir: {skill_dir}")
            else:
                lines.append(t("skills.prompt_injection_item", locale=locale,
                                name=m.name,
                                description=m.get_description(locale),
                                triggers=triggers))

            # annotation-on-fetch: look up by both installed id and author/name form
            if store is not None:
                try:
                    ann_text = store.build_annotation_block(inst.id, locale=locale)
                    if not ann_text:
                        alt_id = f"{m.author}/{m.name}" if m.author else ""
                        if alt_id:
                            ann_text = store.build_annotation_block(alt_id, locale=locale)
                    if ann_text:
                        lines.append(ann_text)
                except Exception:
                    pass

        if has_markdown:
            lines.append("")
            lines.append("提示：对于 markdown 类技能，使用 get_skill_guide(name) 工具获取完整操作指南。")
            lines.append("运行技能脚本时，先 cd 到 skill_dir 再执行，例: cd <skill_dir> && python scripts/xxx.py")
        return "\n".join(lines)

    @staticmethod
    def _read_markdown_skill_body(inst: "SkillInstall") -> str:
        """Read the SKILL.md body (minus frontmatter) for a markdown-runtime
        skill. Returns empty string on any failure."""
        entry_file = inst.manifest.entry or "SKILL.md"
        p = Path(inst.install_dir) / entry_file
        if not p.exists():
            return ""
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            return ""
        # Strip YAML frontmatter — we already showed name+description
        import re as _re
        fm = _re.match(r"^---\s*\n.*?\n---\s*\n?", text, _re.DOTALL)
        if fm:
            return text[fm.end():]
        return text


# ─────────────────────────────────────────────────────────────
# 模块级辅助
# ─────────────────────────────────────────────────────────────

_GLOBAL_REGISTRY: SkillRegistry | None = None


def init_registry(install_root: str, persist_path: str,
                   **kwargs) -> SkillRegistry:
    global _GLOBAL_REGISTRY
    _GLOBAL_REGISTRY = SkillRegistry(install_root, persist_path, **kwargs)
    return _GLOBAL_REGISTRY


def get_registry() -> SkillRegistry | None:
    return _GLOBAL_REGISTRY
