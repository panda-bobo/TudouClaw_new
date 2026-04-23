"""skill-converter 共享 helpers — 把任意 URL 上的 skill 文档转成 TudouClaw skill 包。

## 典型用法（agent 脚本）

```python
from _skill_converter import *

# 1) 抓取 + 清洗 URL 内容
extracted = extract_source("https://clawhub.ai/matrixy/agent-browser-clawdbot")
# extracted.source_url / source_type / title / description_hint / body_markdown

# 2) Agent 基于 CONVERSION_PROMPT 和 extracted.body_markdown 用**自己的 LLM**
#    推理生成 TudouClaw 格式的 skill_md + helpers_py (python 代码字符串)
#    —— 这一步是 agent 的主任务，不是这个 skill 做的
skill_md  = "...你生成的 SKILL.md 内容..."
helpers_py = "...你生成的 _xxx_helpers.py 内容... 或 None"
deps      = ["beautifulsoup4>=4.12"]  # pip deps 列表

# 3) 校验 + 写入
problems = validate_skill_md(skill_md)
if problems:
    raise ValueError("SKILL.md 不合规: " + "; ".join(problems))

paths = write_skill_package(
    skill_name="agent-browser",
    skill_md=skill_md,
    helpers_py=helpers_py,
    deps=deps,
    source_url=extracted.source_url,
    # output_dir 默认 <workspace>/generated_skills/<skill_name>/
)
print("skill written to:", paths)
```

## 支持的 URL 类型

- **ClawHub**: `https://clawhub.ai/<user>/<skill>`
- **GitHub README**: `https://github.com/<user>/<repo>` / `/blob/main/README.md`
- **GitHub 子目录**: `https://github.com/<user>/<repo>/tree/main/skills/pdf`
- **raw markdown**: `https://raw.githubusercontent.com/.../README.md`
- **通用 HTML 页**: 其他任何返回 HTML 的 URL（用 readability 抽主文）

## 关键设计

- **Agent 做转换的"大脑"**：fetch + 清洗 + 写盘由 Python helper 做；
  从 markdown → SKILL.md + helpers.py 的**语义映射**由 agent 自己用 LLM 推理，
  `CONVERSION_PROMPT` 是 agent 该读的指令模板
- **绝不自动安装到 builtin**：转换结果先落到 `generated_skills/`，管理员审核后再人工搬
- **源链接留痕**：SKILL.md metadata 加 `source_url`，便于追溯 + 批量更新
"""
from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "ExtractResult",
    "extract_source",
    "fetch_url",
    "html_to_markdown",
    "detect_source_type",
    "CONVERSION_PROMPT",
    "validate_skill_md",
    "write_skill_package",
    "preview_package",
    "list_generated_skills",
    "ConversionError",
]


class ConversionError(RuntimeError):
    """Raised when a URL can't be fetched or parsed into something usable."""


# ── data class ──────────────────────────────────────────────────────


@dataclass
class ExtractResult:
    source_url: str          # 原始 URL
    source_type: str         # clawhub | github_repo | github_folder | raw_md | html_generic
    title: str = ""          # 页面标题或 skill 名
    description_hint: str = ""  # 描述候选（agent 可能重写）
    body_markdown: str = ""  # 清洗后的 markdown 主体
    raw_bytes: int = 0       # 抓取字节数

    def to_dict(self) -> dict:
        return {
            "source_url": self.source_url,
            "source_type": self.source_type,
            "title": self.title,
            "description_hint": self.description_hint,
            "body_markdown": self.body_markdown,
            "raw_bytes": self.raw_bytes,
        }


# ── fetch ───────────────────────────────────────────────────────────


