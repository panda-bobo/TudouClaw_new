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

# 2026-05-14 governance: this block describes framework MECHANICS only
# (how tool_calls work, what plan_update does, where 📂/📦 markers come
# from). Behavioral rules ("don't repeat the plan in chat", "don't
# narrate before tool calls", batching style, etc.) live in admin-
# editable scene_prompts so each install can tune them — code stays
# the mechanism, scene_prompts hold the policy.
_TOOL_RULES_ZH = (
    "## 工具调用机制\n"
    "• **并行返回**:独立的 tool_calls 在一条回复里同时返回(框架自动并行执行);"
    "后一个工具依赖前一个结果时才串行。\n"
    "• **plan_update**:`action='create_plan'` 建计划,每步含 `acceptance` 字段;"
    "`complete_step` 的 `result_summary` 必须引用 acceptance;TODOs 面板自动渲染。\n"
    "• **team_create**:启子 agent 并行(适合可独立分解的子任务)。\n"
    "• **审批**:bash / 敏感写入可能需要审批,被拒时告知用户并提替代方案。\n"
    "• **📂 指定输入文件**:任务消息含 `## 📂 本步骤指定输入文件` 块时,"
    "read_file 列表里的路径即可(框架已准备好上下游)。列表不足回复『缺少 X 文件』。\n"
    "• **📦 交付契约**:任务可能带 `output_files` + `must_contain` + "
    "`min_lines`,框架在 write_file 后自动校验并以 ✅/❌ 注入 system message;"
    "全部 ✅ 才能 finalize_step / complete_step。"
)

_TOOL_RULES_EN = (
    "## Tool-call mechanics\n"
    "• **Parallel-return**: independent tool_calls go in ONE response "
    "(framework runs them in parallel); serialize only when a later "
    "tool's args depend on an earlier result.\n"
    "• **plan_update**: `action='create_plan'` builds a plan; each "
    "step needs an `acceptance` field; `complete_step`'s "
    "`result_summary` must reference it. The TODOs panel auto-renders "
    "the plan.\n"
    "• **team_create**: spawn parallel sub-agents for independently-"
    "decomposable subtasks.\n"
    "• **Approval**: bash / sensitive writes may need approval; on "
    "denial, tell the user and propose an alternative.\n"
    "• **📂 Pinned input files**: When the task message contains a "
    "`## 📂 Pinned input files` block, read_file the listed paths "
    "(framework already prepared upstream context). If the list is "
    "insufficient, reply \"missing file X\".\n"
    "• **📦 Deliverable contract**: A task may carry `output_files` + "
    "`must_contain` + `min_lines`; framework auto-verifies after each "
    "write_file and injects ✅/❌ status. Only after every output_file "
    "is ✅ may you call finalize_step / complete_step."
)


# ── Continuous-execution contract (2026-05-28) ────────────────────
# @user: 小新 (mimo/deepseek) finishes ONE sub-task then stops —
# unlike Claude which autonomously drives a whole task to completion.
# Weaker models narrate-and-stop ("✓ task 1 done. Starting task 2:")
# then emit no tool_call → the agent loop ends → agent goes idle →
# the user (or a 5-min watchdog) has to re-prompt. This directive
# tells the model to KEEP GOING within the same turn. It's the
# cheapest of the continuity fixes (D); the runtime nudge (A) is the
# enforcement layer for models that ignore it.
_CONTINUITY_RULES_ZH = (
    "## 持续执行（重要）\n"
    "你是一个能自主完成整个任务的 agent，不是一问一答的助手。规则：\n"
    "• **禁止 narrate-and-stop**：不要说「让我…」「下一步我会…」「现在开始…」"
    "然后就停。要么**这一条回复里立即调用对应工具**，要么把任务做完。"
    "宣告意图但不动作 = 违规。\n"
    "• **做完一步立即续下一步**：完成一个子任务后，如果还有未完成的步骤/任务"
    "（计划里还有 open step、或 task 列表里还有 TODO/进行中的项），"
    "**在同一轮里直接继续下一个，不要停下来等用户确认**。\n"
    "• **只在这两种情况停**：(1) 所有步骤/任务**全部完成**；"
    "(2) 你**确实需要用户的输入**才能继续（缺信息、要决策、要授权）—— "
    "这时明确提出你需要什么，而不是含糊停住。\n"
    "• 完成判定要诚实：声称「完成」前，先用工具**验证**（读回文件 / 跑测试 / "
    "检查输出），不要凭记忆报「已完成」。\n"
    "• 卡住时也要动作：工具报错就修或换路，不要只输出文字描述错误然后停。"
)

