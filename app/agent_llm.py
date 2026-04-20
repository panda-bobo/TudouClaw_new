"""
Agent LLM mixin — system prompt, context building, memory, and compression.

Extracted from agent.py to reduce file size.  The Agent dataclass inherits
from this mixin so all ``self.*`` references resolve at runtime.
"""
from __future__ import annotations
import hashlib
import json
import logging
import os
import subprocess as _sp
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger("tudou.agent")

# Lazy import marker (used by _get_memory_manager and others)
get_memory_manager = None
MemoryConfig = None
StepStatus = None
AgentPhase = None

# ---------------------------------------------------------------------------
# Token counting — tiktoken (accurate) with CJK-aware fallback
# ---------------------------------------------------------------------------
_tiktoken_enc = None  # Lazy-loaded tiktoken encoding
_tiktoken_available: bool | None = None


def _count_tokens(text: str) -> int:
    """Count tokens accurately with tiktoken, or use CJK-aware heuristic.

    CJK heuristic: each CJK character ≈ 1.5 tokens, each ASCII word ≈ 1.3 tokens.
    This is much more accurate than the old ``len(text) // 3`` for mixed CJK/EN text.
    """
    global _tiktoken_enc, _tiktoken_available
    if not text:
        return 0

    # Try tiktoken (lazy init, one-shot failure detection)
    if _tiktoken_available is None:
        try:
            import tiktoken
            _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
            _tiktoken_available = True
        except (ImportError, Exception):
            _tiktoken_available = False

    if _tiktoken_available and _tiktoken_enc is not None:
        return len(_tiktoken_enc.encode(text, disallowed_special=()))

    # CJK-aware heuristic fallback
    cjk_count = 0
    ascii_buf: list[str] = []
    ascii_words = 0
    for ch in text:
        cp = ord(ch)
        # CJK Unified Ideographs + common CJK ranges
        if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or
                0xF900 <= cp <= 0xFAFF or 0x20000 <= cp <= 0x2FA1F or
                0x3000 <= cp <= 0x303F or 0xFF00 <= cp <= 0xFFEF):
            cjk_count += 1
            if ascii_buf:
                ascii_words += 1
                ascii_buf.clear()
        elif ch.isspace():
            if ascii_buf:
                ascii_words += 1
                ascii_buf.clear()
        else:
            ascii_buf.append(ch)
    if ascii_buf:
        ascii_words += 1

    # CJK char ≈ 1.5 tokens on average, ASCII word ≈ 1.3 tokens
    return int(cjk_count * 1.5 + ascii_words * 1.3) + 4  # +4 for message overhead