def fetch_url(url: str, timeout: int = 30) -> tuple[str, str]:
    """Fetch URL, return (content_text, content_type). Plain requests, no
    JS rendering — if a page requires JS, you'll get the SPA shell only.
    Caller should detect that (short body / no headings) and bail."""
    try:
        import requests
    except ImportError as e:
        raise ConversionError(f"requests not installed: {e}") from e

    headers = {
        "User-Agent": "TudouClaw-SkillConverter/1.0 (+https://github.com/panda-bobo/TudouClaw_new)",
        "Accept": "text/html,application/xhtml+xml,text/markdown,text/plain,*/*",
    }
    resp = requests.get(url, headers=headers, timeout=timeout,
                        allow_redirects=True)
    if resp.status_code >= 400:
        raise ConversionError(f"HTTP {resp.status_code} fetching {url}")
    return resp.text, resp.headers.get("Content-Type", "")


# ── detect ──────────────────────────────────────────────────────────


_GITHUB_RE = re.compile(r"https?://github\.com/[^/]+/[^/]+/?(tree|blob)?(/[^?#]*)?")


def detect_source_type(url: str, content: str = "",
                        content_type: str = "") -> str:
    u = url.lower()
    if "clawhub.ai/" in u:
        return "clawhub"
    if "github.com/" in u:
        m = _GITHUB_RE.match(url)
        if m and m.group(1) == "tree":
            return "github_folder"
        return "github_repo"
    if u.endswith(".md") or "text/markdown" in content_type.lower():
        return "raw_md"
    return "html_generic"


# ── html → markdown ─────────────────────────────────────────────────


def html_to_markdown(html: str) -> tuple[str, str]:
    """Best-effort HTML → markdown. Returns (title, body_md).
    Uses readability-lxml if available (cleaner), otherwise html2text
    on the whole page, otherwise plain BeautifulSoup text extraction."""
    title = ""
    body = ""

    # Prefer readability + html2text combo
    try:
        from readability import Document  # readability-lxml
        doc = Document(html)
        title = (doc.short_title() or "").strip()
        cleaned_html = doc.summary()
    except Exception:
        cleaned_html = html

    try:
        import html2text
        h = html2text.HTML2Text()
        h.ignore_links = False
        h.ignore_images = True
        h.body_width = 0  # no hard wrap
        body = h.handle(cleaned_html).strip()
    except ImportError:
        # Fallback: BeautifulSoup text extraction
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(cleaned_html, "lxml")
            if not title:
                t = soup.find("title")
                if t:
                    title = t.get_text(strip=True)
            body = soup.get_text(separator="\n\n", strip=True)
        except ImportError:
            # Last resort: strip tags crudely
            body = re.sub(r"<[^>]+>", "", cleaned_html)
            body = re.sub(r"\n{3,}", "\n\n", body).strip()

    # If we still have no title, try to extract from markdown H1
    if not title and body:
        m = re.search(r"^#\s+(.+)$", body, re.MULTILINE)
        if m:
            title = m.group(1).strip()
    return title, body


# ── extract (main entry) ────────────────────────────────────────────


def extract_source(url: str) -> ExtractResult:
    """Fetch URL and extract clean markdown + metadata hints.
    Never does the final conversion — that's the agent's LLM job."""
    url = (url or "").strip()
    if not url:
        raise ConversionError("empty url")
    if not url.startswith(("http://", "https://")):
        raise ConversionError(f"invalid url (need http/https): {url}")

    content, ctype = fetch_url(url)
    st = detect_source_type(url, content, ctype)
    raw_size = len(content.encode("utf-8")) if isinstance(content, str) \
        else len(content)

    title = ""
    description_hint = ""
    body_md = ""

    if st == "raw_md" or "markdown" in ctype.lower():
        body_md = content
        m = re.search(r"^#\s+(.+)$", body_md, re.MULTILINE)
        if m:
            title = m.group(1).strip()
    elif st in ("github_repo", "github_folder"):
        # Try to fetch README.md directly for cleaner content
        body_md, title = _fetch_github_readme(url, content)
        if not body_md:
            title, body_md = html_to_markdown(content)
    else:
        # clawhub / html_generic → full HTML parse
        title, body_md = html_to_markdown(content)

    # Description hint: prefer the first prose paragraph ≤ 300 chars
    if body_md:
        for block in body_md.split("\n\n"):
            b = block.strip()
            # skip headings, code, lists, tables
            if not b or b.startswith(("#", "```", "- ", "* ", "|")):
                continue
            if len(b) < 20:
                continue
            description_hint = b[:300].replace("\n", " ").strip()
            break

    # Guard: excessively short body likely means JS-rendered page that
    # we couldn't extract content from
    if body_md and len(body_md) < 200:
        raise ConversionError(
            f"extracted body too short ({len(body_md)} chars) — "
            f"page may require JS rendering or content is auth-gated"
        )

    return ExtractResult(
        source_url=url,
        source_type=st,
        title=title,
        description_hint=description_hint,
        body_markdown=body_md,
        raw_bytes=raw_size,
    )


