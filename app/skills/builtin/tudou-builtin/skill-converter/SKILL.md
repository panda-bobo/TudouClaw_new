---
name: skill-converter
description: Use when the user asks to 转换 / 导入 / import a skill from an external URL (ClawHub, GitHub README, raw markdown, generic HTML page) into TudouClaw format. This skill fetches + cleans + validates + writes files; the actual semantic conversion from source markdown → TudouClaw SKILL.md + helpers.py is done by the agent's own LLM reasoning using the CONVERSION_PROMPT template provided. Triggers: 转换skill, 导入skill, import skill from URL, 把 clawhub 这个做成 tudou skill, convert skill.
applicable_roles:
  - "general"
  - "researcher"
  - "coder"
scenarios:
  - "ClawHub 技能转换"
  - "GitHub README / 子目录 → TudouClaw skill"
  - "批量导入社区 skill"
metadata:
  source: tudou-builtin
  license: Apache-2.0
  tier: official
---

# skill-converter — URL → TudouClaw skill 转换工具

## ⚠️ 防幻觉 — 先读这一段

**这个 skill 的目录实际内容**（下面没列的文件都不存在，不要 `ls` / `find` 找）：

- `SKILL.md` — 本文档
- `_skill_converter.py` — helpers 模块（import 用）

**不**存在：`convert.sh` / `clawhub_cli.py` / `run.py` / 类似 one-shot 脚本。

**工作流 = 你（agent）自己写脚本跑 `_skill_converter` 的函数**。

---

## 🔒 安全红线（必守）

1. **绝不**把生成的 skill 直接写到 `app/skills/builtin/` —— 必须落到 `generated_skills/` 目录，等 admin 审核
2. **绝不**把源 skill 里出现的 API key / OAuth token / 内网 URL 搬进生成的 skill —— 改写成 env var 占位符（`os.environ["XXX_API_KEY"]`）
3. **绝不**生成包含 `rm -rf` / `curl … | bash` / 未沙箱化子进程的 helper
4. 生成的 skill **必须有防幻觉段**（由 `CONVERSION_PROMPT` 强制约束）
5. 金融/医疗/法律类 skill 必须带免责声明

---

## 工作流（5 步 — 不跳步）

### 1. 抓取 + 提取源内容

```python
# workspace/convert.py
from _skill_converter import extract_source
r = extract_source("https://clawhub.ai/matrixy/agent-browser-clawdbot")
print("type:", r.source_type)         # clawhub / github_repo / github_folder / raw_md / html_generic
print("title:", r.title)
print("size:", r.raw_bytes)
# 把 body 落盘供第 2 步读
open("workspace/_extracted.md", "w").write(r.body_markdown)
```

跑：`python workspace/convert.py`

### 2. Agent 读 `_extracted.md` 并基于 `CONVERSION_PROMPT` 推理

这一步**agent 自己用 LLM 做**，不是脚本做：

- 读 `_skill_converter.CONVERSION_PROMPT` 了解 TudouClaw skill 结构要求
- 读 `workspace/_extracted.md` 的源内容
- **自己推理**生成：
  - `skill_name` (kebab-case)
  - `skill_md` (完整 markdown，带 frontmatter + 防幻觉段 + 工作流 + 函数速查)
  - `helpers_py` (python 代码字符串，如果源 skill 有可包装的 Python/CLI；纯文档类 skill 可为 None)
  - `pip_deps` (list)
  - `notes` (需要人工确认的地方)

**关键**：这一步你必须**至少写 5-10 分钟**想清楚映射关系，不要机械翻译。比如：
- 源 skill 说"call our SDK"，TudouClaw 没这 SDK → 改写为 `bash` 或 `http_request` 调用
- 源 skill 有 OAuth 步骤 → 生成的 helper 要把 token 放 env var，禁止硬编码
- 源 skill 提到"访问 /api/admin/xxx"等内网端点 → 标 `notes` 让人工确认

### 3. 校验 + 写入 generated_skills/

```python
# 紧接上文
from _skill_converter import validate_skill_md, write_skill_package

skill_md = """---
name: agent-browser
...
"""          # 你生成的完整 markdown
helpers_py = None   # 或你生成的代码字符串

problems = validate_skill_md(skill_md)
if problems:
    print("❌ 校验失败:", problems)
else:
    paths = write_skill_package(
        skill_name="agent-browser",
        skill_md=skill_md,
        helpers_py=helpers_py,
        deps=["playwright>=1.40"],          # 或 []
        source_url=r.source_url,
        output_dir="generated_skills",       # 默认值，可省
    )
    print("✅ 写入:", paths)
```

### 4. 预览生成物

```bash
python -c "
from _skill_converter import preview_package
print(preview_package('generated_skills/agent-browser'))
" 2>&1
```

这会显示 SKILL.md 前 40 行 + 元信息，让 agent 自检一遍。

### 5. 汇报给用户