def _ensure_str_content(content: Any) -> str:
    """Safely convert message content to string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Multi-part content (vision, audio, etc.)
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") == "text":
                    parts.append(part.get("text", ""))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return str(content) if content else ""


class AgentLLMMixin:
    """Mixin providing system prompt building, context management,
    memory operations, and context compression."""

    def _get_git_context(self) -> str:
        """Auto-inject git context: branch, status, recent commits."""
        wd = str(self._effective_working_dir())
        parts = []
        try:
            # Check if it's a git repo
            _sp.run(["git", "rev-parse", "--git-dir"],
                    cwd=wd, capture_output=True, timeout=3, check=True)
        except Exception:
            return ""  # Not a git repo

        cmds = {
            "branch": ["git", "branch", "--show-current"],
            "status": ["git", "status", "--short", "--branch"],
            "log": ["git", "log", "--oneline", "-5", "--no-decorate"],
            "diff_stat": ["git", "diff", "--stat", "HEAD"],
        }
        for label, cmd in cmds.items():
            try:
                r = _sp.run(cmd, cwd=wd, capture_output=True, text=True, timeout=5)
                if r.returncode == 0 and r.stdout.strip():
                    parts.append(f"[git {label}]\n{r.stdout.strip()}")
            except Exception:
                pass
        if not parts:
            return ""
        return "<git_context>\n" + "\n\n".join(parts) + "\n</git_context>"

    def _get_skill_context(self) -> str:
        """Load SKILL.md files from project directory for knowledge injection."""
        wd = self._effective_working_dir()
        skill_content = []
        # Look for SKILL.md in working dir and common locations
        skill_paths = [
            wd / "SKILL.md",
            wd / ".claude" / "SKILL.md",
            wd / ".claw" / "SKILL.md",
            wd / "docs" / "SKILL.md",
        ]
        # Also scan for skill files in .claude/skills/ directory
        skills_dir = wd / ".claude" / "skills"
        if skills_dir.is_dir():
            for skill_file in skills_dir.rglob("SKILL.md"):
                if skill_file not in skill_paths:
                    skill_paths.append(skill_file)
        # Same for .claw/skills/
        claw_skills_dir = wd / ".claw" / "skills"
        if claw_skills_dir.is_dir():
            for skill_file in claw_skills_dir.rglob("SKILL.md"):
                if skill_file not in skill_paths:
                    skill_paths.append(skill_file)

        for sp in skill_paths:
            if sp.exists() and sp.is_file():
                try:
                    content = sp.read_text(encoding="utf-8", errors="replace")[:3000]
                    rel_path = str(sp.relative_to(wd)) if sp.is_relative_to(wd) else str(sp)
                    skill_content.append(
                        f'<skill file="{rel_path}">\n{content}\n</skill>'
                    )
                except (OSError, ValueError):
                    pass
        if not skill_content:
            return ""
        return "\n".join(skill_content)

    def _get_agent_home(self) -> Path:
        """Return this agent's home directory under the node data root.

        Layout: ~/.tudou_claw/workspaces/agents/{agent_id}/
        """
        from . import DEFAULT_DATA_DIR
        data_dir = os.environ.get("TUDOU_CLAW_DATA_DIR") or DEFAULT_DATA_DIR
        return Path(data_dir) / "workspaces" / "agents" / self.id

    def _get_agent_workspace(self) -> Path:
        """Return this agent's workspace folder (where MD files live)."""
        return self._get_agent_home() / "workspace"

    def _effective_working_dir(self) -> Path:
        """Return the agent's effective working directory.

        If ``self.working_dir`` is set, use it. Otherwise fall back to the
        agent's private workspace under ``~/.tudou_claw/workspaces/agents/``.

        CRITICAL: never fall back to ``os.getcwd()`` / ``Path.cwd()`` — that
        would leak runtime files into the server-process CWD, which is
        typically the code package directory (e.g.
        ``/Users/.../AIProjects/TudouClaw``). The code tree must never
        receive runtime artefacts.
        """
        if self.working_dir:
            try:
                return Path(self.working_dir)
            except Exception:
                pass
        try:
            return self._ensure_workspace_layout()
        except Exception:
            return self._get_agent_workspace()

    @staticmethod
    def get_shared_workspace_path(project_id: str) -> str:
        """Return the shared workspace path for a project.

        Layout: ~/.tudou_claw/workspaces/shared/{project_id}/
        """
        from . import DEFAULT_DATA_DIR
        data_dir = os.environ.get("TUDOU_CLAW_DATA_DIR") or DEFAULT_DATA_DIR
        return str(Path(data_dir) / "workspaces" / "shared" / project_id)

    def _ensure_workspace_layout(self) -> Path:
        """Create the standard agent directory layout and seed MD templates.

        Layout created:
            {agent_home}/workspace/{Scheduled.md, Tasks.md, Project.md}
            {agent_home}/workspace/shared -> {shared_workspace} (symlink if part of project)
            {agent_home}/{session, memory, logs}/
        Returns the workspace path.
        """
        home = self._get_agent_home()
        ws = home / "workspace"
        try:
            for sub in (ws, home / "session", home / "memory", home / "logs"):
                sub.mkdir(parents=True, exist_ok=True)
        except Exception:
            return ws

        # Create shared workspace symlink if agent is part of a project
        if self.shared_workspace:
            try:
                shared_link = ws / "shared"
                if shared_link.exists() or shared_link.is_symlink():
                    if shared_link.resolve() != Path(self.shared_workspace).resolve():
                        shared_link.unlink()
                        shared_link.symlink_to(self.shared_workspace)
                else:
                    shared_link.symlink_to(self.shared_workspace)
            except Exception:
                pass  # Silently fail on symlink creation (may not be supported on all systems)

        # --- Scheduled.md ---
        sched = ws / "Scheduled.md"
        if not sched.exists():
            sched.write_text(
                "# Scheduled Tasks — Agent: " + (self.name or self.id) + "\n\n"
                "Recurring and scheduled tasks owned by this agent. The agent loads "
                "this file at the start of every conversation, uses it as the "
                "source of truth for what to run daily/weekly/monthly, and appends "
                "new entries here whenever the user asks to schedule something.\n\n"
                "## Format\n\n"
                "```\n"
                "### <short title>\n"
                "- id: <task_id>            # filled after task_update create\n"
                "- recurrence: daily|weekly|monthly|cron|once\n"
                "- spec: HH:MM  OR  DOW HH:MM  OR  D HH:MM  OR  cron expr\n"
                "- status: active|paused|done\n"
                "- last_run: <ISO timestamp or ->\n"
                "- next_run: <ISO timestamp or ->\n"
                "- description: |\n"
                "    what the agent should do when this fires.\n"
                "```\n\n"
                "## Active Schedules\n\n"
                "<!-- Agent appends entries below this line -->\n",
                encoding="utf-8")

        # --- Tasks.md ---
        tasks_md = ws / "Tasks.md"
        if not tasks_md.exists():
            tasks_md.write_text(
                "# Tasks — Agent: " + (self.name or self.id) + "\n\n"
                "Ad-hoc and one-off tasks. Use this for work items that are NOT "
                "recurring (recurring tasks go in Scheduled.md).\n\n"
                "## Format\n\n"
                "```\n"
                "- [ ] <task_id> — <title> (priority, deadline)\n"
                "    description / context\n"
                "```\n\n"
                "Mark done with `[x]` and optionally add `→ result: ...`.\n\n"
                "## Open\n\n"
                "<!-- Agent appends open tasks here -->\n\n"
                "## Done\n\n"
                "<!-- Agent moves completed tasks here -->\n",
                encoding="utf-8")

        # --- Project.md (seed once; user/agent curate over time) ---
        proj_md = ws / "Project.md"
        if not proj_md.exists():
            proj_md.write_text(
                "# Project — Agent: " + (self.name or self.id) + "\n\n"
                "Long-lived project context, goals, constraints, and decisions "
                "this agent is working on. Persists across conversations.\n\n"
                "## Role\n\n"
                f"- Role: {self.role}\n"
                f"- Expertise: {', '.join(self.profile.expertise) or '(not set)'}\n"
                f"- Skills: {', '.join(self.profile.skills) or '(not set)'}\n\n"
                "## Goals\n\n"
                "<!-- Summarize the user's longer-term objectives here -->\n\n"
                "## Constraints / Conventions\n\n"
                "<!-- Style, tech stack, deadlines, language, tone... -->\n\n"
                "## Key Decisions\n\n"
                "<!-- Notable decisions made so the agent can stay consistent -->\n",
                encoding="utf-8")

        # --- skills/ directory (for granted skill packages) ---
        skills_dir = ws / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)

        # --- Skills.md (auto-refreshed: reflects loaded enhancement presets) ---
        skills_md = ws / "Skills.md"
        try:
            lines = ["# Skills — Agent: " + (self.name or self.id), ""]
            lines.append("Auto-generated summary of skill presets loaded on this agent. "
                         "Regenerated every time the agent starts. Do NOT hand-edit — "
                         "manage skills via the Portal (Skills Library) or the "
                         "`enable_enhancement` API.")
            lines.append("")
            if self.enhancer and getattr(self.enhancer, "enabled", False):
                domain = getattr(self.enhancer, "domain", "") or "custom"
                lines.append(f"## Loaded ({domain})")
                lines.append("")
                knows = getattr(self.enhancer, "knowledge", None)
                n_know = len(knows.entries) if knows and hasattr(knows, "entries") else 0
                patterns = getattr(self.enhancer, "reasoning", None)
                n_pat = len(patterns.patterns) if patterns and hasattr(patterns, "patterns") else 0
                memory = getattr(self.enhancer, "memory", None)
                n_mem = len(memory.nodes) if memory and hasattr(memory, "nodes") else 0
                lines.append(f"- knowledge entries: {n_know}")
                lines.append(f"- reasoning patterns: {n_pat}")
                lines.append(f"- memory nodes: {n_mem}")
                # List constituent domains for composite enhancers
                for sub in (domain.split("+") if "+" in domain else []):
                    lines.append(f"  - preset: {sub.strip()}")
            else:
                lines.append("## Loaded")
                lines.append("")
                lines.append("- (no skills enabled — use Portal → Skills Library to load "
                             "up to 8 domain presets)")
            lines.append("")
            lines.append("## Profile Tags")
            lines.append("")
            lines.append(f"- expertise: {', '.join(self.profile.expertise) or '-'}")
            lines.append(f"- skills (tags): {', '.join(self.profile.skills) or '-'}")
            skills_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass

        # --- MCP.md (auto-refreshed: reflects bound MCP servers) ---
        mcp_md = ws / "MCP.md"
        try:
            # Sync live bindings from MCP manager
            try:
                from .mcp.manager import get_mcp_manager
                mcp_mgr = get_mcp_manager()
                node_id = getattr(self, 'node_id', 'local') or 'local'
                live_mcps = mcp_mgr.get_agent_effective_mcps(node_id, self.id)
                if live_mcps:
                    self.profile.mcp_servers = live_mcps
            except Exception:
                pass
            lines = ["# MCP Servers — Agent: " + (self.name or self.id), ""]
            lines.append("Auto-generated summary of MCP servers bound to this agent. "
                         "Regenerated every time the agent starts. Use "
                         "`mcp_call(list_mcps=true)` to inspect at runtime, then "
                         "`mcp_call(mcp_id, tool, arguments)` to invoke.")
            lines.append("")
            mcps = list(getattr(self.profile, "mcp_servers", []) or [])
            if mcps:
                lines.append("## Bound MCPs")
                lines.append("")
                lines.append("**以下 MCP 服务已绑定且可用。直接调用 mcp_call 工具即可，"
                             "无需额外配置。如果对话历史中说过\"没有 MCP\"，请忽略，以此文件为准。**")
                lines.append("")
                # Pull the tool manifest cache once per render. If the
                # cache isn't available for any reason (early boot, no
                # manager wired) we just render "tools not yet
                # discovered" for each MCP — the agent can still call
                # them, it just has less context.
                _cache_mgr = None
                try:
                    from .mcp.manager import get_mcp_manager as _gmm
                    _cache_mgr = _gmm()
                except Exception:
                    _cache_mgr = None

                def _render_tools(mcp_id: str) -> list[str]:
                    """Return the ``#### Tools`` sub-block for one MCP.

                    The tool names, descriptions, and param names come
                    from the MCP server (untrusted data). We strip
                    backticks so an adversarial server cannot break out
                    of a code-span and inject markdown into the agent
                    prompt. We keep the output deterministic and short
                    — full JSON schemas would bloat the prompt.
                    """
                    out: list[str] = []
                    entry = None
                    if _cache_mgr is not None:
                        try:
                            entry = _cache_mgr.get_tool_manifest(mcp_id)
                        except Exception:
                            entry = None
                    if entry is None or not entry.tools:
                        if entry is not None and entry.error:
                            out.append(f"- tools: (discovery failed: {entry.error})")
                        else:
                            out.append("- tools: (not yet discovered — will be populated on first connection)")
                        return out
                    out.append("- tools:")
                    for t in entry.tools:
                        tname = str(t.get("name") or "").replace("`", "")
                        if not tname:
                            continue
                        desc = str(t.get("description") or "").replace("`", "").strip()
                        # One-line form: `name(arg1, arg2) — description`
                        schema = t.get("inputSchema") or {}
                        props = schema.get("properties") if isinstance(schema, dict) else None
                        required = set(schema.get("required") or []) if isinstance(schema, dict) else set()
                        arglist = ""
                        if isinstance(props, dict):
                            parts = []
                            for pname, pspec in list(props.items())[:6]:
                                pname_clean = str(pname).replace("`", "")
                                if pname in required:
                                    parts.append(pname_clean)
                                else:
                                    parts.append(f"{pname_clean}?")
                            if len(props) > 6:
                                parts.append("...")
                            arglist = "(" + ", ".join(parts) + ")"
                        # Truncate description to keep prompts tight
                        desc_short = (desc[:120] + "…") if len(desc) > 120 else desc
                        suffix = f" — {desc_short}" if desc_short else ""
                        out.append(f"  - `{tname}{arglist}`{suffix}")
                    if entry.error:
                        out.append(f"- ⚠️ last refresh failed: {entry.error} "
                                   f"(showing previously-discovered tools)")
                    return out

                for m in mcps:
                    status = "enabled" if getattr(m, "enabled", True) else "disabled"
                    lines.append(f"### {getattr(m, 'name', '') or m.id}")
                    lines.append(f"- id: `{m.id}`")
                    lines.append(f"- transport: {getattr(m, 'transport', 'stdio')}")
                    lines.append(f"- status: {status}")
                    cmd = getattr(m, "command", "") or getattr(m, "url", "")
                    if cmd:
                        lines.append(f"- endpoint: `{cmd}`")
                    # Show configured env vars (keys only, no values for security)
                    env_vars = getattr(m, 'env', {}) or {}
                    if env_vars:
                        env_keys = ", ".join(sorted(env_vars.keys()))
                        lines.append(f"- configured_env: `{env_keys}`")
                        lines.append(f"- ⚠️ 凭据已配置完毕，可直接使用，无需再问用户要密码或配置")
                    # Tool manifest — this is the fix for the class of
                    # bugs where the agent had to guess tool names.
                    lines.extend(_render_tools(m.id))
                    lines.append("")
            else:
                lines.append("## Bound MCPs")
                lines.append("")
                lines.append("- (none — bind MCPs via Portal → MCP Manager, e.g. email, "
                             "slack, github, postgres)")
            mcp_md.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except Exception:
            pass

        return ws

    # ── Skill package sync (grant → copy to agent workspace) ──

    def sync_skill_to_workspace(self, install: Any) -> dict:
        """Copy the full skill package to this agent's workspace/skills/<name>/.

        Called when a skill is granted. Copies SKILL.md, scripts/, reference
        MDs, and any other files from the global install_dir into the
        agent-local skills directory so the agent can ``cd`` into it and
        run scripts directly.

        Also auto-adds a capability entry (``<name>:rw``) to
        ``profile.skill_capabilities`` if not already present.

        Args:
            install: A ``SkillInstall`` instance (from skills/engine.py).

        Returns:
            dict with ``ok``, ``skill_dir``, ``files_copied``, ``capability``.
        """
        import shutil as _shutil

        name = getattr(install, "manifest", None)
        skill_name = getattr(name, "name", "") if name else ""
        if not skill_name:
            skill_name = getattr(install, "id", "unknown")
        src = Path(getattr(install, "install_dir", ""))
        if not src.is_dir():
            return {"ok": False, "error": f"source install_dir not found: {src}"}

        ws = self._get_agent_workspace()
        dest = ws / "skills" / skill_name
        try:
            if dest.exists():
                _shutil.rmtree(dest)
            _shutil.copytree(str(src), str(dest))
            # Fix permissions — source files may be read-only
            for fp in dest.rglob("*"):
                try:
                    fp.chmod(0o644 if fp.is_file() else 0o755)
                except Exception:
                    pass
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

        # Count files copied
        files_copied = [str(f.relative_to(dest)) for f in dest.rglob("*") if f.is_file()]

        # Auto-add capability
        cap = f"{skill_name}:rw"
        if cap not in self.profile.skill_capabilities:
            self.profile.skill_capabilities.append(cap)

        logger.info("sync_skill_to_workspace: %s → %s (%d files)",
                     skill_name, dest, len(files_copied))
        return {
            "ok": True,
            "skill_name": skill_name,
            "skill_dir": str(dest),
            "files_copied": files_copied,
            "capability": cap,
        }

    def remove_skill_from_workspace(self, skill_name: str) -> dict:
        """Remove a skill package from this agent's workspace on revoke.

        Also removes the corresponding capability from
        ``profile.skill_capabilities``.

        Args:
            skill_name: The skill name (directory name under workspace/skills/).

        Returns:
            dict with ``ok`` and details.
        """
        import shutil as _shutil

        ws = self._get_agent_workspace()
        dest = ws / "skills" / skill_name
        removed_files = 0
        if dest.exists():
            try:
                removed_files = sum(1 for f in dest.rglob("*") if f.is_file())
                _shutil.rmtree(dest)
            except Exception as exc:
                return {"ok": False, "error": str(exc)}

        # Remove capability
        cap = f"{skill_name}:rw"
        if cap in self.profile.skill_capabilities:
            self.profile.skill_capabilities.remove(cap)

        logger.info("remove_skill_from_workspace: %s removed (%d files)",
                     skill_name, removed_files)
        return {"ok": True, "skill_name": skill_name, "removed_files": removed_files}

    def get_skill_workspace_dir(self, skill_name: str) -> Path | None:
        """Return the agent-local skill directory if it exists."""
        ws = self._get_agent_workspace()
        d = ws / "skills" / skill_name
        return d if d.is_dir() else None

    def _get_scheduled_context(self) -> str:
        """Load Scheduled.md / Tasks.md / Project.md and inject into system prompt."""
        try:
            ws = self._ensure_workspace_layout()
        except Exception:
            return ""
        blocks = []
        for fname, tag in (("Project.md", "project"),
                           ("Skills.md", "skills"),
                           ("MCP.md", "mcp_servers"),
                           ("Tasks.md", "tasks"),
                           ("Scheduled.md", "scheduled_tasks"),
                           ("ActiveThinking.md", "active_thinking")):
            fp = ws / fname
            if not fp.exists():
                continue
            try:
                content = fp.read_text(encoding="utf-8", errors="replace")[:4000]
            except OSError:
                continue
            blocks.append(f'<{tag} file="workspace/{fname}">\n{content}\n</{tag}>')
        if not blocks:
            return ""
        header = (
            f"\n[Agent workspace: {ws}]\n"
            "工作区规范 / WORKSPACE CONVENTIONS:\n"
            "- workspace/Project.md — 长期目标、约束、关键决策（稳定，更新谨慎）\n"
            "- workspace/Skills.md — 当前加载的 skill presets（自动生成，勿手改）\n"
            "- workspace/MCP.md — 当前绑定的 MCP servers（自动生成，勿手改）\n"
            "- workspace/Tasks.md — 一次性任务列表\n"
            "- workspace/Scheduled.md — 周期性定时任务\n"
            "- workspace/ActiveThinking.md — 主动思考记录（自动生成）\n"
            "- ../session/ ../memory/ ../logs/ — 会话、记忆、日志目录\n\n"
            "重要 / IMPORTANT:\n"
            "- 生成的报告、文档、输出文件 MUST 放在 workspace/ 目录下（或其子目录），"
            "不要放到 agent home 或节点根目录下。\n"
            "- 用户说\"每天/每周/每月X点做Y\"时，你 MUST:\n"
            "  1) 先**立即执行一次**该任务（搜索、整理、发邮件等），让用户马上看到结果；\n"
            "  2) 然后调用 task_update (action=create, recurrence=daily|weekly|monthly, "
            "recurrence_spec=HH:MM) 创建定时任务，后续由调度器自动触发；\n"
            "  3) 用 edit_file/write_file 把新条目追加到 "
            "workspace/Scheduled.md 的 ## Active Schedules 段落之下。\n"
            "  除非用户明确说\"从明天开始\"或\"不用现在执行\"，否则必须先执行再建定时。\n"
            "- 一次性任务写进 workspace/Tasks.md 的 ## Open。完成后移到 ## Done。\n"
            "- 不要回答\"我做不到定时任务\"——调度器会在 next_run_at 自动触发。\n"
            "- 需要发邮件/发消息/调外部服务时，先 mcp_call(list_mcps=true) 查看绑定的 MCP，"
            "再 mcp_call(mcp_id, tool, arguments) 调用。\n"
            "\n⚠️ 配置冲突覆盖规则 / CONFIG OVERRIDE RULE:\n"
            "以下工作区文件反映的是**当前最新的实时配置**，每次对话都会重新生成。"
            "如果对话历史中有旧的信息（例如之前说过\"没有 MCP\"或\"工具不可用\"），"
            "但工作区文件显示已经有了，**以工作区文件为准**，忽略历史中的过时描述。"
            "直接使用最新配置执行任务，不要再说\"不可用\"。\n")
        return header + "\n" + "\n".join(blocks)

    # ------------------------------------------------------------------
    # System prompt: split into STATIC (cached) + DYNAMIC (per-call)
    # ------------------------------------------------------------------

    # Bump this whenever _build_static_system_prompt's body changes
    # in a way that should invalidate every cached prompt — e.g.
    # adding/editing a system-wide section like <file_display>.
    # This guarantees cache freshness even when no profile field
    # changed between two code versions running in the same process.
    _STATIC_PROMPT_BUILD_VERSION = "v4-project_context_freshness"

    def _get_global_system_prompt(self) -> str:
        """Legacy compat — returns empty if global_system_prompt migrated to scene_prompts."""
        try:
            from . import llm as _llm
        except Exception:
            try:
                from app import llm as _llm  # type: ignore
            except Exception:
                return ""
        try:
            cfg = _llm.get_config()
        except Exception:
            return ""
        val = cfg.get("global_system_prompt", "") if isinstance(cfg, dict) else ""
        return val.strip() if isinstance(val, str) else ""

    def _get_scene_prompts_text(self) -> str:
        """Build unified system prompts text from scene_prompts + legacy global_system_prompt."""
        try:
            from . import llm as _llm_mod
            cfg = _llm_mod.get_config()
        except Exception:
            return ""
        parts = []

        # Legacy: if global_system_prompt still has content, include it first
        global_sp = ""
        try:
            val = cfg.get("global_system_prompt", "")
            global_sp = val.strip() if isinstance(val, str) else ""
        except Exception:
            pass
        if global_sp:
            parts.append(f"<system_prompt name=\"Global Rules\">\n{global_sp}\n</system_prompt>")

        # System prompts (unified list) — filter by scope/role
        agent_role = getattr(self, "role", "") or ""
        scene_prompts = cfg.get("scene_prompts", [])
        for sp in scene_prompts:
            if not isinstance(sp, dict):
                continue
            if not sp.get("enabled", True):
                continue
            scope = sp.get("scope", "all")
            if scope == "roles":
                allowed_roles = sp.get("roles", [])
                if agent_role not in allowed_roles:
                    continue
            name = sp.get("name", "").strip()
            prompt = sp.get("prompt", "").strip()
            if not prompt:
                continue
            if name:
                parts.append(f"<system_prompt name=\"{name}\">\n{prompt}\n</system_prompt>")
            else:
                parts.append(f"<system_prompt>\n{prompt}\n</system_prompt>")

        return "\n\n".join(parts) if parts else ""

    def _compute_static_prompt_hash(self) -> str:
        """Compute a lightweight hash of inputs that affect the static prompt.

        If this hash hasn't changed, the cached static prompt is still valid.
        """
        p = self.profile
        parts = [
            self._STATIC_PROMPT_BUILD_VERSION,
            self.name, self.role, self.model or "",
            self.system_prompt or "",
            p.personality, p.communication_style,
            ",".join(p.expertise), ",".join(p.skills),
            p.language or "", p.custom_instructions or "",
            self.working_dir or "",
            self.shared_workspace or "",
            self.project_id or "",
            self.project_name or "",
            self.soul_md or "",
            self._get_global_system_prompt(),
            self._get_scene_prompts_text(),
        ]
        # ── Project-context freshness ───────────────────────────────
        # Include mtime of PROJECT_CONTEXT.md (and legacy siblings) from
        # every searched directory so edits invalidate the cached prompt
        # WITHOUT requiring an agent restart.  Cheap stat() calls, no read.
        try:
            from pathlib import Path as _HPath
            _hdirs: list[str] = []
            if self.working_dir:
                _hdirs.append(self.working_dir)
            if self.shared_workspace and self.shared_workspace != self.working_dir:
                _hdirs.append(self.shared_workspace)
            for _d in _hdirs:
                for _n in ("PROJECT_CONTEXT.md", "TUDOU_CLAW.md",
                           "CLAW.md", "README.md"):
                    try:
                        _f = _HPath(_d) / _n
                        if _f.exists():
                            parts.append(f"{_n}@{_d}:{_f.stat().st_mtime_ns}")
                    except OSError:
                        continue
        except Exception:
            pass
        return hashlib.md5("|".join(parts).encode()).hexdigest()

    def _build_static_system_prompt(self) -> str:
        """Build the STATIC portion of the system prompt.

        This includes: identity, personality, tools description, language,
        custom instructions, project context files (TUDOU_CLAW.md etc.), and
        model-specific guidance.  These change rarely (only on config edits).

        Cached via _cached_static_prompt / _static_prompt_hash.
        """
        from . import security
        from . import tools
        from . import llm

        current_hash = self._compute_static_prompt_hash()
        if self._cached_static_prompt and self._static_prompt_hash == current_hash:
            return self._cached_static_prompt

        p = self.profile
        wd = self._effective_working_dir()

        # --- Build prompt based on whether we have a rich persona ---
        if self.system_prompt and len(self.system_prompt) > 200:
            parts = [self.system_prompt]
            parts.append("")
            parts.append(
                "你可以使用以下工具：读写文件、运行 shell 命令、搜索代码、网络搜索、网页抓取。"
            )
            parts.append(
                "多智能体协作工具：team_create (创建子Agent并行执行任务), "
                "send_message (向其他Agent发送消息), task_update (更新共享任务列表)。"
            )
            parts.append(
                "执行计划工具：plan_update — 在开始执行任务时，务必先使用 plan_update(action='create_plan') "
                "创建一个执行计划，将任务分解为具体步骤。然后在每完成一个步骤时调用 "
                "plan_update(action='complete_step') 标记完成。这样用户可以实时看到你的进度。"
            )
            parts.append("用户让你操作文件或运行命令时，务必使用工具。")
            parts.append(
                "当任务可以分解为多个独立子任务时，使用 team_create 创建子Agent并行执行，"
                "这样3个子任务可以在~1分钟内完成，而非串行的3分钟。"
            )
            parts.append("")
            parts.append(
                "重要提示：某些工具调用（如修改系统的 bash 命令、写入敏感路径）"
                "可能需要人工审批。如果工具调用被拒绝，请告知用户并建议替代方案。"
            )
            parts.append("")
            parts.append(
                "【记忆/知识三件套 — 请严格分流，不要混用】\n"
                "1) skill（技能包）：可安装的能力包，由「技能库」UI 统一管理，"
                "位于 ~/.tudou_claw/skills/。agent 不要用工具去保存/创建 skill，"
                "也不要把复盘/经验内容写成 SKILL.md。\n"
                "2) experience（经验条目）：复盘(retrospective)或主动学习(active_learning) "
                "产出的 scene→核心知识→行动规则/禁忌规则 结构化经验，使用 save_experience "
                "工具写入你角色的经验库，之后会自动注入到同角色 agent 的系统提示里。\n"
                "3) knowledge（全局知识 wiki）：跨角色共享的参考资料（设计规范、技术栈、"
                "网站清单等），按需使用 knowledge_lookup 工具查询，不要复制其内容去创建 "
                "experience 或 skill。\n"
                "简单判断：想存『我下次遇到 X 场景应该怎么做』→ save_experience；"
                "想查『官方规范/已沉淀资料』→ knowledge_lookup；"
                "想加『可复用能力包』→ 让用户去技能库 UI 安装，不要自己建。"
            )
            if p.custom_instructions:
                parts.append("")
                parts.append(p.custom_instructions)
            if p.language and p.language != "auto":
                lang_map = {"zh-CN": "中文", "en": "English",
                            "ja": "日本語", "ko": "한국어", "es": "Español",
                            "fr": "Français", "de": "Deutsch"}
                lang_name = lang_map.get(p.language, p.language)
                parts.append(f"\n始终使用 {lang_name} 回复。")
        else:
            # Default build (no rich persona)
            parts = [
                f"You are {self.name}, an AI programming assistant.",
                f"Your role: {self.role}.",
            ]
            if p.personality != "helpful":
                parts.append(f"Your personality: {p.personality}.")
            if p.communication_style != "technical":
                parts.append(f"Your communication style: {p.communication_style}.")
            if p.expertise:
                parts.append(f"Your areas of expertise: {', '.join(p.expertise)}.")
            if p.skills:
                parts.append(f"Your specialized skills: {', '.join(p.skills)}.")
            if p.language and p.language != "auto":
                lang_map = {"zh-CN": "Chinese (Simplified)", "en": "English",
                            "ja": "Japanese", "ko": "Korean", "es": "Spanish",
                            "fr": "French", "de": "German"}
                lang_name = lang_map.get(p.language, p.language)
                parts.append(f"Always respond in {lang_name}.")
            parts.append("")
            parts.append(
                "You have access to tools for reading/writing files, running shell commands, "
                "searching code, web search, and web fetch."
            )
            parts.append(
                "Multi-agent coordination tools: team_create (spawn sub-agents for parallel "
                "task execution), send_message (inter-agent messaging), task_update (shared task list)."
            )
            parts.append(
                "Execution Plan tool: plan_update — at the START of any multi-step task, "
                "ALWAYS use plan_update(action='create_plan') to decompose the task into steps. "
                "Then call plan_update(action='complete_step') after each step completes. "
                "This lets the user see your real-time progress."
            )
            parts.append(
                "Always use tools when the user asks you to interact with files or run commands."
            )
            parts.append(
                "When a task can be decomposed into independent sub-tasks, use team_create to "
                "spawn sub-agents that execute in parallel (3 sub-agents ~1 min vs serial ~3 min)."
            )
            parts.append("Be concise and helpful. Use markdown formatting for code.")
            parts.append("")
            parts.append(
                "IMPORTANT: Some tool calls (especially bash commands that modify the system, "
                "writes to sensitive paths) may require human approval. If a tool call is denied, "
                "inform the user and suggest an alternative approach."
            )
            parts.append("")
            parts.append(
                "[Memory/Knowledge trio — keep these strictly separated]\n"
                "1) skill — installable capability package, managed exclusively via the Skill "
                "Registry UI (lives under ~/.tudou_claw/skills/). DO NOT use tools to create, "
                "save, or write SKILL.md. Never persist retrospective/experience content as a skill.\n"
                "2) experience — structured lesson (scene → core knowledge → action/taboo rules) "
                "produced by retrospectives or active learning. Use the save_experience tool; it "
                "writes to your role's experience library and is auto-injected into same-role prompts.\n"
                "3) knowledge — global reference wiki shared across roles (design specs, tech stack, "
                "site lists). Query on-demand via knowledge_lookup; do NOT copy its contents into "
                "experience or skill.\n"
                "Quick rule: 'next time I hit scene X, do Y' → save_experience; "
                "'look up official/standing reference' → knowledge_lookup; "
                "'install a reusable capability' → tell the user to install via the Skill Registry UI."
            )
            if self.system_prompt:
                parts.append("")
                parts.append(self.system_prompt)
            if p.custom_instructions:
                parts.append("")
                parts.append(p.custom_instructions)

        # File display contract — keeps the agent from writing broken
        # markdown image syntax for binary files, or "drag the file into
        # the chat" prose. The portal renders FileCards automatically
        # from the deliverable_dir, so the agent does not need to (and
        # must not) try to embed media inline in its reply text.
        parts.append("")
        parts.append(
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

        # Project context files — persistent project knowledge pinned into
        # every turn.  Modeled after Claude Code's CLAUDE.md convention:
        # drop a PROJECT_CONTEXT.md at the project root and its contents
        # automatically prime every agent that touches the directory.
        #
        # Search order:
        #   • working_dir (private)   + shared_workspace (team)
        #   • filename priority per dir: PROJECT_CONTEXT.md (canonical) >
        #     TUDOU_CLAW.md > CLAW.md > README.md  (first match per dir)
        #
        # Cost: lives in the STATIC system prompt, so it's captured by both
        # local KV-cache prefixes and Anthropic prompt-caching (2.2.5) —
        # after first turn it's effectively free tokens.
        from pathlib import Path as _PCPath
        _ctx_dirs: list[_PCPath] = [wd]
        if getattr(self, "shared_workspace", None):
            try:
                _sw = _PCPath(self.shared_workspace)
                if _sw.resolve() != wd.resolve():
                    _ctx_dirs.append(_sw)
            except (OSError, ValueError):
                pass
        _seen_paths: set[str] = set()
        for _dir in _ctx_dirs:
            for name in ("PROJECT_CONTEXT.md", "TUDOU_CLAW.md",
                         "CLAW.md", "README.md"):
                ctx_file = _dir / name
                try:
                    if not ctx_file.exists():
                        continue
                    _rp = str(ctx_file.resolve())
                    if _rp in _seen_paths:
                        continue
                    _seen_paths.add(_rp)
                    content = ctx_file.read_text(
                        encoding="utf-8", errors="replace")[:4000]
                    parts.append(
                        f"\n<project_context file=\"{name}\" "
                        f"dir=\"{_dir.name}\">\n{content}\n"
                        f"</project_context>"
                    )
                    break  # one file per directory (priority order)
                except OSError:
                    continue

        # Model-specific tool use guidance (depends on model, rarely changes)
        guidance = security.get_model_tool_guidance(self.model or "")
        if guidance:
            parts.append(guidance)

        is_zh = (self.system_prompt and len(self.system_prompt) > 200)
        # --- Workspace awareness: tell the Agent exactly where to write files ---
        ws_lines = []
        # Detect Chinese: use zh mode if system_prompt is rich CJK, or language is zh-CN
        use_zh = is_zh or (p.language and p.language.startswith("zh"))
        has_project = bool(self.shared_workspace and self.project_name)
        if use_zh:
            ws_lines.append("\n<workspace_context>")
            ws_lines.append(f"私有工作目录 (你自己的空间): {wd}")
            if has_project:
                ws_lines.append(f"项目共享目录 (团队共享): {self.shared_workspace}")
                ws_lines.append(f"所属项目: {self.project_name} (ID: {self.project_id})")
            ws_lines.append("")
            ws_lines.append("⚠️ 文件写入规则 (必须遵守):")
            if has_project:
                ws_lines.append(f"• 项目相关的代码/文件 → 写入项目共享目录: {self.shared_workspace}")
                ws_lines.append("  （其他 Agent 需要访问的文件必须放这里）")
                ws_lines.append(f"• 个人临时文件/草稿/日志 → 写入私有目录: {wd}")
                ws_lines.append("  （只有你自己会用到的文件放这里）")
                ws_lines.append("• 判断标准：这个文件是否需要被项目中其他 Agent 看到？")
                ws_lines.append("  - 需要 → 项目共享目录")
                ws_lines.append("  - 不需要 → 私有目录")
            else:
                ws_lines.append(f"• 所有文件操作在工作目录下进行: {wd}")
            ws_lines.append("• 使用相对路径（如 src/main.py）而非绝对路径。")
            ws_lines.append("• 创建子Agent (team_create) 时不要指定 working_dir，自动继承。")
            ws_lines.append("</workspace_context>")
        else:
            ws_lines.append("\n<workspace_context>")
            ws_lines.append(f"Private workspace (your own): {wd}")
            if has_project:
                ws_lines.append(f"Project shared directory (team): {self.shared_workspace}")
                ws_lines.append(f"Project: {self.project_name} (ID: {self.project_id})")
            ws_lines.append("")
            ws_lines.append("⚠️ File write rules (MUST follow):")
            if has_project:
                ws_lines.append(f"• Project code/files → write to shared dir: {self.shared_workspace}")
                ws_lines.append("  (Files other agents need access to MUST go here)")
                ws_lines.append(f"• Personal temp files/drafts/logs → write to private dir: {wd}")
                ws_lines.append("  (Files only you will use go here)")
                ws_lines.append("• Decision rule: Will other agents in this project need this file?")
                ws_lines.append("  - Yes → project shared directory")
                ws_lines.append("  - No  → private workspace")
            else:
                ws_lines.append(f"• All file operations within: {wd}")
            ws_lines.append("• Use relative paths (e.g., src/main.py), not absolute paths.")
            ws_lines.append("• When spawning sub-agents (team_create), do NOT set working_dir.")
            ws_lines.append("</workspace_context>")
        parts.append("\n".join(ws_lines))

        # --- Inline image display: tell the agent how to surface images ---
        # Portal chat renders markdown `![alt](path)` as an inline <img> by
        # routing the path through /api/portal/attachment. The agent doesn't
        # need to know that detail — just that emitting the markdown is the
        # correct way to show a picture in the reply.
        if use_zh:
            parts.append(
                "\n<image_display>\n"
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
        else:
            parts.append(
                "\n<image_display>\n"
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

        result = "\n".join(parts)

        # Prepend system prompts (unified: global + scene-based).
        # Goes at the very top so per-agent persona/system_prompt can still
        # override tone/identity in later sections.
        system_prompts_text = self._get_scene_prompts_text()
        if system_prompts_text:
            result = system_prompts_text + "\n\n" + result

        self._cached_static_prompt = result
        self._static_prompt_hash = current_hash
        logger.debug("Static system prompt rebuilt (hash=%s, len=%d, sys_prompts=%d)",
                     current_hash[:8], len(result), len(system_prompts_text))
        return result

    def _build_dynamic_context(self, current_query: str = "") -> str:
        """Build the DYNAMIC portion injected as a separate context message.

        This includes: git status, workspace/scheduled tasks, skill files,
        experience library, enhancement knowledge, and L2/L3 memory retrieval.
        These may change on every call, so they are kept separate from the
        static system prompt to preserve prompt caching.

        Budget-aware: limits total dynamic context to at most 30% of the
        context window, so conversation messages have room to breathe.
        """
        from . import llm

        context_limit = self._get_context_limit()
        static_prompt = self._cached_static_prompt or ""
        static_tokens = _count_tokens(static_prompt) if static_prompt else 0
        # Reserve at least 50% of context for conversation; 30% for dynamic context
        max_dynamic_tokens = max(200, (context_limit - static_tokens) * 3 // 10)
        # Heuristic: 1 token ≈ 2 chars for CJK-heavy, ≈ 4 chars for EN-heavy → use 3
        max_dynamic_chars = max_dynamic_tokens * 3

        parts = []
        total_chars = 0

        def _try_add(text: str) -> bool:
            """Add text to parts if within budget. Returns True if added."""
            nonlocal total_chars
            if not text:
                return False
            if total_chars + len(text) > max_dynamic_chars:
                # Try truncated version
                remaining = max_dynamic_chars - total_chars
                if remaining > 200:
                    parts.append(text[:remaining] + "\n...[truncated]")
                    total_chars = max_dynamic_chars
                return False
            parts.append(text)
            total_chars += len(text)
            return True

        # Priority order: most important context first

        # 0. Intent-aware context hint (from IntentResolver)
        _intent = getattr(self, "_last_resolved_intent", None)
        if _intent and _intent.confidence >= 0.6:
            _INTENT_HINTS = {
                "code_task": "用户意图: 代码任务。优先提供代码实现、示例和技术细节。",
                "query": "用户意图: 信息查询。简洁清晰地回答问题，提供关键信息。",
                "deployment": "用户意图: 部署/发布。关注环境、版本、回滚计划。",
                "communication": "用户意图: 沟通/通知。确保准确传达信息。",
                "file_operation": "用户意图: 文件操作。注意路径和权限。",
                "workflow": "用户意图: 工作流执行。关注流程步骤和依赖。",
                "task_management": "用户意图: 任务管理。关注优先级和状态。",
                "learning": "用户意图: 学习/研究。用通俗易懂的方式解释概念。",
                "configuration": "用户意图: 配置/设置。提供具体的配置项和值。",
            }
            hint = _INTENT_HINTS.get(_intent.category, "")
            if hint:
                # Add extracted slots if any
                _extracted = {k: v.value for k, v in _intent.slots.items()
                              if v.extracted and v.value}
                if _extracted:
                    slot_info = "; ".join(f"{k}={v}" for k, v in _extracted.items())
                    hint += f"\n提取参数: {slot_info}"
                _try_add(f"<intent_hint>\n{hint}\n</intent_hint>")

        # 0.5 RolePresetV2 Playbook injection —— 岗位做事逻辑 + 场景筛选规则
        # 只要 agent 的 V2 preset 有 playbook 就注入；Phase 2 核心行为改造点。
        try:
            from .role_preset_registry import get_registry as _get_role_reg
            from .playbook_runtime import build_playbook_context
            from .scope_detector import detect_scopes
            _role_id = getattr(self.profile, "role_preset_id", "") or ""
            if _role_id:
                _preset = _get_role_reg().get(_role_id)
                if _preset is not None and not _preset.playbook.is_empty():
                    _scopes = detect_scopes(current_query or "")
                    # 缓存供 Post-hook 复用
                    self._playbook_active_scopes = _scopes
                    self._playbook_active_preset_id = _role_id
                    _pb_ctx = build_playbook_context(_preset, _scopes)
                    if _pb_ctx:
                        _try_add(f"<playbook scope=\"{','.join(_scopes)}\">\n{_pb_ctx}\n</playbook>")
        except Exception as _e:
            # playbook 故障不应阻断聊天
            try:
                logger.debug("playbook injection skipped: %s", _e)
            except Exception:
                pass

        # 1. Shared Knowledge Wiki (lightweight title list)
        try:
            from . import knowledge as _kb
            kb_summary = _kb.get_prompt_summary()
            _try_add(kb_summary)
        except Exception:
            pass

        # 2. Workspace files (MCP, Tasks, Scheduled — needed for tool usage)
        sched_ctx = self._get_scheduled_context()
        _try_add(sched_ctx)

        # 3. Git context (with cooldown)
        now = time.time()
        if now - self._git_context_ts >= self._GIT_CONTEXT_COOLDOWN:
            self._cached_git_context = self._get_git_context()
            self._git_context_ts = now
        _try_add(self._cached_git_context)

        # 4. Three-layer memory: L2 + L3 retrieval (query-dependent)
        mm = self._get_memory_manager()
        if mm and current_query and total_chars < max_dynamic_chars:
            try:
                mem_config = self._get_memory_config()
                memory_context = mm.retrieve_for_prompt(
                    self.id, current_query, config=mem_config,
                )
                _try_add(memory_context or "")
                # ── 记录本次记忆注入的体量，供 portal 展示"记忆使用比例" ──
                try:
                    mem_chars = len(memory_context or "")
                    stats = getattr(self, "_memory_usage_stats", None)
                    if stats is None:
                        stats = {
                            "last_mem_chars": 0,
                            "last_total_chars": 0,
                            "last_budget": 0,
                            "last_ratio": 0.0,
                            "ema_ratio": 0.0,
                            "samples": 0,
                            "last_query_ts": 0.0,
                        }
                        self._memory_usage_stats = stats
                    stats["last_mem_chars"] = mem_chars
                    stats["last_budget"] = max_dynamic_chars
                    stats["last_query_ts"] = time.time()
                    # ratio = 记忆字符 / 动态上下文预算
                    ratio = mem_chars / max(max_dynamic_chars, 1)
                    stats["last_ratio"] = ratio
                    stats["samples"] += 1
                    # 指数移动平均，便于展示稳定的"近期记忆占用"
                    alpha = 0.3
                    stats["ema_ratio"] = (
                        alpha * ratio + (1 - alpha) * stats["ema_ratio"]
                    )
                except Exception as _se:
                    logger.debug("memory_usage_stats update failed: %s", _se)
            except Exception as e:
                logger.debug("Memory retrieval failed: %s", e)

        # 5. SKILL.md knowledge
        if total_chars < max_dynamic_chars:
            _try_add(self._get_skill_context())

        # 6. Enhancement module knowledge
        if total_chars < max_dynamic_chars and self.enhancer and self.enhancer.enabled:
            enhanced = self.enhancer.enhance_system_prompt("", context_hint=self.role)
            _try_add(enhanced or "")

        # 7. Self-improvement experience library
        if total_chars < max_dynamic_chars and self.self_improvement and self.self_improvement.enabled:
            exp_ctx = self.self_improvement.build_experience_context()
            _try_add(exp_ctx or "")

        # 8. Granted skills (from skill registry)
        if total_chars < max_dynamic_chars:
            try:
                import sys as _sys
                _llm_mod = _sys.modules.get(__package__ + ".llm") if __package__ else None
                hub = getattr(_llm_mod, "_active_hub", None) if _llm_mod else None
                if hub is not None and getattr(hub, "skill_registry", None) is not None:
                    skill_block = hub.skill_registry.build_prompt_block(
                        self.id, agent_workspace=str(self._get_agent_workspace()))
                    if skill_block:
                        _try_add(skill_block)
            except Exception as _se:
                logger.debug("skill prompt injection failed: %s", _se)

        if not parts:
            return ""
        result = "\n\n".join(parts)
        logger.debug("Dynamic context: %d chars / %d budget (%.0f%%)",
                     len(result), max_dynamic_chars,
                     len(result) / max(max_dynamic_chars, 1) * 100)
        # 顺便把"记忆 / 动态上下文实际占比"也算出来
        try:
            stats = getattr(self, "_memory_usage_stats", None)
            if stats is not None and stats.get("last_mem_chars", 0) > 0:
                stats["last_total_chars"] = len(result)
        except Exception:
            pass
        return result

    def _build_system_prompt(self) -> str:
        """Build full system prompt (backward compat — used by enable/disable methods).

        For the main chat loop, _ensure_system_message() uses the split
        static + dynamic approach instead.
        """
        static = self._build_static_system_prompt()
        dynamic = self._build_dynamic_context()
        if dynamic:
            return static + "\n\n" + dynamic
        return static

    def _get_memory_manager(self):
        """懒加载获取 MemoryManager 实例。"""
        if self._memory_manager is not None:
            return self._memory_manager
        if get_memory_manager is None:
            return None
        try:
            self._memory_manager = get_memory_manager()
            return self._memory_manager
        except Exception as e:
            logger.debug("Failed to init MemoryManager: %s", e)
            return None

    def _get_memory_consolidator(self):
        """懒加载获取 MemoryConsolidator 实例。"""
        if self._memory_consolidator is not None:
            return self._memory_consolidator
        mm = self._get_memory_manager()
        if mm is None:
            return None
        try:
            from .core.memory import MemoryConsolidator
        except ImportError:
            try:
                from app.core.memory import MemoryConsolidator
            except ImportError:
                return None
        self._memory_consolidator = MemoryConsolidator(mm)
        return self._memory_consolidator

    def _get_memory_config(self):
        """获取当前 agent 的记忆配置。"""
        mm = self._get_memory_manager()
        if mm is None or MemoryConfig is None:
            return None
        try:
            return mm.get_config(self.id)
        except Exception:
            return MemoryConfig() if MemoryConfig else None

    def _ensure_system_message(self, current_query: str = ""):
        """Ensure the system message is present AND up-to-date.

        Architecture for KV cache reuse (critical for LM Studio / Ollama):
          messages[0] = STATIC system prompt — only changes when config changes.
                        This ensures the prefix of the message array is STABLE,
                        so local inference servers can reuse their KV cache.

        Dynamic context (git, memory, experience) is NOT injected into the
        message array here.  Instead, it's injected as a transient message
        right before sending in the chat loop (see _inject_dynamic_context).
        This keeps self.messages stable between calls.
        """
        static_prompt = self._build_static_system_prompt()

        if not self.messages or self.messages[0].get("role") != "system":
            self.messages.insert(0, {"role": "system", "content": static_prompt})
        else:
            # Only update if actually changed (preserves KV cache prefix)
            if self.messages[0]["content"] != static_prompt:
                self.messages[0]["content"] = static_prompt

        # Clean up any old dynamic context messages left from previous versions
        for i in range(min(len(self.messages), 4) - 1, 0, -1):
            if self.messages[i].get("_dynamic"):
                self.messages.pop(i)

    def _inject_dynamic_context(self, messages: list[dict], current_query: str = "") -> list[dict]:
        """Inject dynamic context into a COPY of messages for sending to LLM.

        Dynamic context is appended at the END (right before the last user
        message) so the prefix stays stable for KV cache reuse.

        Returns a new list — does NOT modify self.messages.
        """
        dynamic_ctx = self._build_dynamic_context(current_query=current_query)
        if not dynamic_ctx:
            return messages

        # Find the last user message index to insert context before it
        last_user_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "user":
                last_user_idx = i
                break

        # Create a copy and inject
        result = list(messages)
        ctx_msg = {"role": "system", "content": dynamic_ctx, "_dynamic": True}
        if last_user_idx is not None and last_user_idx > 0:
            result.insert(last_user_idx, ctx_msg)
        else:
            # No user message found — append at end
            result.append(ctx_msg)
        return result

    def _memory_write_back(self, user_message: str, assistant_response: str):
        """
        三层记忆 write-back:
        1. 累计轮次计数，达到阈值时将溢出的 L1 消息压缩为 L2 摘要
        2. 从对话中提取 L3 事实（异步，不阻塞主流程）
        """
        mm = self._get_memory_manager()
        if mm is None:
            return

        mem_config = self._get_memory_config()
        if mem_config is None or not mem_config.enabled:
            return

        self._memory_turn_counter += 1

        try:
            # === L1→L2: 渐进式压缩溢出消息 ===
            # Dynamic threshold: increases after each compression to allow
            # longer uncompressed windows as conversation matures.
            # Level 0: compress at 15 turns, Level 1: at 20, Level 2+: at 25
            from .core.memory import _agent_compression_level
            _comp_level = _agent_compression_level.get(self.id, 0)
            _dynamic_threshold = mem_config.l2_compress_threshold + min(_comp_level * 5, 10)

            if self._memory_turn_counter >= _dynamic_threshold:
                overflow = mm.get_overflow_messages(
                    self.messages, max_turns=mem_config.l1_max_turns,
                )
                if overflow:
                    llm_call = self._make_summary_llm_call()
                    mm.compress_to_episodic(
                        agent_id=self.id,
                        messages=overflow,
                        llm_call=llm_call,
                        turn_start=max(0, self._memory_turn_counter - len(overflow)),
                    )
                    self._log("memory", {
                        "action": "compress_to_episodic",
                        "overflow_msgs": len(overflow),
                        "compression_level": _comp_level,
                        "threshold": _dynamic_threshold,
                    })
                self._memory_turn_counter = 0

            # === Feedback Learning: detect user corrections/preferences → L3 ===
            try:
                # Get previous assistant response for context
                prev_assistant = ""
                for _m in reversed(self.messages[:-2]):
                    if _m.get("role") == "assistant" and _m.get("content"):
                        prev_assistant = _ensure_str_content(_m["content"])
                        break
                feedback_llm = self._make_summary_llm_call()
                feedback_facts = mm.detect_and_learn_feedback(
                    agent_id=self.id,
                    user_message=user_message,
                    assistant_response=assistant_response,
                    prev_assistant=prev_assistant,
                    llm_call=feedback_llm,
                )
                if feedback_facts:
                    self._log("memory", {
                        "action": "feedback_learning",
                        "count": len(feedback_facts),
                        "facts": [f.content[:50] for f in feedback_facts[:3]],
                    })
            except Exception as _fb_err:
                logger.debug("Feedback learning failed: %s", _fb_err)

            # === L3: 提取事实 ===
            if mem_config.auto_extract_facts:
                llm_call = self._make_summary_llm_call()
                facts = mm.extract_facts(
                    agent_id=self.id,
                    user_message=user_message,
                    assistant_response=assistant_response,
                    llm_call=llm_call,
                    config=mem_config,
                )
                if facts:
                    self._log("memory", {
                        "action": "extract_facts",
                        "count": len(facts),
                        "facts": [f.content[:50] for f in facts[:3]],
                    })

            # === Session-level action buffer flush ===
            # Aggregate buffered tool actions into a single outcome memory
            # instead of recording per-tool log entries.
            try:
                llm_call_flush = self._make_summary_llm_call()
                outcome = mm.flush_action_buffer(self.id, llm_call=llm_call_flush)
                if outcome:
                    self._log("memory", {
                        "action": "flush_action_buffer",
                        "outcome": outcome.content[:100],
                    })
            except Exception as _flush_err:
                logger.debug("flush_action_buffer failed: %s", _flush_err)

            # === L3: 记忆整理 (Consolidate) ===
            consolidator = self._get_memory_consolidator()
            if consolidator:
                llm_call = self._make_summary_llm_call()
                report = consolidator.consolidate(
                    agent_id=self.id, llm_call=llm_call)
                if not report.get("skipped"):
                    total = (report.get("plans_resolved", 0)
                             + report.get("facts_merged", 0)
                             + report.get("facts_decayed", 0)
                             + report.get("facts_deleted", 0))
                    if total > 0:
                        self._log("memory", {
                            "action": "consolidate",
                            "plans_resolved": report.get("plans_resolved", 0),
                            "facts_merged": report.get("facts_merged", 0),
                            "facts_decayed": report.get("facts_decayed", 0),
                            "facts_deleted": report.get("facts_deleted", 0),
                        })
                        parts = []
                        if report.get("plans_resolved"):
                            parts.append(f"intent→outcome={report['plans_resolved']}")
                        if report.get("facts_merged"):
                            parts.append(f"merged={report['facts_merged']}")
                        if report.get("facts_decayed"):
                            parts.append(f"decayed={report['facts_decayed']}")
                        if report.get("facts_deleted"):
                            parts.append(f"deleted={report['facts_deleted']}")
                        self.history_log.add(
                            "consolidate",
                            f"[Consolidate] 记忆整理: {', '.join(parts)}"
                        )

        except Exception as e:
            logger.debug("Memory write-back failed: %s", e)

    def _sync_enhancement_to_memory(self, learn_result):
        """将 Enhancement 自我学习的成果同步到 L3 记忆。

        Enhancement 模块有自己的 MemoryGraph，但那个只用于增强 prompt。
        我们把关键的学习成果也写入 L3，使得向量搜索能检索到 Agent 的经验。
        """
        mm = self._get_memory_manager()
        if mm is None:
            return
        try:
            # learn_result 是 MemoryNode 对象
            title = getattr(learn_result, 'title', '') or ''
            content = getattr(learn_result, 'content', '') or ''
            kind = getattr(learn_result, 'kind', '') or ''

            if not content or len(content) < 10:
                return

            # 映射 Enhancement kind → L3 category
            kind_to_category = {
                "error_fix": "learned",
                "success_pattern": "learned",
                "observation": "learned",
                "knowledge": "context",
                "rule": "rule",
            }
            category = kind_to_category.get(kind, "learned")
            fact_content = f"[自我学习] {title}: {content}" if title else f"[自我学习] {content}"

            from .core.memory import SemanticFact
            fact = SemanticFact(
                agent_id=self.id,
                category=category,
                content=fact_content[:500],
                source="enhancement:auto_learn",
                confidence=0.7,
            )
            mm.save_fact(fact)
            logger.debug("Synced enhancement learning to L3 memory: %s", title[:60])
        except Exception as e:
            logger.debug("Enhancement→memory sync failed: %s", e)

    # 高价值工具 — 这些操作值得记录到 Agent 记忆
    _MEMORY_WORTHY_TOOLS = {
        # 文件操作
        "write_file": "写入文件",
        "edit_file": "编辑文件",
        "create_file": "创建文件",
        "delete_file": "删除文件",
        # 系统操作
        "bash": "执行命令",
        "bash_exec": "执行命令",
        # MCP 调用
        "mcp_call": "MCP工具调用",
        # 通信
        "send_message": "发送消息",
        "send_email": "发送邮件",
        # 工作流
        "task_update": "更新任务",
        "plan_update": "更新计划",
        # 代码操作
        "run_code": "运行代码",
        "deploy": "部署",
    }

    def _record_tool_action(self, tool_name: str, result_str: str):
        """将 Agent 的关键工具操作记录到 L3 记忆。

        只记录修改性操作 (写文件、执行命令、发消息等)，
        不记录查询性操作 (搜索、列表、状态查询等)。
        """
        if tool_name not in self._MEMORY_WORTHY_TOOLS:
            return
        mm = self._get_memory_manager()
        if mm is None:
            return
        mem_config = self._get_memory_config()
        if mem_config is None or not mem_config.enabled:
            return
        try:
            action_label = self._MEMORY_WORTHY_TOOLS[tool_name]
            # 从结果中提取关键摘要 (首行或前100字)
            summary_line = result_str.strip().split("\n")[0][:150] if result_str else ""
            # 过滤错误结果 (不记录 DENIED、Error 等)
            if summary_line.startswith(("DENIED:", "Error:", "error:", "Failed")):
                return
            mm.record_agent_action(
                agent_id=self.id,
                action_type="tool_exec",
                tool_name=tool_name,
                summary=f"{action_label}: {tool_name}",
                details=summary_line,
            )
        except Exception as e:
            logger.debug("Failed to record tool action: %s", e)

    # ------------------------------------------------------------------
    # Memory context builder — inject top-k relevant memories into system
    # prompt as BACKGROUND; the LLM is always the one that answers. Memory
    # augments, it never substitutes for LLM reasoning.
    # ------------------------------------------------------------------

    def _build_memory_context(self, query: str, max_facts_per_cat: int = 3) -> str | None:
        """Retrieve top-k relevant memory facts and format them as a
        system-prompt context snippet.

        Returns a string to inject into the LLM's system context, or None
        if memory is disabled / no hits / no query.
        """
        if not query or not query.strip():
            return None

        mm = self._get_memory_manager()
        if mm is None:
            return None

        mem_config = self._get_memory_config()
        if mem_config is None or not mem_config.enabled:
            return None

        # ---- Pull structured progress from the active ExecutionPlan ----
        plan_summary = self._format_active_plan_summary()

        # ---- Retrieve relevant facts from L3 memory ----
        use_vector = mem_config.vector_search_enabled and mm._check_chromadb_available()
        facts_by_category: dict[str, list] = {}
        for cat in ("goal", "action_plan", "action_done", "decision", "context"):
            try:
                if use_vector:
                    facts = mm.search_facts_vector(
                        self.id, query, top_k=max_facts_per_cat, category=cat)
                else:
                    facts = mm.search_facts(
                        self.id, query, top_k=max_facts_per_cat, category=cat)
            except Exception:
                facts = []
            if facts:
                facts_by_category[cat] = facts

        if not plan_summary and not facts_by_category:
            mc = getattr(self, "_memory_hit_counts", None) or {"hits": 0, "misses": 0}
            mc["misses"] = mc.get("misses", 0) + 1
            self._memory_hit_counts = mc
            return None

        parts = [
            "<memory_context>",
            "以下是从 agent 私有记忆中检索到的与当前问题相关的背景信息。",
            "这些是【参考资料】而非【答案】：",
            "  • 仅在与用户问题直接相关时使用；",
            "  • 若与问题无关，请忽略并按你自己的理解回答；",
            "  • 禁止把整段记忆原样复述给用户；",
            "  • 回答必须基于对用户问题的真实理解，而非记忆字段的 dump。",
            "",
        ]

        if plan_summary:
            parts.append("【当前执行计划】")
            parts.append(plan_summary)
            parts.append("")

        _CAT_TITLES = {
            "goal": "目标/里程碑",
            "action_plan": "待办事项",
            "action_done": "已完成",
            "decision": "关键决策",
            "context": "项目上下文",
        }
        for cat, facts in facts_by_category.items():
            title = _CAT_TITLES.get(cat, cat)
            parts.append(f"【{title}】")
            for f in facts[:max_facts_per_cat]:
                parts.append(f"- {f.content}")
            parts.append("")

        parts.append("</memory_context>")
        ctx = "\n".join(parts)

        self._log("memory_context", {
            "query": query[:100],
            "plan_hit": bool(plan_summary),
            "fact_categories": list(facts_by_category.keys()),
            "fact_count": sum(len(v) for v in facts_by_category.values()),
            "chars": len(ctx),
        })

        mc = getattr(self, "_memory_hit_counts", None) or {"hits": 0, "misses": 0}
        mc["hits"] = mc.get("hits", 0) + 1
        self._memory_hit_counts = mc

        return ctx

    def _format_active_plan_summary(self) -> str:
        """将当前活跃的 ExecutionPlan 格式化为可读摘要。"""
        active_plans = [p for p in self.execution_plans if p.status == "active"]
        if not active_plans:
            return ""

        plan = active_plans[-1]  # 最近的活跃计划
        progress = plan.get_progress()
        lines = [
            f"**当前任务: {plan.task_summary}**",
            f"进度: {progress['done']}/{progress['total']} "
            f"({progress['percent']}%)\n",
        ]
        for step in plan.steps:
            if StepStatus and hasattr(StepStatus, 'COMPLETED'):
                if step.status == StepStatus.COMPLETED:
                    icon = "✅"
                elif step.status == StepStatus.IN_PROGRESS:
                    icon = "🔄"
                elif step.status == StepStatus.FAILED:
                    icon = "❌"
                elif step.status == StepStatus.SKIPPED:
                    icon = "⏭️"
                else:
                    icon = "⬜"
            else:
                icon = "⬜"
            line = f"{icon} {step.order + 1}. {step.title}"
            if step.result_summary:
                line += f" → {step.result_summary[:80]}"
            lines.append(line)

        return "\n".join(lines)

    def _build_checkpoint_context(self) -> str:
        """构建任务恢复上下文，注入到系统提示中。

        [F1] 过期过滤：若所有信号都早于 TUDOU_CHECKPOINT_STALE_HOURS
        （默认 24h），返回空串，避免复活老任务。
        """
        import os as _os
        import time as _time
        try:
            stale_hours = float(_os.environ.get("TUDOU_CHECKPOINT_STALE_HOURS", "24"))
        except (TypeError, ValueError):
            stale_hours = 24.0
        stale_cutoff = _time.time() - stale_hours * 3600

        mm = self._get_memory_manager()
        parts = []

        def _plan_latest_ts(p):
            ts = p.created_at
            for s in p.steps:
                if getattr(s, "completed_at", 0) and s.completed_at > ts:
                    ts = s.completed_at
                if getattr(s, "started_at", 0) and s.started_at > ts:
                    ts = s.started_at
            return ts

        fresh_active = any(
            p.status == "active" and _plan_latest_ts(p) >= stale_cutoff
            for p in self.execution_plans
        )
        if fresh_active:
            plan_summary = self._format_active_plan_summary()
            if plan_summary:
                parts.append(plan_summary)

        if mm:
            recent_done = mm.get_recent_facts(self.id, limit=10, category="action_done")
            fresh_done = [f for f in recent_done
                          if getattr(f, "updated_at", 0) >= stale_cutoff]
            if fresh_done:
                parts.append("\n**最近完成的操作:**")
                for f in fresh_done[:10]:
                    parts.append(f"- {f.content}")

            plans_facts = mm.get_recent_facts(self.id, limit=5, category="action_plan")
            fresh_plans = [f for f in plans_facts
                           if getattr(f, "updated_at", 0) >= stale_cutoff]
            if fresh_plans:
                parts.append("\n**待办事项:**")
                for f in fresh_plans[:5]:
                    parts.append(f"- {f.content}")

        if not parts:
            return ""

        return (
            "\n<task_checkpoint>\n"
            "⚠️ 你正在继续之前的任务，以下是当前进展。\n"
            "请从断点继续，不要重复已完成的工作。\n"
            "已有文件请先检查再修改，不要重新创建。\n\n"
            + "\n".join(parts)
            + "\n</task_checkpoint>\n"
        )

    def _write_plan_to_memory(self, plan: "ExecutionPlan"):
        """将 ExecutionPlan 的里程碑/步骤写入 L3 记忆。

        在计划创建时调用，使得后续查询可以从记忆中直接获取。
        """
        mm = self._get_memory_manager()
        if mm is None:
            return
        try:
            from .core.memory import SemanticFact
        except ImportError:
            try:
                from app.core.memory import SemanticFact
            except ImportError:
                return

        try:
            # 写入目标 (goal)
            if plan.task_summary:
                mm.save_fact(SemanticFact(
                    agent_id=self.id,
                    category="goal",
                    content=f"[任务目标] {plan.task_summary}",
                    source=f"execution_plan:{plan.id}",
                    confidence=0.95,
                ))

            # 写入每个步骤为 action_plan
            for step in plan.steps:
                mm.save_fact(SemanticFact(
                    agent_id=self.id,
                    category="action_plan",
                    content=f"[步骤{step.order + 1}] {step.title}"
                             + (f" - {step.detail}" if step.detail else ""),
                    source=f"execution_plan:{plan.id}:step:{step.id}",
                    confidence=0.9,
                ))

            self._log("memory", {
                "action": "plan_to_memory",
                "plan_id": plan.id,
                "steps": len(plan.steps),
            })
        except Exception as e:
            logger.debug("Failed to write plan to memory: %s", e)

    def _write_step_completion_to_memory(self, plan: "ExecutionPlan",
                                          step: "ExecutionStep"):
        """将步骤完成结果写入 L3 记忆 (action_done)。"""
        mm = self._get_memory_manager()
        if mm is None:
            return
        try:
            from .core.memory import SemanticFact
        except ImportError:
            try:
                from app.core.memory import SemanticFact
            except ImportError:
                return

        try:
            content = (
                f"[{time.strftime('%Y-%m-%d %H:%M')}] "
                f"完成步骤: {step.title}"
            )
            if step.result_summary:
                content += f" → 结果: {step.result_summary[:200]}"

            mm.save_fact(SemanticFact(
                agent_id=self.id,
                category="action_done",
                content=content,
                source=f"execution_plan:{plan.id}:step:{step.id}",
                confidence=0.95,
            ))
            self._log("memory", {
                "action": "step_done_to_memory",
                "plan_id": plan.id,
                "step": step.title[:50],
            })
        except Exception as e:
            logger.debug("Failed to write step completion to memory: %s", e)

    def _update_agent_phase(self):
        """根据当前 ExecutionPlan 状态自动更新 agent_phase。"""
        active_plans = [p for p in self.execution_plans if p.status == "active"]
        if not active_plans:
            if AgentPhase and hasattr(AgentPhase, 'BLOCKED') and hasattr(AgentPhase, 'IDLE'):
                if self.agent_phase != AgentPhase.BLOCKED:
                    self.agent_phase = AgentPhase.IDLE
            return

        plan = active_plans[-1]
        progress = plan.get_progress()

        if AgentPhase is None or not hasattr(AgentPhase, 'PLANNING'):
            return

        if progress["total"] == 0:
            self.agent_phase = AgentPhase.PLANNING
        elif progress["done"] == progress["total"]:
            self.agent_phase = AgentPhase.REVIEWING
        elif progress["in_progress"] > 0 or progress["done"] > 0:
            self.agent_phase = AgentPhase.EXECUTING
        else:
            self.agent_phase = AgentPhase.PLANNING

    def _make_summary_llm_call(self):
        """
        构建用于记忆摘要/提取的 LLM 调用函数。
        复用当前 agent 的 provider/model 配置。
        """
        try:
            from . import llm
        except ImportError:
            try:
                from app import llm  # type: ignore
            except ImportError:
                return None

        _eff_provider, _eff_model = self._resolve_effective_provider_model()

        def _call(prompt: str) -> str:
            messages = [
                {"role": "system", "content": "你是一个信息提取助手，请精确按照要求的格式返回结果。"},
                {"role": "user", "content": prompt},
            ]
            resp = llm.chat_no_stream(
                messages, tools=None,
                provider=_eff_provider, model=_eff_model,
            )
            return resp.get("message", {}).get("content", "")

        return _call

    def _estimate_token_count(self) -> int:
        """Estimate total token count of current messages.

        Uses tiktoken if available (accurate), otherwise a CJK-aware
        heuristic that counts CJK characters and ASCII words separately.
        """
        total = 0
        for m in self.messages:
            content = _ensure_str_content(m.get("content"))
            if content:
                total += _count_tokens(content)
            tc = m.get("tool_calls", [])
            if tc:
                total += _count_tokens(json.dumps(tc, ensure_ascii=False))
        return total

    def _get_context_limit(self) -> int:
        """Get the context window token limit based on model.

        Priority:
        1. Provider's configured context_length (if > 0)
        2. Model name heuristic
        3. Default 4096 (safe for local models like LM Studio)
        """
        from . import llm

        # Check provider: explicit config or auto-detected from server
        try:
            reg = llm.get_registry()
            if self.provider:
                entry = reg.get(self.provider)
                if entry:
                    if entry.context_length > 0:
                        return entry.context_length
                    # Try auto-detect from the server API
                    detected = llm.detect_context_length(entry, model=self.model)
                    if detected > 0:
                        entry.context_length = detected  # Cache for future calls
                        return detected
        except Exception:
            pass

        # Heuristic based on model name
        model = (self.model or "").lower()
        if "128k" in model:
            return 128000
        if "32k" in model:
            return 32000
        if "claude" in model:
            return 200000
        if "gpt-4" in model:
            return 128000
        if "gpt-3.5" in model:
            return 16000
        # Local models: infer from model name or use a sensible default.
        # Users can override via provider.context_length for exact control.
        if "qwen3" in model or "qwen2.5" in model:
            return 32768  # Qwen 3/2.5 support 32k+ natively
        if "qwen" in model:
            return 8192
        if "deepseek" in model:
            return 16384
        if "llama" in model or "mistral" in model or "gemma" in model:
            return 8192
        return 8192  # safe default for most modern local models

    def _llm_summarize_context(self, messages_to_compress: list) -> str | None:
        """
        Call LLM to generate a structured summary of conversation turns.

        Serializes messages with labeled format, then uses the agent's own
        LLM provider/model to generate a structured summary with Goal/Progress/
        Decisions/Files/Next Steps sections. Handles iterative updates if a
        previous summary exists.

        Args:
            messages_to_compress: List of message dicts to summarize

        Returns:
            Summary string with prefix, or None if LLM call fails
        """
        from . import llm
        import time as time_module

        # Check cooldown: don't retry for 10 minutes if previous attempt failed
        now = time_module.time()
        if now < self._compression_cooldown:
            logger.debug("Context summarization in cooldown (%.0fs remaining)",
                        self._compression_cooldown - now)
            return None

        # Serialize messages into labeled text format
        parts = []
        for msg in messages_to_compress:
            role = msg.get("role", "unknown")
            content = _ensure_str_content(msg.get("content"))

            # Tool results: keep significant detail (up to 2000 chars)
            if role == "tool":
                tool_id = msg.get("tool_call_id", "")
                if len(content) > 2000:
                    content = content[:1200] + "\n...[truncated]...\n" + content[-600:]
                parts.append(f"[TOOL RESULT {tool_id}]: {content}")
                continue

            # Assistant messages: include tool call names and truncated arguments
            if role == "assistant":
                if len(content) > 2000:
                    content = content[:1200] + "\n...[truncated]...\n" + content[-600:]
                tool_calls = msg.get("tool_calls", [])
                if tool_calls:
                    tc_parts = []
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            fn = tc.get("function", {})
                            name = fn.get("name", "?")
                            args = fn.get("arguments", "")
                            if len(args) > 300:
                                args = args[:250] + "..."
                            tc_parts.append(f"  {name}({args})")
                        else:
                            fn = getattr(tc, "function", None)
                            name = getattr(fn, "name", "?") if fn else "?"
                            tc_parts.append(f"  {name}(...)")
                    content += "\n[Tool calls:\n" + "\n".join(tc_parts) + "\n]"
                parts.append(f"[ASSISTANT]: {content}")
                continue

            # User and other roles
            if len(content) > 2000:
                content = content[:1200] + "\n...[truncated]...\n" + content[-600:]
            parts.append(f"[{role.upper()}]: {content}")

        content_to_summarize = "\n\n".join(parts)

        # Build prompt: iterative update if previous summary exists
        if self._previous_compression_summary:
            prompt = f"""You are updating a context compression summary. A previous compaction produced the summary below. New conversation turns have occurred and need to be incorporated.

PREVIOUS SUMMARY:
{self._previous_compression_summary}

NEW TURNS TO INCORPORATE:
{content_to_summarize}

Update the summary using this exact structure. PRESERVE all existing information that is still relevant. ADD new progress. Move items from "In Progress" to "Done" when completed. Remove information only if clearly obsolete.

## Goal
[What the user is trying to accomplish — preserve from previous summary, update if goal evolved]

## Constraints & Preferences
[User preferences, coding style, constraints, important decisions — accumulate across compressions]

## Progress
### Done
[Completed work — include specific file paths, commands run, results obtained]
### In Progress
[Work currently underway]
### Blocked
[Any blockers or issues encountered]

## Key Decisions
[Important technical decisions and why they were made]

## Relevant Files
[Files read, modified, or created — with brief note on each. Accumulate across compressions.]

## Next Steps
[What needs to happen next to continue the work]

## Critical Context
[Any specific values, error messages, configuration details, or data that would be lost without explicit preservation]

Target ~2000 tokens. Be specific — include file paths, command outputs, error messages, and concrete values.

Write only the summary body. Do not include any preamble or prefix."""
        else:
            prompt = f"""Create a structured handoff summary for a later assistant that will continue this conversation after earlier turns are compacted.

TURNS TO SUMMARIZE:
{content_to_summarize}

Use this exact structure:

## Goal
[What the user is trying to accomplish]

## Constraints & Preferences
[User preferences, coding style, constraints, important decisions]

## Progress
### Done
[Completed work — include specific file paths, commands run, results obtained]
### In Progress
[Work currently underway]
### Blocked
[Any blockers or issues encountered]

## Key Decisions
[Important technical decisions and why they were made]

## Relevant Files
[Files read, modified, or created — with brief note on each]

## Next Steps
[What needs to happen next to continue the work]

## Critical Context
[Any specific values, error messages, configuration details, or data that would be lost without explicit preservation]

Target ~2000 tokens. Be specific — include file paths, command outputs, error messages, and concrete values.

Write only the summary body. Do not include any preamble or prefix."""

        try:
            # Get effective provider/model
            _eff_provider, _eff_model = self._resolve_effective_provider_model()

            # Call LLM summarization
            response = llm.chat_no_stream(
                messages=[{"role": "user", "content": prompt}],
                provider=_eff_provider,
                model=_eff_model,
                max_tokens=4000,
            )

            # Extract content from response
            summary = response.get("content", "").strip() if isinstance(response, dict) else ""
            if not summary and hasattr(response, "choices"):
                # Handle structured response object
                summary = response.choices[0].message.content if response.choices else ""

            if not summary:
                logger.warning("LLM summarization returned empty content")
                return None

            # Store for iterative updates on next compression
            self._previous_compression_summary = summary
            self._compression_cooldown = 0.0

            # Add prefix for context
            prefix = (
                "[CONTEXT COMPACTION] Earlier turns in this conversation were compacted "
                "to save context space. The summary below describes work that was "
                "already completed, and the current session state may still reflect "
                "that work (for example, files may already be changed). Use the summary "
                "and the current state to continue from where things left off, and "
                "avoid repeating work:"
            )
            return f"{prefix}\n{summary}"

        except Exception as e:
            # Set cooldown: don't retry for 600 seconds (10 minutes)
            self._compression_cooldown = now + 600.0
            logger.warning(
                "Failed to generate context summary: %s. "
                "Further summary attempts paused for 600 seconds.",
                e,
            )
            return None

    def _compress_context(self):
        """
        LLM-powered context compression: when token usage exceeds 50% of context limit,
        compress earlier conversation turns using structured LLM summarization.

        Algorithm:
        1. Check if compression is needed (50% threshold, not 70%)
        2. Separate messages: system (preserve), head (first 2 exchanges), tail (last 20 or ~30%),
           middle (everything else to compress)
        3. Pre-pass: prune old tool results >200 chars to placeholder
        4. Call _llm_summarize_context() for structured summary
        5. Fall back to text-join approach if LLM fails
        6. Sanitize tool_call/tool_result pairs after compression
        """
        token_count = self._estimate_token_count()
        context_limit = self._get_context_limit()
        threshold = int(context_limit * 0.5)  # 50% threshold, not 70%

        if token_count <= threshold:
            return  # Below threshold, no compression needed

        self.history_log.add("context_compress",
                             f"tokens={token_count} limit={context_limit} threshold={threshold}")

        # Separate system message
        system_msg = None
        non_system = self.messages
        if self.messages and self.messages[0].get("role") == "system":
            system_msg = self.messages[0]
            non_system = self.messages[1:]

        if len(non_system) <= 6:  # Need at least head + tail + middle
            return  # Too few messages to compress

        # Calculate boundary: head (first 2 exchanges = ~4 msgs), tail (last 20 or ~30%)
        head_count = min(4, len(non_system) // 3)  # First 2 user-assistant pairs
        tail_count = max(20, len(non_system) * 3 // 10)  # Last ~30% or 20, whichever is more

        if head_count + tail_count >= len(non_system) - 1:
            return  # Not enough middle to compress

        head = non_system[:head_count]
        tail = non_system[-(tail_count):]
        to_compress = non_system[head_count:-(tail_count)]

        # Phase 1: CHEAP pre-pass - prune old tool results >200 chars
        pruned_compress = []
        pruned_count = 0
        for msg in to_compress:
            if msg.get("role") == "tool":
                content = _ensure_str_content(msg.get("content"))
                if len(content) > 200 and content != "[Tool output cleared to save context]":
                    pruned_count += 1
                    pruned_compress.append({
                        **msg,
                        "content": "[Tool output cleared to save context]"
                    })
                else:
                    pruned_compress.append(msg)
            else:
                pruned_compress.append(msg)

        if pruned_count > 0:
            logger.debug("Pre-compression: pruned %d old tool result(s)", pruned_count)

        # Phase 2: Try LLM-powered summarization
        summary = self._llm_summarize_context(pruned_compress)

        # Phase 3: Fall back to text-join if LLM failed
        if summary is None:
            logger.debug("LLM summarization unavailable, falling back to text-join approach")
            summary_parts = []
            for m in pruned_compress:
                role = m.get("role", "unknown")
                content = _ensure_str_content(m.get("content"))
                if content.strip():
                    preview = content.replace("\n", " ").strip()
                    if len(preview) > 500:
                        preview = preview[:500] + "..."
                    if role == "user":
                        summary_parts.append(f"[User] {preview}")
                    elif role == "assistant":
                        summary_parts.append(f"[Assistant] {preview}")
                    elif role == "tool":
                        summary_parts.append(f"[Tool Result] {preview[:250]}")

            summary = (
                f"[Context Compressed: {len(pruned_compress)} messages summarized]\n"
                f"--- Earlier Conversation Summary ---\n"
                + "\n".join(summary_parts)
                + "\n--- End Summary ---"
            )

        summary_msg = {"role": "user", "content": summary}

        # Phase 4: Rebuild message list
        self.messages = (
            ([system_msg] if system_msg else [])
            + head
            + [summary_msg]
            + tail
        )

        # Phase 5: Sanitize tool_call/tool_result pairs
        self._sanitize_tool_pairs()

        new_token_count = self._estimate_token_count()
        self.history_log.add("context_compressed",
                             f"removed={len(to_compress)} msgs, "
                             f"tokens: {token_count} -> {new_token_count}")
        self._log("status", {
            "action": "context_compressed",
            "removed_messages": len(to_compress),
            "tokens_before": token_count,
            "tokens_after": new_token_count,
        })

    def _sanitize_tool_pairs(self):
        """
        Fix orphaned tool_call / tool_result pairs after compression.

        Two failure modes:
        1. A tool result references a call_id whose assistant tool_call was removed.
           The API rejects this: "No tool call found for function call output with call_id ..."
        2. An assistant message has tool_calls whose results were dropped.
           The API rejects because every tool_call must have a matching tool result.

        Removes orphaned results and inserts stub results for orphaned calls.
        """
        # Collect surviving tool call IDs
        surviving_call_ids = set()
        for msg in self.messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    cid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "")
                    if cid:
                        surviving_call_ids.add(cid)

        # Collect existing tool result IDs
        result_call_ids = set()
        for msg in self.messages:
            if msg.get("role") == "tool":
                cid = msg.get("tool_call_id")
                if cid:
                    result_call_ids.add(cid)

        # 1. Remove tool results with no matching tool_call
        orphaned_results = result_call_ids - surviving_call_ids
        if orphaned_results:
            self.messages = [
                m for m in self.messages
                if not (m.get("role") == "tool" and m.get("tool_call_id") in orphaned_results)
            ]
            logger.debug("Sanitizer: removed %d orphaned tool result(s)", len(orphaned_results))

        # 2. Add stub results for tool_calls with no result
        missing_results = surviving_call_ids - result_call_ids
        if missing_results:
            patched = []
            for msg in self.messages:
                patched.append(msg)
                if msg.get("role") == "assistant":
                    for tc in msg.get("tool_calls") or []:
                        cid = tc.get("id") if isinstance(tc, dict) else getattr(tc, "id", "")
                        if cid in missing_results:
                            patched.append({
                                "role": "tool",
                                "content": "[Result from earlier conversation — see context summary above]",
                                "tool_call_id": cid,
                            })
            self.messages = patched
            logger.debug("Sanitizer: added %d stub tool result(s)", len(missing_results))

    def _trim_context(self):
        """
        上下文管理（三层记忆增强版）：
        1. 如果启用了三层记忆，使用 L1 窗口裁剪（只保留最近 N 轮）
        2. 否则回退到原有的消息数量限制
        3. 按 token 用量 70% 阈值智能压缩（兜底）
        """
        # Phase 0: Memory-aware L1 windowing
        mm = self._get_memory_manager()
        mem_config = self._get_memory_config()
        if mm and mem_config and mem_config.enabled:
            # 使用 L1 窗口：只保留最近 N 轮 + system 消息
            l1_messages = mm.get_l1_messages(
                self.messages, max_turns=mem_config.l1_max_turns,
            )
            if l1_messages and len(l1_messages) < len(self.messages):
                self.messages = l1_messages
                self._log("memory", {
                    "action": "l1_window",
                    "kept": len(l1_messages),
                    "max_turns": mem_config.l1_max_turns,
                })
        else:
            # Phase 1: legacy message count limit
            max_msgs = self.profile.max_context_messages
            if max_msgs > 0 and len(self.messages) > max_msgs + 1:
                system = self.messages[0] if self.messages[0].get("role") == "system" else None
                trimmed = self.messages[-(max_msgs):]
                self.messages = ([system] if system else []) + trimmed

        # Phase 2: token-based compression at 70% (safety net)
        self._compress_context()

    # ---- tool execution with policy check ----

    def _get_effective_tools(self) -> list[dict]:
        """Filter tool definitions based on agent's profile permissions."""
        from . import tools
        all_tools = tools.get_tool_definitions()
        allowed = self.profile.allowed_tools
        denied = set(self.profile.denied_tools)

        if allowed:
            allowed_set = set(allowed)
            all_tools = [t for t in all_tools
                         if t["function"]["name"] in allowed_set]

        if denied:
            all_tools = [t for t in all_tools
                         if t["function"]["name"] not in denied]

        return all_tools

    def _message_is_multimodal(self, user_message: Any) -> bool:
        """Detect whether the pending user message contains vision/audio parts."""
        try:
            if isinstance(user_message, list):
                for part in user_message:
                    if isinstance(part, dict):
                        t = str(part.get("type", "")).lower()
                        if t in ("image", "image_url", "input_image",
                                 "audio", "input_audio"):
                            return True
            if isinstance(user_message, dict):
                content = user_message.get("content")
                if isinstance(content, list):
                    return self._message_is_multimodal(content)
        except Exception:
            pass
        return False

    def _is_coding_context(self) -> bool:
        """Check if current execution context involves code/tool operations."""
        try:
            from .agent import AgentPhase
            return self.agent_phase == AgentPhase.EXECUTING
        except Exception:
            return False

    def _resolve_effective_provider_model(self, user_message: Any = None) -> tuple[str, str]:
        """Re-resolve provider/model from registry before each LLM call.

        If the configured provider is empty, disabled, or removed, falls back
        to the global default. This ensures agents pick up live config changes
        (new API key, URL, etc.) without needing a restart or re-create.

        P2 #8: if `user_message` is provided and contains vision/audio parts,
        route to the multimodal provider/model when configured.
        """
        from . import llm

        # Per-task override takes top priority — task A uses LLM A, task B uses LLM B.
        ct = self._current_task
        if ct is not None and (getattr(ct, "provider", "") or getattr(ct, "model", "")):
            prov = ct.provider or self.provider
            mdl = ct.model or self.model
        else:
            prov = self.provider
            mdl = self.model

        # 方案乙: extra_llms 路由 —— 如果 task 带了 llm_label，优先从
        # agent.extra_llms 里找 label 或 purpose 命中的 slot，命中就覆盖
        # provider/model。这是最简形态：单层查找、无 fallback chain。
        # 以后要做按成本/上下文长度/模态自动挑，也只改这一段。
        try:
            label = ""
            if ct is not None:
                label = (getattr(ct, "llm_label", "") or "").strip()
            if label and self.extra_llms:
                for slot in self.extra_llms:
                    if not isinstance(slot, dict):
                        continue
                    slot_label = str(slot.get("label", "")).strip()
                    slot_purpose = str(slot.get("purpose", "")).strip()
                    if slot_label == label or slot_purpose == label:
                        sp = str(slot.get("provider", "")).strip()
                        sm = str(slot.get("model", "")).strip()
                        if sp or sm:
                            logger.info(
                                "Agent %s: extra_llms[%s] → routing to %s/%s",
                                self.id[:8], label, sp or prov, sm or mdl,
                            )
                            prov = sp or prov
                            mdl = sm or mdl
                        break
        except Exception as _el_err:
            logger.debug("extra_llms routing skipped: %s", _el_err)

        # 方案乙(b): auto_route 启发式 —— 没显式指定 llm_label 时，按输入
        # 类型自动挑 extra_llms 里的某个 slot：
        #   multimodal 输入 → auto_route["multimodal"]
        #   长/复杂 prompt  → auto_route["complex"]
        #   其他             → auto_route["default"]（留空就是走 agent.provider/model）
        # 全部可选，任何没命中的分支都安全回退。
        try:
            ar = self.auto_route or {}
            explicit_label = ""
            if ct is not None:
                explicit_label = (getattr(ct, "llm_label", "") or "").strip()
            if (
                ar.get("enabled")
                and self.extra_llms
                and not explicit_label  # 显式 label 已经在上面处理过了
            ):
                # ---- 决定 category ----
                category = "default"
                try:
                    if user_message is not None and self._message_is_multimodal(user_message):
                        category = "multimodal"
                    elif self._is_coding_context():
                        category = "coding"
                    else:
                        # 粗略估算 prompt 长度：取字符串化后的长度
                        threshold = int(ar.get("complex_threshold_chars", 2000) or 2000)
                        msg_text = ""
                        if isinstance(user_message, str):
                            msg_text = user_message
                        elif isinstance(user_message, list):
                            # OpenAI 风格 multi-part：拼一下 text 部分
                            parts = []
                            for p in user_message:
                                if isinstance(p, dict):
                                    t = p.get("text") or ""
                                    if isinstance(t, str):
                                        parts.append(t)
                            msg_text = "\n".join(parts)
                        elif isinstance(user_message, dict):
                            msg_text = str(user_message.get("content", "") or "")
                        if threshold > 0 and len(msg_text) >= threshold:
                            category = "complex"
                except Exception:
                    category = "default"

                target_label = str(ar.get(category, "") or "").strip()
                if target_label:
                    for slot in self.extra_llms:
                        if not isinstance(slot, dict):
                            continue
                        slot_label = str(slot.get("label", "")).strip()
                        slot_purpose = str(slot.get("purpose", "")).strip()
                        if slot_label == target_label or slot_purpose == target_label:
                            sp = str(slot.get("provider", "")).strip()
                            sm = str(slot.get("model", "")).strip()
                            if sp or sm:
                                logger.info(
                                    "Agent %s: auto_route[%s=%s] → routing to %s/%s",
                                    self.id[:8], category, target_label,
                                    sp or prov, sm or mdl,
                                )
                                prov = sp or prov
                                mdl = sm or mdl
                            break
        except Exception as _ar_err:
            logger.debug("auto_route skipped: %s", _ar_err)

        # Multimodal routing: if the incoming message is multimodal and a
        # dedicated multimodal model is configured, prefer it.
        try:
            if user_message is not None and self._message_is_multimodal(user_message):
                mm_prov = self.multimodal_provider or prov
                mm_mdl = self.multimodal_model or mdl
                if self.multimodal_provider or self.multimodal_model:
                    logger.info(
                        "Agent %s: multimodal input → routing to %s/%s",
                        self.id[:8], mm_prov, mm_mdl,
                    )
                    prov, mdl = mm_prov, mm_mdl
                else:
                    logger.warning(
                        "Agent %s: multimodal input detected but no "
                        "multimodal_provider/model configured — using "
                        "default %s/%s (may not support vision)",
                        self.id[:8], prov, mdl,
                    )
        except Exception as _mm_err:
            logger.debug("multimodal routing skipped: %s", _mm_err)
        # Coding routing: when executing tool calls / code generation and
        # a dedicated coding model is configured, prefer it.
        try:
            if (self.coding_provider or self.coding_model) and self._is_coding_context():
                cd_prov = self.coding_provider or prov
                cd_mdl = self.coding_model or mdl
                logger.info(
                    "Agent %s: coding context → routing to %s/%s",
                    self.id[:8], cd_prov, cd_mdl,
                )
                prov, mdl = cd_prov, cd_mdl
        except Exception as _cd_err:
            logger.debug("coding routing skipped: %s", _cd_err)
        # RolePresetV2 LLM Tier routing: when no explicit provider resolved yet
        # but agent.profile.llm_tier is set, resolve via LLMTierRouter.
        # Priority: per-task override > extra_llm slot > auto_route > multimodal
        #         > coding > explicit agent.provider > **llm_tier** > global default
        try:
            if not prov and getattr(self.profile, "llm_tier", ""):
                from .llm_tier_routing import get_router as _get_tier_router
                tier_router = _get_tier_router()
                # Context-specific tier override (e.g. coding task uses coding tier)
                active_tier = self.profile.llm_tier
                tier_overrides = getattr(self.profile, "llm_tier_overrides", {}) or {}
                try:
                    if self._is_coding_context() and "coding" in tier_overrides:
                        active_tier = tier_overrides["coding"]
                    elif tier_overrides.get("multimodal") and user_message:
                        # Simple multimodal detection: message contains file attachments
                        from .agent_types import _message_has_visual  # type: ignore
                        try:
                            if _message_has_visual(user_message):
                                active_tier = tier_overrides["multimodal"]
                        except Exception:
                            pass
                except Exception:
                    pass
                tier_prov, tier_mdl = tier_router.resolve(active_tier)
                if tier_prov and tier_mdl:
                    logger.info(
                        "Agent %s: llm_tier=%s → routing to %s/%s",
                        self.id[:8], active_tier, tier_prov, tier_mdl,
                    )
                    prov, mdl = tier_prov, tier_mdl
        except Exception as _tier_err:
            logger.debug("llm_tier routing skipped: %s", _tier_err)
        try:
            cfg = llm.get_config()
            if not prov:
                # No provider set — use global default
                prov = cfg.get("provider", "")
                mdl = mdl or cfg.get("model", "")
            else:
                # Provider set — verify it still exists and is enabled
                reg = llm.get_registry()
                entry = reg.get(prov)
                if entry is None or not entry.enabled:
                    prov = cfg.get("provider", "")
                    mdl = mdl or cfg.get("model", "")
                    logger.warning(
                        "Agent %s: provider '%s' unavailable, "
                        "falling back to '%s/%s'",
                        self.id[:8], self.provider, prov, mdl)
        except Exception as e:
            logger.error("Agent %s: provider resolution failed: %s",
                         self.id[:8], e)
        return prov, mdl

    def _handle_large_result(self, tool_name: str, result: str) -> str:
        """If tool result exceeds 100KB, save to file and return a summary + path."""
        import datetime

        LARGE_RESULT_THRESHOLD = 100_000  # 100KB

        if len(result) <= LARGE_RESULT_THRESHOLD:
            return result

        # Save to working_dir or a results directory
        results_dir = os.path.join(
            self.working_dir or os.path.join(
                os.environ.get("TUDOU_CLAW_DATA_DIR", "."),
                "workspaces", self.id
            ),
            "large_results"
        )
        os.makedirs(results_dir, exist_ok=True)

        # Generate filename with timestamp
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = os.path.join(results_dir, f"{tool_name}_{timestamp}.txt")

        # Save result to file
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.write(result)
        except Exception as e:
            logger.error(f"Failed to save large result to {filepath}: {e}")
            # Fall back to returning truncated result if save fails
            return result[:LARGE_RESULT_THRESHOLD] + f"\n...[result truncated, failed to save to file: {e}]"

        # Return truncated preview + file path
        preview = result[:2000] + "\n...\n" + result[-500:]
        return f"[Result too large ({len(result)} chars), saved to {filepath}]\n\nPreview:\n{preview}"