def _fetch_github_readme(url: str, fallback_html: str) -> tuple[str, str]:
    """Given a github.com URL, try to get the raw README.md.
    Returns (body_md, title). Empty tuple on failure."""
    # Normalise: strip /tree/main/path -> user/repo + path
    m = re.match(
        r"https?://github\.com/([^/]+)/([^/?#]+)(?:/(?:tree|blob)/([^/?#]+)(/.*)?)?",
        url,
    )
    if not m:
        return "", ""
    user, repo, ref, subpath = m.groups()
    ref = ref or "main"
    subpath = subpath or ""
    # Candidate README locations (first wins)
    candidates = []
    if subpath and subpath != "/":
        sp = subpath.strip("/")
        candidates.append(f"https://raw.githubusercontent.com/{user}/{repo}/{ref}/{sp}/README.md")
        candidates.append(f"https://raw.githubusercontent.com/{user}/{repo}/{ref}/{sp}/SKILL.md")
    candidates += [
        f"https://raw.githubusercontent.com/{user}/{repo}/{ref}/README.md",
        f"https://raw.githubusercontent.com/{user}/{repo}/main/README.md",
        f"https://raw.githubusercontent.com/{user}/{repo}/master/README.md",
    ]
    for c in candidates:
        try:
            txt, _ = fetch_url(c, timeout=15)
            if txt and len(txt) > 100:
                m2 = re.search(r"^#\s+(.+)$", txt, re.MULTILINE)
                title = m2.group(1).strip() if m2 else f"{user}/{repo}"
                return txt, title
        except Exception:
            continue
    return "", ""


# ── conversion prompt (read by agent) ───────────────────────────────

CONVERSION_PROMPT = """\
你是 TudouClaw skill 转换器。下面给你一段**其他平台的 skill 文档**（markdown），
请把它转换成 TudouClaw 格式的 skill 包。

TudouClaw skill 结构约定:

1. **SKILL.md** 必填:
   - YAML frontmatter: name (kebab-case), description (包含触发词!), applicable_roles
     (从 [general, coder, analyst, researcher, tester, reviewer, cto] 中选 1-3 个),
     scenarios (2-4 条), metadata: { source: tudou-builtin, license, tier: official }
   - 正文章节:
     * ⚠️ 防幻觉段（**必有**）: 列出本 skill 实际包含的文件，明确禁止 agent 瞎猜不存在的脚本
     * 🚨 合规/安全声明（若涉及金融/医疗/法律/隐私数据）
     * 工作流（4-5 步，包含 verify 步骤）
     * 可用函数速查表（若有 helpers.py）
     * 常见坑（2-5 条）
     * 完整示例（10-30 行 python）

2. **`_<name>_helpers.py`** 可选:
   - 仅当源 skill 提供 Python / CLI 可复用的操作时才生成
   - 导出 `__all__` 列表，函数签名清晰
   - 每个函数处理至少 1 次重试 + 友好 error
   - 顶部 docstring 给"典型用法"

3. **pip deps**: 列出需要的包（如 ["akshare>=1.15", "matplotlib>=3.7"]）

4. **合规红线**:
   - 涉及金融数据 → 强制"不构成投资建议"免责
   - 涉及个人数据 → 明确 data handling 策略
   - 需要 API key/OAuth → 明确 env var 名，绝不硬编码

5. **绝不**生成:
   - 执行 `rm -rf` / `curl | bash` 类危险命令的 helper
   - 硬编码任何 API key / secret / 内网 URL
   - ClawHub 独有但 TudouClaw 不存在的 API（如 "clawhub SDK"），这类应改写为 TudouClaw 原生能力

返回一个 JSON 对象，**不要任何 markdown 包裹**:
{
  "skill_name": "<kebab-case>",
  "skill_md": "<完整 markdown 字符串，含 frontmatter>",
  "helpers_py": "<python 代码字符串>" 或 null,
  "pip_deps": ["dep1>=x.y", ...],
  "notes": "<转换时做的判断 / 人工需确认的地方>"
}
"""