# 2026-06-03 (after @user "目录规划的也乱糟糟"): without an explicit
# project-isolation rule, agents dump every new project's files into
# the workspace root. Real-world example: one workspace had 5 distinct
# projects (RPG game, PCI-DSS audit, project-mgmt tool, bug-hunt-toolkit,
# obsidian vault) all mixed together — same-named files collided
# (package.json / index.html / main.js for different projects), and the
# folder hierarchy was unnavigable. This rule tells the agent up front.
_WORKSPACE_HYGIENE_ZH = (
    "## 工作区组织（每个项目独立子目录）\n"
    "你的 workspace 根目录 (~/.tudou_claw/workspaces/agents/<id>/workspace) "
    "是所有项目共享的根。**每个独立项目必须放在自己的子目录里**,不要在根目录"
    "直接创建项目文件。规则:\n"
    "• **开新项目第一步**: `mkdir -p <项目名>/ && cd <项目名>/`,所有后续工作在子目录内。"
    "项目名用 snake_case 英文(如 `marketing_skill/`, `pm_tool/`),避免中文/空格/特殊字符。\n"
    "• **绝不在 workspace 根直接放**: `package.json` / `index.html` / `src/` / "
    "`main.js` / `Constants.js` 这类项目文件。根目录只允许:平台 stub 文件 "
    "(Project.md / Tasks.md / Scheduled.md / Skills.md / STATUS.md / README.md)、"
    "跨项目共享笔记 (`obsidian-vault/` 之类的知识库)。\n"
    "• **开新项目前先看根目录**: `glob_files(pattern='*', path='.')` 或 `bash ls`,"
    "确认要么用已有项目子目录,要么 mkdir 新的。不要假设 workspace 是空的。\n"
    "• **避免同名冲突**: 同一 workspace 里两个项目都叫 `package.json` / `main.js` "
    "→ 工具调用容易读错文件。子目录隔离是唯一的防护。\n"
    "• **临时文件也分目录**: 测试脚本放项目子目录的 `tmp/` 或 `scratch/`,"
    "不要 `test1.py` `debug.py` 这种平铺在根。"
)

_WORKSPACE_HYGIENE_EN = (
    "## Workspace organization (one subdirectory per project)\n"
    "Your workspace root (~/.tudou_claw/workspaces/agents/<id>/workspace) "
    "is shared across ALL projects you ever work on. **Every distinct "
    "project MUST live in its own subdirectory** — don't dump files at "
    "the root. Rules:\n"
    "• **First step for any new project**: `mkdir -p <project>/ && "
    "cd <project>/`, then do ALL the work inside. Project names use "
    "snake_case English (e.g. `marketing_skill/`, `pm_tool/`); avoid "
    "spaces, Chinese, or special characters.\n"
    "• **NEVER put at the workspace root**: `package.json` / `index.html` "
    "/ `src/` / `main.js` / `Constants.js`-style project files. The root "
    "is reserved for: platform stub files (Project.md / Tasks.md / "
    "Scheduled.md / Skills.md / STATUS.md / README.md) and cross-project "
    "shared notes (`obsidian-vault/`-style knowledge bases).\n"
    "• **Before starting a new project, list the root**: "
    "`glob_files(pattern='*', path='.')` or `bash ls` — either reuse an "
    "existing project subdir or `mkdir` a new one. Don't assume the "
    "workspace is empty.\n"
    "• **Prevent name collisions**: two projects both with `package.json` "
    "or `main.js` at root will confuse every read/edit — subdirectory "
    "isolation is the only defense.\n"
    "• **Scratch files too**: put `test1.py` / `debug.py` in the project's "
    "`tmp/` or `scratch/`, not loose at the root."
)