在 chat 里告诉用户：
- 生成路径
- `notes` 里需要人工确认的项
- 管理员审核通过后如何搬到 builtin：
  ```
  # 管理员手动操作（不要 agent 做）:
  mv generated_skills/<name> app/skills/builtin/tudou-builtin/<name>
  # 或压缩后走 Settings → Skills → Import 流程
  ```

---

## 可用函数速查

### 抓取 + 提取

```python
extract_source(url) -> ExtractResult
# 返回: source_url / source_type / title / description_hint / body_markdown / raw_bytes

fetch_url(url, timeout=30) -> (content_text, content_type)
# 底层 HTTP，不做清洗

detect_source_type(url) -> "clawhub"|"github_repo"|"github_folder"|"raw_md"|"html_generic"

html_to_markdown(html) -> (title, body_md)
# readability + html2text (fallback: BeautifulSoup)
```

### 校验 + 写入

```python
validate_skill_md(content) -> list[str]
# 返回问题列表 (空=合格). 检查 frontmatter / 防幻觉段 / kebab-case name

write_skill_package(skill_name, skill_md, helpers_py=None,
                     deps=None, source_url="", output_dir=None) -> dict
# 写盘 + 注入 source_url 到 metadata + 生成 .conversion_meta.json
# 默认 output_dir = "generated_skills"
# 会强制校验，校验失败抛 ConversionError

preview_package(skill_dir) -> str
# 返回 markdown 预览字符串

list_generated_skills(base_dir="generated_skills") -> list[dict]
# 列出所有已转换的 skill + 状态
```

### 常量

```python
CONVERSION_PROMPT  # 你（agent）自己转换时该遵循的指令模板
```

---

## CONVERSION_PROMPT 要点速读

读完 `CONVERSION_PROMPT`（见 `_skill_converter.py` 顶部 or `print(CONVERSION_PROMPT)`），
核心约束：

1. **kebab-case name**（小写 + 短横）
2. **description 必须包含触发词** —— agent 靠这个判断何时用
3. **applicable_roles** 从预设列表选 1-3 个
4. **防幻觉段必填**
5. 金融/医疗 → 合规声明
6. 不硬编码 key、不生成危险命令
7. 返回**纯 JSON**（不要 markdown 代码块包裹）

---

## 常见坑

1. **JS 渲染页面**：SPA 站（只返回 shell）→ body_markdown 会 < 200 字符，`extract_source` 会抛 `ConversionError`。给用户建议改用 GitHub README 或直接请他们贴 markdown 内容
2. **GitHub 私有仓库**：抓 README 返回 404，告诉用户需要公开或改用复制粘贴
3. **源 skill 有依赖 ClawHub SDK / Maton 专有 API**：agent 推理时必须改写成 TudouClaw 原生能力（bash / http_request / 其他 builtin），不要硬搬
4. **中文/多语言**：body_markdown 已 UTF-8，无需额外处理
5. **重复转换同一 URL**：`write_skill_package` 会覆盖同名目录下的 `SKILL.md` + helpers，老数据 → 最好先 `list_generated_skills` 查是否已有
6. **body 超大**（> 50KB）：截取关键段（`### ` 开头的章节 + 代码示例）再喂 LLM，避免 token 爆炸

---

## 完整示例

```python
# workspace/convert_clawhub.py
from _skill_converter import extract_source, validate_skill_md, write_skill_package

# ── Step 1: 抓取 ──
url = "https://clawhub.ai/mbpz/akshare-stock"
r = extract_source(url)
print(f"抓到 {r.source_type}: {r.title} ({r.raw_bytes} bytes)")

# 把 body 落盘，agent 下一步读
open("workspace/_extracted.md", "w", encoding="utf-8").write(r.body_markdown)
print("已落盘 workspace/_extracted.md，第 2 步由 agent 推理生成 SKILL.md")
```

```python
# workspace/write_skill.py —— agent 第 2 步推理完后跑
from _skill_converter import validate_skill_md, write_skill_package

skill_md = '''---
name: akshare-stock-v2
description: ...（agent 按 CONVERSION_PROMPT 推理出来的完整 markdown）
...
---

# akshare-stock-v2

## ⚠️ 防幻觉
...
'''

helpers_py = '''"""..."""
import akshare as ak
...
'''

problems = validate_skill_md(skill_md)
assert not problems, f"校验失败: {problems}"

paths = write_skill_package(
    skill_name="akshare-stock-v2",
    skill_md=skill_md,
    helpers_py=helpers_py,
    deps=["akshare>=1.15", "matplotlib>=3.7"],
    source_url="https://clawhub.ai/mbpz/akshare-stock",
)
print("✅ 生成到:", paths)
# 用户就能在 chat 里看到 generated_skills/akshare-stock-v2/ 的 SKILL.md 文件卡片
```

跑完后 chat 自动显示生成的 `SKILL.md` + `_helpers.py` 文件卡片，用户审核，管理员搬到 builtin。
