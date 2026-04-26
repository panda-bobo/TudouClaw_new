"""Single source of truth for agent system prompts.

Architecture: every agent's system prompt is composed of two parts.

  1. **DEFAULT** (hardcoded here, in this file)
     The minimum contract every agent must carry: identity / language /
     tool-use rules / knowledge-write rules / file & image display
     protocol / workspace context. Operators CANNOT disable this part —
     without it the agent doesn't know how to use the platform.

  2. **SETTINGS** (read from ``config.yaml`` → Settings UI)
     Operator-editable rules: ``scene_prompts`` list (per-role or
     all-agent global rules), legacy ``global_system_prompt`` field.
     Edit via Settings → System Prompts in the portal; takes effect on
     next prompt rebuild.

The agent's own ``system_prompt`` / ``custom_instructions`` (persona) is
still composed in ``agent.py`` because it needs per-agent state. This
module exposes the building blocks; ``agent.py`` calls them.

Other modules MUST import from here. Do NOT inline new prompt text in
agent.py / agent_llm.py / agent_growth.py / repl.py — change it once
here and every agent picks it up.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("tudou.system_prompt")


# ═════════════════════════════════════════════════════════════════════
#  PART 1: DEFAULT  — hardcoded, not configurable
# ═════════════════════════════════════════════════════════════════════
#
# These constants are the platform's contract with the LLM. They tell
# the model what tools it has, how to use the wiki, where files go, etc.
# Operators cannot disable any of this — it's load-bearing.
#
# If you want to ADD an operator-configurable rule, add it to
# ``scene_prompts`` in ``config.yaml`` (see PART 2). If you want to
# CHANGE platform-level behavior, edit the constants here.

# ── Tool usage ─────────────────────────────────────────────────────

_TOOL_RULES_ZH = (
    "## 工具使用\n"
    "• 多个独立工具 → 在**一条**回复里返回多个 tool_calls 并行执行;只有"
    "后一个工具依赖前一个结果时才串行。\n"
    "• 多步任务 (≥3 步) → 先 plan_update(action='create_plan') 写计划"
    "(每步必须有 acceptance 字段);每步结束 complete_step 时 result_summary "
    "必须引用 acceptance。\n"
    "• ⚠️ 调完 plan_update 后,**禁止**在 chat 里用文字、markdown 列表、"
    "checkbox 复述步骤(UI 的 TODOs 面板已经显示);你下一步该直接 "
    "start_step + 调实际工具,不是再讲一遍计划。\n"
    "• 可拆为独立子任务 → team_create 启子 agent 并行(3 个并行 ~1 分钟 vs "
    "串行 ~3 分钟)。\n"
    "• Bash / 敏感写入可能需要人工审批;被拒时告知用户并给替代方案。"
)

_TOOL_RULES_EN = (
    "## Tools\n"
    "• Independent tools → return multiple tool_calls in ONE assistant "
    "response (parallel). Only serialize when a later tool's args "
    "depend on an earlier tool's result.\n"
    "• Multi-step tasks (3+) → call plan_update(action='create_plan') "
    "FIRST; each step needs an `acceptance` field; complete_step's "
    "result_summary must reference it.\n"
    "• ⚠️ AFTER calling plan_update, do NOT repeat the plan in chat as "
    "prose / markdown bullets / checkboxes — the TODOs panel already "
    "shows it. Next step is start_step + the real tool call, not "
    "re-stating the plan.\n"
    "• Independent subtasks → use team_create for parallel sub-agents "
    "(3 sub-agents ~1 min vs serial ~3 min).\n"
    "• Bash / sensitive writes may require human approval; if denied, "
    "tell the user and propose an alternative."
)


# ── Knowledge & experience (Karpathy wiki pattern) ────────────────

_KNOWLEDGE_RULES_ZH = (
    "## 知识 / 经验\n"
    "• 写：调 wiki_ingest(kind, title, body)。kind ∈ "
    "experience(场景+行动规则) | methodology(方法论) | "
    "template(写作/结构模板) | pattern(固定逻辑) | reference(规范/wiki)。"
    "scope 默认本角色;跨角色共享传 scope='global'。\n"
    "• 查：调 knowledge_lookup(query) — 一次查询里压完所有关键词。\n"
    "• 装新工具能力 → 让用户从技能库 UI 安装(不要自建 skill 或写 SKILL.md)。\n"
    "• save_experience 已弃用,新内容写 wiki。"
)

_KNOWLEDGE_RULES_EN = (
    "## Knowledge & Experience\n"
    "• Write reusable lessons: wiki_ingest(kind, title, body). "
    "kind ∈ experience | methodology | template | pattern | reference. "
    "scope defaults to your role; pass scope='global' for cross-role.\n"
    "• Query: knowledge_lookup(query) — pack all keywords in ONE call.\n"
    "• New capabilities → ask user to install via Skill Registry UI "
    "(do NOT create skills or write SKILL.md yourself).\n"
    "• save_experience is deprecated; new entries go to wiki."
)


# ── File / image display protocols ────────────────────────────────

_FILE_DISPLAY = (
    "<file_display>\n"
    "When you produce or reference a file artifact (PDF / DOCX / PPTX / "
    "XLSX / image / video / md / txt / csv / json / etc.), surface the "
    "FULL workspace-relative path on its own line so the chat UI can "
    "render it as a clickable card. Avoid wrapping the path in code "
    "spans (`...`) or code blocks. When delivering a file, ALWAYS "
    "quote its path explicitly in your final assistant message.\n"
    "</file_display>"
)

# Long-form file_display contract — emitted by agent.py when the agent has
# file-producing tools (write_file / create_pptx / etc.). Mixes EN bullet
# list + Chinese summary so single-language agents still get the rules.
# Single source of truth — agent.py and prompt_block_catalog.py both pull
# from here. DO NOT inline this string in callers.
_FILE_DISPLAY_LONG = (
    "<file_display>\n"
    "When you produce a file in your workspace (video, image, audio, "
    "document, archive, etc.) the portal automatically renders a "
    "clickable FileCard for it in the chat UI — you do NOT need to "
    "embed it yourself. Follow these rules:\n"
    "  1. NEVER write markdown image syntax `![name](path)` for "
    "non-image files (mp4, mp3, pdf, docx, zip, etc.). It always "
    "renders as a broken image.\n"
    "  2. NEVER tell the user to drag the file into the chat window, "
    "or to copy/move the file manually. The card is already there.\n"
    "  3. NEVER fabricate `/api/portal/attachment?path=...` URLs in "
    "your reply text. Use the file's plain relative or absolute "
    "path if you must mention it; the FileCard handles the link.\n"
    "  4. Keep your reply short: a one-line summary of what the file "
    "is and (if relevant) what makes it interesting. The card "
    "carries the filename, size, kind, and click-to-open action.\n"
    "  5. For images specifically, you MAY use markdown image "
    "syntax — but it is still optional, the card already includes "
    "a thumbnail.\n"
    "中文说明:你在 workspace 里产出文件后(视频/图片/音频/文档/压缩包等),"
    "聊天界面会自动渲染一个可点击的 FileCard 卡片。你不需要、也不要试图自己"
    "把文件嵌入消息里。规则:不要给非图片文件写 ![名字](路径) 的 markdown "
    "图片语法(永远显示为破损图标);不要叫用户把文件拖进聊天框或手动复制;"
    "不要在回复里编造 /api/portal/attachment?path=... 链接;一句话说明文件"
    "做了什么就够,卡片自带文件名/大小/打开按钮。\n"
    "</file_display>"
)

_IMAGE_DISPLAY_ZH = (
    "<image_display>\n"
    "回复里要显示图片时:\n"
    "• 工作区图片 → markdown: ![](workspace/x.png)\n"
    "• 网页 URL → ![](https://...)\n"
    "• 用户刚上传的图片 → 直接用文字描述,不必再贴 link\n"
    "禁止把图片路径放进代码块,会变成纯文本不显示。\n"
    "</image_display>"
)

_IMAGE_DISPLAY_EN = (
    "<image_display>\n"
    "When showing images:\n"
    "• Workspace images → markdown: ![](workspace/x.png)\n"
    "• Web URLs → ![](https://...)\n"
    "• User-uploaded images (already in your visual context) → "
    "describe in text; do not re-link.\n"
    "Do NOT wrap image paths in code blocks.\n"
    "</image_display>"
)

# Long-form image_display — adds front-end rendering details (Portal
# routes the path through /api/portal/attachment, supported formats,
# remote URLs render the same way). Used by agent.py inline.
_IMAGE_DISPLAY_LONG_ZH = (
    "<image_display>\n"
    "当你需要给用户展示本地图片/截图（例如你生成、下载、找到的 "
    "PNG/JPG/GIF/WEBP 文件）时，直接在回复里用 markdown 图片语法："
    "  ![简短描述](相对路径或绝对路径)\n"
    "前端会自动把它渲染成可点击放大的图片。\n"
    "• 优先使用相对于你工作目录的路径，例如 `./blog-screenshot.png`；\n"
    "• 也可以写绝对路径，只要文件在你的工作目录下；\n"
    "• 不要只说「文件保存在 xxx」，要同时贴出 ![](path)，这样用户能立即看到；\n"
    "• 远端 URL（http/https）直接写即可，同样会渲染成图片；\n"
    "• 只支持 png/jpg/jpeg/gif/webp/svg/bmp/ico，其他类型走普通文件链接。\n"
    "</image_display>"
)

_IMAGE_DISPLAY_LONG_EN = (
    "<image_display>\n"
    "When you need to show the user a local image/screenshot (e.g. a "
    "PNG/JPG/GIF/WEBP file you generated, downloaded, or found), embed "
    "it directly in your reply with markdown image syntax:\n"
    "  ![short description](relative-or-absolute-path)\n"
    "The portal chat UI will render it inline as a clickable, zoomable image.\n"
    "• Prefer paths relative to your working directory, e.g. `./blog-screenshot.png`.\n"
    "• Absolute paths are fine as long as the file lives inside your workspace.\n"
    "• Don't just say \"saved to xxx\" — always paste ![](path) so the user sees it.\n"
    "• Remote http/https URLs work too and render the same way.\n"
    "• Supported formats: png, jpg, jpeg, gif, webp, svg, bmp, ico.\n"
    "</image_display>"
)


# ── Attachment contract — for agents with messaging / send_* tools ──

_ATTACHMENT_CONTRACT_ZH = (
    "<attachment_contract>\n"
    "当你调用发送类工具（send_email / send_message / 类似的 IM "
    "发送工具）且本轮对话中你刚产出了文件（PPT、文档、报告、图片等）"
    "或用户明确要求发送某个文件时，必须：\n"
    "  1. 把文件的完整路径放进工具调用的 `attachments` 参数"
    "（数组）。\n"
    "  2. 不要只在邮件/消息正文里写文件名 —— 收件人不会因为正文"
    "提到文件名就自动收到附件。\n"
    "  3. 如果工具有多个附件参数名（如 attachments / files / "
    "attach_paths），任选一个支持的即可，但不能留空。\n"
    "  4. 如果不确定文件是否需要作为附件发送，先问用户；不要"
    "静默省略。\n"
    "</attachment_contract>"
)

_ATTACHMENT_CONTRACT_EN = (
    "<attachment_contract>\n"
    "When you call a send-type tool (send_email / send_message / "
    "any IM send tool) AND you produced a file in this turn "
    "(PPT, doc, report, image, etc.) OR the user explicitly asked "
    "you to send a file, you MUST:\n"
    "  1. Put the file's full path into the tool call's "
    "`attachments` parameter (an array).\n"
    "  2. Do NOT rely on mentioning the filename in the email/"
    "message body — recipients will not get the file just "
    "because you named it in prose.\n"
    "  3. If the tool exposes multiple attachment-like "
    "parameters (attachments / files / attach_paths), pick any "
    "supported one, but it must not be empty.\n"
    "  4. If unsure whether a file should be attached, ask the "
    "user — don't silently omit it.\n"
    "</attachment_contract>"
)


# ── Plan + step tracking protocol (drives UI task-queue panel) ────

_PLAN_PROTOCOL_ZH = (
    "## 任务分解 & 进度汇报协议\n"
    "当用户请求是一个多步任务（比如研究 + 写报告、搜索 + 生成文件 + 发邮件），"
    "请在**开始执行之前**先输出一个计划块，然后再开始动手：\n"
    "\n"
    "```\n"
    "📋 计划\n"
    "1. [第一步做什么] — 工具: <tool_name>\n"
    "2. [第二步做什么] — 工具: <tool_name>\n"
    "3. ...\n"
    "```\n"
    "\n"
    "规则：\n"
    "- 计划块只在**首次响应**里出现一次；后续轮次无需重复。\n"
    "- 每完成一步，单独一行写 `✓ 第 N 步：<一句话说做了什么>`。\n"
    "- 如果用户只是闲聊/一次问答（不涉及多步交付），**跳过**计划块，直接回答。\n"
    "- 工具名要和你后续实际调用的工具一致（如 `web_search` / `bash` / `write_file`）。\n"
    "- 步骤数 1–6 个，不要拆得太细；一个「搜 3 个来源」算一步，不要写成 3 步。\n"
    "\n"
    "这个协议只是让 UI 能把工具调用归到对应步骤——你该说的话、用的工具都不变。"
)


# ── Workspace context (parameterized 6 → 1) ───────────────────────

def _workspace_context(
    *,
    ctx_type: str,
    use_zh: bool,
    working_dir: str,
    shared_workspace: str = "",
    project_name: str = "",
    project_id: str = "",
    meeting_id: str = "",
) -> str:
    """Render the ``<workspace_context>`` block for one of solo /
    project / meeting × zh / en. Empty string is returned if
    ``working_dir`` is empty (no useful info to render)."""
    if not working_dir and not shared_workspace:
        return ""
    lines = ["<workspace_context>"]
    if ctx_type == "project":
        dest = shared_workspace or working_dir
        if use_zh:
            lines.append(f"项目: {project_name} (id={project_id})")
            lines.append(f"共享工作区: {dest}")
            lines.append(
                "⚠️ 文件写入规则 (必须遵守): 所有交付物写到上面共享工作区,"
                "不要写到私人工作区,否则团队其他成员看不到。"
            )
        else:
            lines.append(f"Project: {project_name} (id={project_id})")
            lines.append(f"Shared workspace: {dest}")
            lines.append(
                "⚠️ File write rule (MANDATORY): all deliverables MUST "
                "go to the shared workspace above, NOT the private one."
            )
    elif ctx_type == "meeting":
        dest = shared_workspace or working_dir
        if use_zh:
            lines.append(f"会议工作区: {dest}")
            if meeting_id:
                lines.append(f"会议 id: {meeting_id}")
            lines.append(
                "⚠️ 文件写入规则 (必须遵守): 会议产出文件写到上面这个会议"
                "工作区,所有参会 agent 共享访问。"
            )
        else:
            lines.append(f"Meeting workspace: {dest}")
            if meeting_id:
                lines.append(f"Meeting id: {meeting_id}")
            lines.append(
                "⚠️ File write rule (MANDATORY): meeting deliverables go "
                "to the meeting workspace; all attending agents share access."
            )
    else:  # solo or unknown
        if use_zh:
            lines.append(f"私人工作区: {working_dir}")
            lines.append(
                "⚠️ 文件写入规则: write_file / edit_file / create_pptx 等"
                "工具的相对路径会落到上面这个目录;绝对路径不动。"
            )
        else:
            lines.append(f"Private workspace: {working_dir}")
            lines.append(
                "⚠️ File write rule: write_file / edit_file / create_pptx "
                "relative paths land in the directory above."
            )
    lines.append("</workspace_context>")
    return "\n".join(lines)


# ── Workspace context (LONG: deliverable routing rules) ───────────
#
# Used by agent.py inline today — moved here so prompt_block_catalog
# can mirror it without duplicating text. Differs from
# ``_workspace_context`` (SHORT) by:
#   • mandatory deliverable destination rules (CAPS warning lines)
#   • zh/en branches with sub-agent guidance (team_create no working_dir)
#   • degrades to solo when ctx_type=project|meeting but shared empty


def _workspace_context_long(
    *,
    ctx_type: str,
    use_zh: bool,
    working_dir: str,
    shared_workspace: str = "",
    project_name: str = "",
    project_id: str = "",
) -> str:
    """LONG-form workspace context with deliverable routing rules.

    Returns "" when neither ``working_dir`` nor ``shared_workspace`` is
    set — assembler treats that as empty render and skips the block.
    """
    if not working_dir and not shared_workspace:
        return ""

    ctx_type = (ctx_type or "solo").lower()
    # If project/meeting but no shared dir, degrade to solo so we don't
    # point the agent at an empty path.
    if ctx_type in ("project", "meeting") and not shared_workspace:
        ctx_type = "solo"

    lines: list[str] = []
    if use_zh:
        lines.append("<workspace_context>")
        if ctx_type == "solo":
            lines.append(f"工作目录 (你自己的空间): {working_dir}")
            lines.append("")
            lines.append("⚠️ 文件写入规则 (必须遵守):")
            lines.append(f"• 所有产出文件写入工作目录: {working_dir}")
        elif ctx_type == "project":
            lines.append(f"私有工作目录 (scratch/日志用): {working_dir}")
            lines.append(f"项目共享目录 (所有产出必须写这里): {shared_workspace}")
            if project_name:
                lines.append(f"所属项目: {project_name} (ID: {project_id})")
            lines.append("")
            lines.append("⚠️ 文件写入规则 (必须遵守):")
            lines.append(f"• 所有交付物 / 产出文件 → 必须写入项目共享目录: {shared_workspace}")
            lines.append("  （PPT、文档、报告、代码、图片等，一律放这里，不要自行判断"
                         "是否只有你会用到）")
            lines.append(f"• 仅供你自己临时使用的 scratch / 日志 → 可写入私有目录: {working_dir}")
        else:  # meeting
            lines.append(f"私有工作目录 (scratch/日志用): {working_dir}")
            lines.append(f"会议共享目录 (所有产出必须写这里): {shared_workspace}")
            lines.append("")
            lines.append("⚠️ 文件写入规则 (必须遵守):")
            lines.append(f"• 所有交付物 / 产出文件 → 必须写入会议共享目录: {shared_workspace}")
            lines.append("  （会议纪要、行动项、附件等，一律放这里）")
            lines.append(f"• 仅供你自己临时使用的 scratch / 日志 → 可写入私有目录: {working_dir}")
        lines.append("• 使用相对路径（如 src/main.py）而非绝对路径。")
        lines.append("• 创建子Agent (team_create) 时不要指定 working_dir，自动继承。")
        lines.append("</workspace_context>")
    else:
        lines.append("<workspace_context>")
        if ctx_type == "solo":
            lines.append(f"Workspace (your own): {working_dir}")
            lines.append("")
            lines.append("⚠️ File write rules (MUST follow):")
            lines.append(f"• All produced files go to your workspace: {working_dir}")
        elif ctx_type == "project":
            lines.append(f"Private workspace (scratch/logs only): {working_dir}")
            lines.append(f"Project shared directory (ALL deliverables go here): {shared_workspace}")
            if project_name:
                lines.append(f"Project: {project_name} (ID: {project_id})")
            lines.append("")
            lines.append("⚠️ File write rules (MUST follow):")
            lines.append(f"• ALL deliverables / produced files → MUST go to shared dir: {shared_workspace}")
            lines.append("  (PPTs, docs, reports, code, images — all go here. Do NOT second-guess "
                         "whether peers need the file.)")
            lines.append(f"• Your own scratch / logs only → may go to private dir: {working_dir}")
        else:  # meeting
            lines.append(f"Private workspace (scratch/logs only): {working_dir}")
            lines.append(f"Meeting shared directory (ALL deliverables go here): {shared_workspace}")
            lines.append("")
            lines.append("⚠️ File write rules (MUST follow):")
            lines.append(f"• ALL deliverables / produced files → MUST go to meeting shared dir: {shared_workspace}")
            lines.append("  (Meeting notes, action items, attachments — all go here.)")
            lines.append(f"• Your own scratch / logs only → may go to private dir: {working_dir}")
        lines.append("• Use relative paths (e.g., src/main.py), not absolute paths.")
        lines.append("• When spawning sub-agents (team_create), do NOT set working_dir.")
        lines.append("</workspace_context>")
    return "\n".join(lines)


# ── Identity prelude ──────────────────────────────────────────────

def _identity_line(name: str, role: str, language: str = "auto") -> str:
    """First line of every prompt. Tells the model who/what it is."""
    name = (name or "").strip() or "Agent"
    role = (role or "").strip() or "general"
    if isinstance(language, str) and language.lower().startswith("zh"):
        return f"你是 {name},角色: {role}。"
    return f"You are {name}. Role: {role}."


def _language_directive(language: str) -> str:
    """Return ``Always respond in <lang>.`` line if language is set, else ""."""
    if not language or language.lower() in ("auto", ""):
        return ""
    lang_map = {
        "zh-CN": "中文", "zh": "中文",
        "en": "English",
        "ja": "日本語", "ko": "한국어",
        "es": "Español", "fr": "Français", "de": "Deutsch",
    }
    name = lang_map.get(language, language)
    if name == "中文":
        return "始终用中文回复。"
    return f"Always respond in {name}."


# ─────────────────────────────────────────────────────────────────────
# Public DEFAULT builder
# ─────────────────────────────────────────────────────────────────────


def build_default_prompt(
    *,
    name: str,
    role: str,
    language: str = "auto",
    ctx_type: str = "solo",
    working_dir: str = "",
    shared_workspace: str = "",
    project_name: str = "",
    project_id: str = "",
    meeting_id: str = "",
) -> str:
    """Compose PART 1 (DEFAULT) — the hardcoded baseline every agent gets.

    Includes: identity, language directive (if any), tool rules,
    knowledge rules, file/image display protocols, workspace context.

    Caller is responsible for appending PART 2 (settings block) and
    persona on top of this. See ``compose_full_prompt`` for the full
    composition helper.
    """
    use_zh = isinstance(language, str) and language.lower().startswith("zh")

    parts: list[str] = []
    parts.append(_identity_line(name, role, language))
    lang_dir = _language_directive(language)
    if lang_dir:
        parts.append(lang_dir)

    parts.append(_TOOL_RULES_ZH if use_zh else _TOOL_RULES_EN)
    parts.append(_KNOWLEDGE_RULES_ZH if use_zh else _KNOWLEDGE_RULES_EN)
    parts.append(_FILE_DISPLAY)
    parts.append(_IMAGE_DISPLAY_ZH if use_zh else _IMAGE_DISPLAY_EN)

    ws = _workspace_context(
        ctx_type=ctx_type, use_zh=use_zh,
        working_dir=working_dir, shared_workspace=shared_workspace,
        project_name=project_name, project_id=project_id,
        meeting_id=meeting_id,
    )
    if ws:
        parts.append(ws)

    return "\n\n".join(parts)


# ═════════════════════════════════════════════════════════════════════
#  PART 2: SETTINGS  — read from config.yaml (Settings UI editable)
# ═════════════════════════════════════════════════════════════════════
#
# Operators add platform-wide or role-specific rules through the
# Settings UI. Backed by ``config.yaml`` keys:
#
#   global_system_prompt: <string>          ← legacy single block
#   scene_prompts:
#     - id: ...
#       name: ...
#       prompt: ...
#       enabled: true|false
#       scope: all | roles
#       roles: [<role>, ...]                ← when scope == "roles"
#
# This module is the ONLY reader. agent.py / agent_llm.py do not
# inline scene_prompts logic anymore.


def _read_config() -> dict:
    """Best-effort config.yaml read. Returns {} on any failure."""
    try:
        from . import llm as _llm
    except Exception:
        try:
            from app import llm as _llm  # type: ignore
        except Exception:
            return {}
    try:
        cfg = _llm.get_config()
    except Exception:
        return {}
    return cfg if isinstance(cfg, dict) else {}


def build_settings_block(agent_role: str = "") -> str:
    """Compose PART 2 — the operator-configured rules block.

    Reads ``global_system_prompt`` (legacy) + ``scene_prompts`` list,
    filters by ``scope`` / ``roles``, wraps each in a labeled
    ``<system_prompt name="...">`` block so the LLM can tell them apart.

    Returns "" when nothing is configured — caller should drop the
    empty string rather than emit blank lines.
    """
    cfg = _read_config()
    parts: list[str] = []

    # Legacy: global_system_prompt as the first block (back-compat)
    legacy = cfg.get("global_system_prompt") or ""
    if isinstance(legacy, str) and legacy.strip():
        parts.append(
            f"<system_prompt name=\"Global Rules\">\n"
            f"{legacy.strip()}\n"
            f"</system_prompt>"
        )

    scene_prompts = cfg.get("scene_prompts", [])
    if not isinstance(scene_prompts, list):
        return "\n\n".join(parts) if parts else ""

    for sp in scene_prompts:
        if not isinstance(sp, dict):
            continue
        if not sp.get("enabled", True):
            continue
        scope = sp.get("scope", "all")
        if scope == "roles":
            allowed = sp.get("roles", []) or []
            if agent_role and agent_role not in allowed:
                continue
        name = (sp.get("name") or "").strip()
        prompt = (sp.get("prompt") or "").strip()
        if not prompt:
            continue
        if name:
            parts.append(
                f"<system_prompt name=\"{name}\">\n{prompt}\n</system_prompt>"
            )
        else:
            parts.append(f"<system_prompt>\n{prompt}\n</system_prompt>")

    return "\n\n".join(parts) if parts else ""


# ═════════════════════════════════════════════════════════════════════
#  PART 3: PERSONA  — per-agent customization
# ═════════════════════════════════════════════════════════════════════
#
# Three semantic fields per agent, each with a DISTINCT job:
#
#   system_prompt          — IDENTITY + EXPERTISE: what this agent does,
#                            its specialty, the rules of its profession.
#                            Example: "You are a senior A-share analyst..."
#
#   soul_md                — COMMUNICATION + BEHAVIOR: how this agent
#                            speaks, its tone, mannerisms, persona traits.
#                            Example: "Calm and methodical. Uses 'let's
#                            walk through this'..."
#
#   custom_instructions    — SHORT NOTES: ad-hoc additions or overrides
#                            the operator wants applied last.
#
# Historically these three got jumbled — many agents have system_prompt
# == soul_md (literally identical text). With this builder, content
# moves to whichever field semantically fits, and we wrap each in a
# labeled section so the LLM can parse the distinction.

def build_persona_block(
    *,
    system_prompt: str = "",
    soul_md: str = "",
    custom_instructions: str = "",
    use_zh: bool = False,
) -> str:
    """Render the per-agent persona section.

    Empty fields are skipped. Returns "" when all three are empty.
    Sections are labeled in the agent's language so the LLM can tell
    them apart and apply each appropriately.
    """
    parts: list[str] = []

    sp = (system_prompt or "").strip()
    sm = (soul_md or "").strip()
    ci = (custom_instructions or "").strip()

    if sp:
        head = "## 身份与专业" if use_zh else "## Identity & Expertise"
        parts.append(f"{head}\n{sp}")

    if sm:
        head = "## 沟通风格与行为方式" if use_zh else "## Communication & Behavior"
        parts.append(f"{head}\n{sm}")

    if ci:
        head = "## 补充指令" if use_zh else "## Additional Notes"
        parts.append(f"{head}\n{ci}")

    return "\n\n".join(parts)


# ═════════════════════════════════════════════════════════════════════
#  Convenience: full composition (DEFAULT + SETTINGS [+ PERSONA])
# ═════════════════════════════════════════════════════════════════════


def compose_full_prompt(
    *,
    name: str,
    role: str,
    language: str = "auto",
    ctx_type: str = "solo",
    working_dir: str = "",
    shared_workspace: str = "",
    project_name: str = "",
    project_id: str = "",
    meeting_id: str = "",
    # PART 3 persona inputs (all optional)
    agent_system_prompt: str = "",
    agent_soul_md: str = "",
    agent_custom_instructions: str = "",
) -> str:
    """Full static system prompt: DEFAULT + SETTINGS + PERSONA.

    Single entry point for ``Agent._build_static_system_prompt`` —
    callers pass agent fields, get back the composed text. Empty
    sections are silently skipped.
    """
    use_zh = isinstance(language, str) and language.lower().startswith("zh")

    default_block = build_default_prompt(
        name=name, role=role, language=language,
        ctx_type=ctx_type, working_dir=working_dir,
        shared_workspace=shared_workspace,
        project_name=project_name, project_id=project_id,
        meeting_id=meeting_id,
    )
    settings_block = build_settings_block(role)
    persona_block = build_persona_block(
        system_prompt=agent_system_prompt,
        soul_md=agent_soul_md,
        custom_instructions=agent_custom_instructions,
        use_zh=use_zh,
    )

    parts = [default_block]
    if settings_block:
        parts.append(settings_block)
    if persona_block:
        parts.append(persona_block)
    return "\n\n".join(parts)


# Back-compat alias — earlier callers used this name; still works.
def compose_default_and_settings(**kwargs) -> str:
    """Legacy alias for ``compose_full_prompt`` without persona."""
    # Strip any persona kwargs the caller might pass; they're allowed
    # but ignored here for back-compat with the older signature.
    for k in ("agent_system_prompt", "agent_soul_md",
              "agent_custom_instructions"):
        kwargs.pop(k, None)
    return compose_full_prompt(**kwargs)


# Public surface
__all__ = [
    # PART 1: default builders
    "build_default_prompt",
    # PART 2: settings reader
    "build_settings_block",
    # PART 3: persona builder
    "build_persona_block",
    # combined
    "compose_full_prompt",
    "compose_default_and_settings",   # back-compat alias
    # Phase 2b — extracted block-level constants / fns (for catalog reuse)
    # Note: these are also referenced by app.agent so refactor stays
    # single-source.
]