_CONTINUITY_RULES_EN = (
    "## Continuous execution (important)\n"
    "You are an agent that autonomously drives a whole task to "
    "completion — not a one-shot Q&A assistant. Rules:\n"
    "• **No narrate-and-stop**: Never say \"Let me…\" / \"Next I'll…\" / "
    "\"Now starting…\" and then stop. Either **call the tool NOW in "
    "this same response**, or finish the task. Announcing intent "
    "without acting = violation.\n"
    "• **Finish one step, immediately continue the next**: After "
    "completing a sub-task, if there are still open steps/tasks "
    "(plan has open steps, or the task list has TODO/in-progress "
    "items), **continue to the next one in the SAME turn — do not "
    "stop to wait for user confirmation**.\n"
    "• **Stop ONLY when**: (1) ALL steps/tasks are done; or (2) you "
    "genuinely need user input to proceed (missing info, a decision, "
    "authorization) — then clearly state what you need instead of "
    "stopping vaguely.\n"
    "• Be honest about completion: before claiming \"done\", **verify** "
    "with a tool (read the file back / run tests / check output) — "
    "don't report \"completed\" from memory.\n"
    "• Act even when stuck: if a tool errors, fix it or change "
    "approach — don't just describe the error in text and stop."
)


# ── Knowledge & experience (Karpathy wiki pattern) ────────────────

# 2026-05-14 governance: signatures only. Detailed memory-fact-extraction
# policy lives in admin scene_prompt "Agent 记忆提取补充规则" (config.yaml).
_KNOWLEDGE_RULES_ZH = (
    "## 知识 / 经验工具\n"
    "• `wiki_ingest(kind, title, body, scope='role'|'global')`:"
    "kind ∈ experience | methodology | template | pattern | reference。\n"
    "• `knowledge_lookup(query)`:跨角色检索 wiki。\n"
    "• 装新能力 → 让用户从技能库 UI 安装。"
)