# ── validate ────────────────────────────────────────────────────────


def validate_skill_md(content: str) -> list[str]:
    """Return list of problems (empty if valid). Strict only on structural
    must-haves; content quality is up to reviewer."""
    problems: list[str] = []
    if not content or not content.strip():
        return ["empty content"]
    # Frontmatter
    fm = re.match(r"^---\n(.*?)\n---\n", content, re.DOTALL)
    if not fm:
        problems.append("missing YAML frontmatter (--- ... ---)")
    else:
        fm_text = fm.group(1)
        for required in ("name:", "description:"):
            if required not in fm_text:
                problems.append(f"frontmatter missing '{required}'")
    # Anti-hallucination section
    if "防幻觉" not in content and "anti-hallucination" not in content.lower():
        problems.append("missing 防幻觉 / anti-hallucination section")
    # Kebab-case name check
    m = re.search(r"^name:\s*(\S+)", content, re.MULTILINE)
    if m:
        name = m.group(1).strip().strip("'\"")
        if not re.match(r"^[a-z][a-z0-9-]*$", name):
            problems.append(
                f"name '{name}' must be kebab-case (lowercase, digits, hyphens)"
            )
    return problems


# ── write ───────────────────────────────────────────────────────────


def write_skill_package(
    skill_name: str,
    skill_md: str,
    helpers_py: Optional[str] = None,
    deps: Optional[list[str]] = None,
    source_url: str = "",
    output_dir: Optional[str] = None,
) -> dict:
    """Write the skill to <output_dir>/<skill_name>/.
    Default output_dir = ./generated_skills/ (agent workspace).
    NEVER writes directly to app/skills/builtin/ — admin must review first."""
    if not skill_name or not re.match(r"^[a-z][a-z0-9-]*$", skill_name):
        raise ConversionError(
            f"invalid skill_name '{skill_name}' — must be kebab-case"
        )
    problems = validate_skill_md(skill_md)
    if problems:
        raise ConversionError(
            "SKILL.md validation failed:\n  - " + "\n  - ".join(problems)
        )

    base = Path(output_dir) if output_dir else Path("generated_skills")
    skill_dir = base / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)

    # Inject source_url into skill_md metadata if absent
    if source_url and "source_url:" not in skill_md:
        skill_md = _inject_source_url(skill_md, source_url)

    paths = {}
    skill_md_p = skill_dir / "SKILL.md"
    skill_md_p.write_text(skill_md, encoding="utf-8")
    paths["skill_md"] = str(skill_md_p.resolve())

    if helpers_py and helpers_py.strip():
        helpers_name = f"_{skill_name.replace('-', '_')}_helpers.py"
        helpers_p = skill_dir / helpers_name
        helpers_p.write_text(helpers_py, encoding="utf-8")
        paths["helpers_py"] = str(helpers_p.resolve())

    if deps:
        deps_p = skill_dir / "requirements.txt"
        deps_p.write_text("\n".join(deps) + "\n", encoding="utf-8")
        paths["requirements"] = str(deps_p.resolve())

    # Sidecar manifest for traceability
    manifest = {
        "skill_name": skill_name,
        "source_url": source_url,
        "generated_by": "skill-converter",
        "files": list(paths.keys()),
        "review_status": "pending",
    }
    manifest_p = skill_dir / ".conversion_meta.json"
    manifest_p.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    paths["_meta"] = str(manifest_p.resolve())

    return paths