_KNOWLEDGE_RULES_EN = (
    "## Knowledge / experience tools\n"
    "• `wiki_ingest(kind, title, body, scope='role'|'global')`: "
    "kind ∈ experience | methodology | template | pattern | reference.\n"
    "• `knowledge_lookup(query)`: cross-role wiki search.\n"
    "• New capabilities → ask the user to install via Skill Registry UI."
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
# file-producing tools (write_file / create_pptx / etc.). English-only:
# even Chinese-content agents follow English instruction structure with
# high fidelity (Qwen / DeepSeek / GLM all train heavily on EN
# instruction-following data); previous version repeated the rules in
# Chinese as a "safety net", which was pure duplication (~500 chars/turn).
# Phrased positively where possible per prompt-engineering principle:
# "do X" gets higher adherence than "don't do Y".
# Single source of truth — agent.py and prompt_block_catalog.py both pull
# from here. DO NOT inline this string in callers.
_FILE_DISPLAY_LONG = (
    "<file_display>\n"
    "When you produce a file in your workspace (video, image, audio, "
    "document, archive, etc.), the portal automatically renders a "
    "clickable FileCard with filename, size, kind, and open action. "
    "Your reply only needs to summarize what the file is — the card "
    "handles delivery.\n"
    "Rules:\n"
    "  1. Reply with a short one-line summary; let the FileCard show "
    "the file. For images you MAY add markdown `![alt](path)` — "
    "optional, the card already has a thumbnail.\n"
    "  2. For non-image files (mp4, mp3, pdf, docx, zip), use plain "
    "text path references only. Markdown image syntax on these always "
    "renders as a broken image — DO NOT use it.\n"
    "  3. The card delivers the file. DO NOT instruct the user to "
    "drag, copy, or move the file manually, and DO NOT fabricate "
    "`/api/portal/attachment?path=...` URLs.\n"
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
    "展示本地图片/截图(你生成、下载、找到的 PNG/JPG/GIF/WEBP)给用户:\n"
    "  ![简短描述](路径)\n"
    "前端自动渲染成可点击放大的图片。\n"
    "• 路径优先用相对路径(`./blog-screenshot.png`),绝对路径也行,"
    "只要文件在你工作目录下。\n"
    "• 同时贴 `![](path)` + 文字说明,让用户能立刻看到 — 只说"
    "「文件保存在 xxx」用户看不到图。\n"
    "• 远端 http/https URL 也能直接写,渲染方式相同。\n"
    "• 支持格式:png / jpg / jpeg / gif / webp / svg / bmp / ico;"
    "其它类型走普通文件链接(参见 file_display)。\n"
    "</image_display>"
)

_IMAGE_DISPLAY_LONG_EN = (
    "<image_display>\n"
    "To show the user a local image/screenshot (PNG/JPG/GIF/WEBP you "
    "generated, downloaded, or found):\n"
    "  ![short description](path)\n"
    "The portal renders it inline as a clickable, zoomable image.\n"
    "• Prefer relative paths (`./blog-screenshot.png`); absolute paths "
    "are fine as long as the file lives in your workspace.\n"
    "• Always paste `![](path)` alongside any prose — the user can't "
    "see anything if you only say \"saved to xxx\".\n"
    "• Remote http/https URLs work the same way.\n"
    "• Supported: png / jpg / jpeg / gif / webp / svg / bmp / ico; "
    "other formats render as plain file links (see file_display).\n"
    "</image_display>"
)


# ── Attachment contract — for agents with messaging / send_* tools ──

_ATTACHMENT_CONTRACT_ZH = (
    "<attachment_contract>\n"
    "调用发送类工具(send_email / send_message / IM 发送)且本轮产出了"
    "文件 — 或用户明确要求发某个文件 — 时:\n"
    "  1. 把文件完整路径放进工具的 `attachments` 数组参数。\n"
    "  2. 邮件/消息正文里写文件名只是给人看的标注;附件能否送达完全"
    "由 `attachments` 数组决定,正文里写名字不会触发附件发送。\n"
    "  3. 工具有多个附件参数名(attachments / files / attach_paths)"
    "时,任选一个支持的填上,保持非空。\n"
    "  4. 不确定要不要带附件 → 先问用户。\n"
    "</attachment_contract>"
)

_ATTACHMENT_CONTRACT_EN = (
    "<attachment_contract>\n"
    "When you call a send-type tool (send_email / send_message / any "
    "IM send tool) AND you produced a file in this turn (PPT, doc, "
    "report, image, etc.) OR the user explicitly asked you to send a "
    "file:\n"
    "  1. Put the file's full path into the tool's `attachments` "
    "array parameter.\n"
    "  2. The filename in the email/message body is just a label for "
    "the human reader; whether the attachment is delivered is "
    "determined entirely by the `attachments` array — naming the "
    "file in prose does not trigger attachment.\n"
    "  3. If the tool exposes multiple attachment-like parameters "
    "(attachments / files / attach_paths), pick any supported one "
    "and keep it non-empty.\n"
    "  4. Unsure whether a file should be attached → ask the user.\n"
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

_PLAN_PROTOCOL_EN = (
    "## Plan & progress protocol\n"
    "For multi-step tasks (research + write, search + generate + send), "
    "output a plan block **before** executing:\n"
    "\n"
    "```\n"
    "📋 Plan\n"
    "1. [step 1] — tool: <tool_name>\n"
    "2. [step 2] — tool: <tool_name>\n"
    "```\n"
    "\n"
    "Rules:\n"
    "- Plan block only on first response; not repeated.\n"
    "- After each step, one line: `✓ Step N: <what you did>`.\n"
    "- Skip the plan for one-shot Q&A or chitchat.\n"
    "- Tool names must match actual calls (e.g. `web_search`, `write_file`).\n"
    "- 1–6 steps; don't over-decompose (\"search 3 sources\" = 1 step).\n"
    "\n"
    "This is purely for UI bucketing of tool calls — what you say and which "
    "tools you use don't change."
)


def select_plan_protocol(language: str) -> str:
    """Return the language-appropriate plan protocol text. EN agents used to
    receive ``_PLAN_PROTOCOL_ZH`` (the only version) which both wasted
    ~427 chars on Chinese rules they couldn't act on and was a correctness
    bug for English-only deployments. Default ZH for ``auto`` since
    TudouClaw is Chinese-first."""
    if (language or "").lower().startswith("en"):
        return _PLAN_PROTOCOL_EN
    return _PLAN_PROTOCOL_ZH


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
    # Continuous-execution contract (2026-05-28) — drives weak models
    # to keep working through a multi-step task instead of stopping
    # after each sub-task. See _CONTINUITY_RULES_ZH for rationale.
    # Default to ZH for 'auto' (same convention as select_plan_protocol):
    # this is a behavioral directive aimed at Chinese-first deployments
    # + Chinese models (mimo); ZH lands better than EN for them. Only
    # explicit en* agents get the EN form.
    _continuity_use_en = isinstance(language, str) and language.lower().startswith("en")
    parts.append(_CONTINUITY_RULES_EN if _continuity_use_en else _CONTINUITY_RULES_ZH)
    # Workspace hygiene (2026-06-03) — every project in its own subdir.
    # Same EN-vs-ZH selector logic as continuity.
    parts.append(_WORKSPACE_HYGIENE_EN if _continuity_use_en else _WORKSPACE_HYGIENE_ZH)
    parts.append(_KNOWLEDGE_RULES_ZH if use_zh else _KNOWLEDGE_RULES_EN)
    # NOTE: _FILE_DISPLAY (SHORT, ~410 chars) and _IMAGE_DISPLAY (SHORT,
    # ~220 chars) used to be appended here, but agent.py unconditionally
    # appends the LONG variants right after compose_full_prompt() returns.
    # Both LONG forms cover everything the SHORT forms said and more, so
    # emitting both was pure duplication (~625 chars wasted per turn).
    # Phase 2b dedup pulled these out as part of the prompt-size cleanup.

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
    filters by ``scope`` / ``roles``, labels each with a markdown
    ``## <name>`` header so the LLM can tell them apart.

    2026-05-14: switched from ``<system_prompt name="...">`` XML envelope
    to markdown headers. The XML form was being mimicked back into chat
    replies (LLM saw the outer envelope in its system message and
    produced ``<system_prompt name=\"my reply\">...`` in output).
    Markdown headers carry the same labeling intent without the
    pattern-mimic hazard.

    Returns "" when nothing is configured — caller should drop the
    empty string rather than emit blank lines.
    """
    cfg = _read_config()
    parts: list[str] = []

    # Legacy: global_system_prompt as the first block (back-compat)
    legacy = cfg.get("global_system_prompt") or ""
    if isinstance(legacy, str) and legacy.strip():
        parts.append(f"## Global Rules\n{legacy.strip()}")

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
            parts.append(f"## {name}\n{prompt}")
        else:
            parts.append(f"## System Rule\n{prompt}")

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

    Dedup (2026-04-28): historically many agents have ``system_prompt``
    and ``soul_md`` containing IDENTICAL text (the create-agent UI
    confused the two fields, so users pasted the same persona into both).
    Audit shows 4/6 production agents affected, wasting 290-941 tokens
    per chat per agent. When the two strings match, we emit ONE merged
    block instead of two duplicate sections.
    """
    parts: list[str] = []

    sp = (system_prompt or "").strip()
    sm = (soul_md or "").strip()
    ci = (custom_instructions or "").strip()

    if sp and sm and sp == sm:
        # Identical content in both fields — emit once with combined header.
        head = "## 身份与行为方式" if use_zh else "## Identity & Behavior"
        parts.append(f"{head}\n{sp}")
    else:
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
    handoff_block = build_handoff_role_block(role, use_zh=use_zh)

    parts = [default_block]
    if settings_block:
        parts.append(settings_block)
    if handoff_block:
        parts.append(handoff_block)
    if persona_block:
        parts.append(persona_block)
    return "\n\n".join(parts)


# Phase 2 P2-3 (2026-05-06) — role-aware handoff guidance.
# Coordinator/PM-class roles should DISPATCH structured tasks to others.
# Worker-class roles should ACCEPT structured tasks and not write
# free-form 任务派发 markdown documents.
_PM_LIKE_ROLES = frozenset({
    "pm", "ceo", "cto", "manager", "coordinator", "lead", "architect",
    "product", "owner", "chief",
})
_WORKER_LIKE_ROLES = frozenset({
    "coder", "developer", "engineer", "designer", "tester", "qa",
    "reviewer", "researcher", "writer", "data", "devops", "analyst",
    "general",
})


def build_handoff_role_block(role: str, *, use_zh: bool = False) -> str:
    """Return role-specific handoff guidance, or '' for unknown roles.

    PM-class: instructed to use ``dispatch_task`` (structured) instead
    of writing 任务派发_X.md free-form documents.

    Worker-class: instructed to start each turn with
    ``inbox_assignments`` / ``accept_task`` and consume the structured
    brief, NOT to read free-form 任务派发 markdown.
    """
    r = (role or "").strip().lower()
    is_pm = any(tag in r for tag in _PM_LIKE_ROLES)
    is_worker = (not is_pm) and any(tag in r for tag in _WORKER_LIKE_ROLES)
    if not (is_pm or is_worker):
        return ""

    if use_zh:
        if is_pm:
            return (
                "## Handoff 角色规则 (你是协调/PM 角色)\n"
                "• 给其他 agent 派活,**必须**用 `dispatch_task(to_agent, brief, "
                "deliverables, context_refs)` 工具 — 结构化派单。\n"
                "• **禁止** write_file 写「任务派发_X.md」「指派_X.md」这类自由文本派单文档 —— "
                "下游会忽略并报错。\n"
                "• brief ≤500 字, 1-3 句; 每个 deliverable 必须给 path + must_contain "
                "+ min_lines, 否则下游无法验收。\n"
                "• 派单前先 `query_team_status` 看谁有空 (Watcher 可见)。"
            )
        return (
            "## Handoff 角色规则 (你是执行/Worker 角色)\n"
            "• Turn 开始时,**必须**先调 `inbox_assignments` 看是否有派单; "
            "有的话先 `accept_task` 接最高优先级那条。\n"
            "• 拿到 TaskAssignment 后,**只读** context_refs 列出的文件 (read_file) — "
            "**禁止** glob_files / find / search 探索其它文件。\n"
            "• **禁止** read_file「任务派发_X.md」这类历史派单文档 (已 deprecated, "
            "新派单走 dispatch_task)。\n"
            "• 写完所有 deliverables 后, 框架会自动校验 must_contain; "
            "全部 ✅ 才能 task_complete。"
        )
    if is_pm:
        return (
            "## Handoff Role Rules (you are a coordinator/PM-class role)\n"
            "• To assign work to another agent, **MUST** use `dispatch_task("
            "to_agent, brief, deliverables, context_refs)` — structured "
            "handoff.\n"
            "• **DO NOT** write_file free-form '任务派发_X.md' / "
            "'assignment_X.md' documents — downstream will ignore and "
            "report errors.\n"
            "• brief ≤ 500 chars, 1-3 sentences; every deliverable MUST "
            "have path + must_contain + min_lines or downstream cannot "
            "verify.\n"
            "• Before dispatching, call `query_team_status` to see who's "
            "available (when Watcher is in play)."
        )
    return (
        "## Handoff Role Rules (you are a worker/executor-class role)\n"
        "• At turn start, **MUST** call `inbox_assignments` to check for "
        "dispatched tasks; if any, call `accept_task` to take the "
        "highest-priority one.\n"
        "• Once you have a TaskAssignment, **read ONLY** the listed "
        "context_refs (via read_file) — **DO NOT** use glob_files / find "
        "/ search to discover other files.\n"
        "• **DO NOT** read_file '任务派发_X.md' / 'assignment_X.md' "
        "(deprecated; new assignments come via dispatch_task).\n"
        "• Once all deliverables are written, the framework auto-verifies "
        "must_contain; only when every output is ✅ may you task_complete."
    )


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
    # PART 4: handoff role guidance (Phase 2 P2-3)
    "build_handoff_role_block",
    # combined
    "compose_full_prompt",
    "compose_default_and_settings",   # back-compat alias
    # Phase 2b — extracted block-level constants / fns (for catalog reuse)
    # Note: these are also referenced by app.agent so refactor stays
    # single-source.
]