def _inject_source_url(skill_md: str, source_url: str) -> str:
    """Insert source_url: ... into metadata block of frontmatter."""
    fm_match = re.match(r"^(---\n)(.*?)(\n---\n)", skill_md, re.DOTALL)
    if not fm_match:
        return skill_md
    head, body, tail = fm_match.groups()
    # If metadata: block exists, insert source_url there
    if re.search(r"^metadata:", body, re.MULTILINE):
        new_body = re.sub(
            r"(^metadata:.*?)(\n(?=\S)|\Z)",
            lambda m: m.group(1) + f"\n  source_url: \"{source_url}\"" + m.group(2),
            body, count=1, flags=re.MULTILINE | re.DOTALL,
        )
    else:
        new_body = body.rstrip() + (
            f"\nmetadata:\n  source_url: \"{source_url}\"\n"
        )
    return head + new_body + tail + skill_md[fm_match.end():]


def preview_package(skill_dir: str) -> str:
    """Return a human-readable summary of a generated skill package."""
    p = Path(skill_dir)
    if not p.is_dir():
        raise ConversionError(f"not a directory: {skill_dir}")
    lines = [f"# Skill package preview — {p.name}", ""]
    files = sorted(p.iterdir())
    lines.append("## Files")
    for f in files:
        if f.is_file():
            size = f.stat().st_size
            lines.append(f"- `{f.name}` ({size} bytes)")
    meta_p = p / ".conversion_meta.json"
    if meta_p.exists():
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        lines += ["", "## Metadata", f"```json\n{json.dumps(meta, indent=2, ensure_ascii=False)}\n```"]
    skill_md_p = p / "SKILL.md"
    if skill_md_p.exists():
        md = skill_md_p.read_text(encoding="utf-8")
        lines += ["", "## SKILL.md (head 40 lines)", "```markdown"]
        lines += md.splitlines()[:40]
        lines += ["...", "```"]
    return "\n".join(lines)


def list_generated_skills(base_dir: str = "generated_skills") -> list[dict]:
    """List all generated skills with status."""
    base = Path(base_dir)
    if not base.is_dir():
        return []
    out = []
    for sd in sorted(base.iterdir()):
        if not sd.is_dir():
            continue
        meta_p = sd / ".conversion_meta.json"
        meta = {}
        if meta_p.exists():
            try:
                meta = json.loads(meta_p.read_text(encoding="utf-8"))
            except Exception:
                pass
        out.append({
            "name": sd.name,
            "path": str(sd.resolve()),
            "source_url": meta.get("source_url", ""),
            "review_status": meta.get("review_status", "unknown"),
            "has_helpers": any(sd.glob("_*_helpers.py")),
        })
    return out


# ── self-test ───────────────────────────────────────────────────────


if __name__ == "__main__":
    print("skill-converter self-test")
    print("=" * 50)
    test_url = sys.argv[1] if len(sys.argv) > 1 else \
        "https://clawhub.ai/matrixy/agent-browser-clawdbot"
    print(f"URL: {test_url}")
    try:
        r = extract_source(test_url)
        print(f"✓ source_type = {r.source_type}")
        print(f"✓ title = {r.title[:60]!r}")
        print(f"✓ raw_bytes = {r.raw_bytes}")
        print(f"✓ body_markdown = {len(r.body_markdown)} chars")
        print(f"  desc_hint = {r.description_hint[:100]!r}")
        print()
        print("First 500 chars of body_markdown:")
        print("-" * 40)
        print(r.body_markdown[:500])
        print("-" * 40)
    except ConversionError as e:
        print(f"✗ extraction failed: {e}")
        sys.exit(1)
