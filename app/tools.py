"""
Tool system — Claude Code style tools with JSON schema definitions.
"""
import fnmatch
import json
import logging
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .defaults import (
    MAX_PARALLEL_WORKERS as _DEF_MAX_WORKERS,
    MAX_HTTP_RESPONSE_CHARS, MAX_JSON_RESULT_CHARS,
)

from . import sandbox as _sandbox
from . import knowledge as _knowledge

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ToolRegistry pattern (singleton, inspired by Hermes Agent)
# ---------------------------------------------------------------------------

@dataclass
class ToolEntry:
    """Registry entry for a single tool."""
    name: str
    toolset: str  # e.g. "core", "web", "system", "coordination"
    schema: dict  # JSON schema definition (the function dict)
    handler: Callable  # The actual function to call
    check_fn: Optional[Callable] = None  # Optional availability check (returns bool)
    requires_env: list[str] = field(default_factory=list)  # Required environment variables
    is_async: bool = False  # Whether the tool is async
    description: str = ""  # Tool description
    risk_level: str = "safe"  # "safe", "moderate", or "dangerous"


class ToolRegistry:
    """Singleton registry for managing tools."""
    _instance: Optional["ToolRegistry"] = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._tools: dict[str, ToolEntry] = {}
        self._aliases: dict[str, str] = {}  # alias → canonical name
        self._initialized = True

    def register(
        self,
        name: str,
        toolset: str,
        schema: dict,
        handler: Callable,
        check_fn: Optional[Callable] = None,
        requires_env: Optional[list[str]] = None,
        is_async: bool = False,
        description: str = "",
        risk_level: str = "safe",
    ) -> None:
        """Register a new tool in the registry."""
        if name in self._tools:
            logger.warning(f"Tool '{name}' already registered, overwriting")

        entry = ToolEntry(
            name=name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            check_fn=check_fn,
            requires_env=requires_env or [],
            is_async=is_async,
            description=description,
            risk_level=risk_level,
        )
        self._tools[name] = entry

    def unregister(self, name: str) -> bool:
        """Remove a tool from the registry. Returns True if removed, False if not found."""
        if name in self._tools:
            del self._tools[name]
            # Also remove any aliases pointing to this tool
            aliases_to_remove = [alias for alias, target in self._aliases.items() if target == name]
            for alias in aliases_to_remove:
                del self._aliases[alias]
            return True
        return False

    def add_alias(self, alias: str, canonical_name: str) -> None:
        """Add an alias for a tool."""
        if canonical_name not in self._tools:
            raise ValueError(f"Cannot alias '{alias}' to unknown tool '{canonical_name}'")
        self._aliases[alias] = canonical_name

    def dispatch(self, name: str, arguments: dict) -> str:
        """
        Dispatch a tool call by name.
        - Resolves aliases
        - Checks availability (check_fn)
        - Calls handler with arguments
        Returns a string result.
        """
        # Resolve alias
        canonical_name = self._aliases.get(name, name)

        # Auto-redirect common shell-command-as-tool misfires. Some LLMs
        # (Qwen / smaller models) after reading a SKILL.md that shows
        # `ls -la foo/` or `python build.py` mistake the command itself
        # for a registered tool name — we get `name='ls', args={...}`
        # instead of `name='bash', args={cmd: 'ls ...'}`. Rather than
        # bubble up "Unknown tool 'ls'" and loop the LLM on its mistake,
        # transparently reinterpret it as a bash call. This mirrors how
        # a human would read the intent.
        if canonical_name not in self._tools:
            _SHELL_COMMAND_REDIRECTS = frozenset({
                "ls", "cat", "cd", "pwd", "mkdir", "rm", "cp", "mv",
                "grep", "find", "head", "tail", "wc", "sort", "uniq",
                "which", "whoami", "ps", "kill", "chmod", "touch",
                "echo", "tree", "du", "df",
            })
            if canonical_name in _SHELL_COMMAND_REDIRECTS and "bash" in self._tools:
                # Build a bash command from the args. If the LLM passed
                # `{command: "..."}` or `{args: "..."}`, use that
                # verbatim — otherwise rebuild `ls <positional>` from
                # the original tool name + args dict.
                if isinstance(arguments, dict):
                    cmd_str = (
                        arguments.get("command")
                        or arguments.get("cmd")
                        or arguments.get("shell")
                        or ""
                    )
                    if not cmd_str:
                        # Stitch together: `<tool_name> <arg1> <arg2> ...`
                        parts = [canonical_name]
                        for v in arguments.values():
                            if v is None:
                                continue
                            parts.append(str(v))
                        cmd_str = " ".join(parts)
                else:
                    cmd_str = f"{canonical_name} {arguments}" if arguments else canonical_name
                logger.info(
                    "tool dispatch: %r not registered — redirecting to bash(%r)",
                    name, cmd_str[:200],
                )
                entry = self._tools["bash"]
                try:
                    return entry.handler(command=cmd_str)
                except Exception as _rx_err:
                    return (f"Error: redirected '{name}' to bash but bash "
                            f"itself failed: {_rx_err}")

            available = list(self._tools.keys())
            return (f"Error: Unknown tool '{name}'. "
                    f"Available: {available}. "
                    f"For shell commands use 'bash'.")

        entry = self._tools[canonical_name]

        # Check availability
        if entry.check_fn and not entry.check_fn():
            return f"Error: Tool '{canonical_name}' is not available in this context"

        # Check required environment variables
        missing_env = [var for var in entry.requires_env if var not in os.environ]
        if missing_env:
            return f"Error: Tool '{canonical_name}' requires environment variables: {missing_env}"

        # Call handler
        try:
            return entry.handler(**arguments)
        except TypeError as e:
            # Special handling for bash tool (argument name mismatch)
            if canonical_name == "bash" and arguments and "command" not in arguments:
                cmd = (arguments.get("cmd") or arguments.get("script") or
                       arguments.get("code") or next(iter(arguments.values()), ""))
                if isinstance(cmd, str) and cmd:
                    try:
                        return entry.handler(command=cmd)
                    except Exception as e2:
                        return f"Error executing tool '{canonical_name}': {e2}"
            return f"Error executing tool '{canonical_name}': {e}"
        except Exception as e:
            return f"Error executing tool '{canonical_name}': {e}"

    def get_definitions(self) -> list[dict]:
        """Return JSON schema definitions for all available tools.

        Returns tools in OpenAI function-calling format:
        {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
        """
        definitions = []
        for entry in self._tools.values():
            if entry.check_fn is None or entry.check_fn():
                schema = entry.schema
                # Ensure OpenAI function-calling wrapper is present
                if schema.get("type") == "function" and "function" in schema:
                    # Already wrapped correctly
                    definitions.append(schema)
                elif "name" in schema:
                    # Bare schema (name, description, parameters) — wrap it
                    definitions.append({
                        "type": "function",
                        "function": schema,
                    })
                else:
                    definitions.append(schema)
        return definitions

    def get_available_tools(self) -> list[str]:
        """Return list of tool names that pass their check_fn (or have no check_fn)."""
        return [
            name for name, entry in self._tools.items()
            if entry.check_fn is None or entry.check_fn()
        ]

    def is_parallel_safe(self, name: str) -> bool:
        """Check if a tool is safe for parallel execution."""
        canonical_name = self._aliases.get(name, name)
        return canonical_name in PARALLEL_SAFE_TOOLS

    def get_tool_entry(self, name: str) -> Optional[ToolEntry]:
        """Get the ToolEntry for a tool (resolving aliases)."""
        canonical_name = self._aliases.get(name, name)
        return self._tools.get(canonical_name)

    def list_tools(self) -> list[str]:
        """Return sorted list of all registered tool names."""
        return sorted(self._tools.keys())


def tool_result(result: Any, tool_name: str = "") -> str:
    """Standardized JSON tool result response."""
    if isinstance(result, str):
        return result
    return json.dumps({"status": "success", "result": result, "tool": tool_name})


def tool_error(message: str, tool_name: str = "", details: Optional[dict] = None) -> str:
    """Standardized JSON tool error response."""
    error_obj = {"status": "error", "message": message, "tool": tool_name}
    if details:
        error_obj["details"] = details
    return json.dumps(error_obj)


# ---------------------------------------------------------------------------
# Parallel execution configuration
# ---------------------------------------------------------------------------

# Tools that are safe to execute in parallel (read-only, no side effects)
PARALLEL_SAFE_TOOLS = frozenset({
    "read_file", "search_files", "glob_files",
    "web_search", "web_fetch", "web_screenshot",
    "datetime_calc", "json_process", "text_process",
    "get_skill_guide",
})

# Max parallel workers
MAX_PARALLEL_WORKERS = _DEF_MAX_WORKERS


# ---------------------------------------------------------------------------
# Tool definitions (JSON schema for function calling)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read UTF-8 text content from a file with optional line range.\n"
                "Use when: viewing code/config/docs/data files, inspecting a known file path, reading part of a large file with offset+limit.\n"
                "Not for: searching by content (use search_files) or finding files by name (use glob_files). Binary files return replacement chars.\n"
                "Output: header line [path — lines N-M of T] + numbered lines (1-based).\n"
                "GOTCHA: offset is 0-based but output line numbers are 1-based. For binary files prefer file-specific tools (pptx/pdf skills). Path is resolved against the sandbox root."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative file path"},
                    "offset": {
                        "type": "integer",
                        "description": "Start reading from this line number (0-based). Default 0.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to read. Default: read all.",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Create a new file or overwrite an existing one with UTF-8 content. Auto-creates parent directories.\n"
                "Use when: generating a new file, saving agent output, creating config/scripts.\n"
                "Not for: surgical edits to an existing file (use edit_file to avoid clobbering). Do not use to append — this is full overwrite.\n"
                "Output: absolute path and byte count on success. The written path is what artifact cards link to.\n"
                "⚠️ MANDATORY 必填:\n"
                "  • `path` MUST be present in arguments — relative paths are resolved against your workspace_dir.\n"
                "  • Files MUST be created INSIDE your workspace (or shared workspace if the agent has one). Absolute paths outside workspace will be rejected by sandbox.\n"
                "  • For long content (>500 lines / >20KB) prefer `edit_file` on an existing file — write_file with huge content can hit max_tokens and the JSON gets truncated mid-call (`{path: ..., content: <CUT>` → arguments fail to parse → schema reports 'path missing'). If you MUST write a large file, split into multiple write_file calls or use bash heredoc.\n"
                "GOTCHA: overwrites silently — read_file first if uncertain.\n"
                "Example: write_file(path=\"index.html\", content=\"<!DOCTYPE html>...\")"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "REQUIRED. Relative path INSIDE workspace (e.g. 'index.html', "
                            "'src/main.py'), or absolute path under workspace_dir. "
                            "Sandbox rejects paths outside workspace."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "REQUIRED. Full file content (UTF-8). For files larger than "
                            "~20KB consider edit_file instead — large content risks "
                            "max_tokens truncation that corrupts the tool call."
                        ),
                    },
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace an exact substring in an existing file with a new substring. Requires the old_string to appear EXACTLY ONCE in the file.\n"
                "Use when: making surgical changes to a known file, renaming a unique identifier, adjusting a specific line.\n"
                "Not for: creating new files (use write_file). Not for replacing strings that appear multiple times — widen old_string with surrounding context to force uniqueness.\n"
                "Output: confirmation 'Successfully edited PATH (replaced 1 occurrence)'.\n"
                "GOTCHA: fails with a count error if old_string appears 0 or 2+ times. Whitespace and indentation must match byte-for-byte. Prefer short context anchors over regex patterns."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit"},
                    "old_string": {"type": "string", "description": "Exact string to find"},
                    "new_string": {"type": "string", "description": "Replacement string"},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "Execute a shell command with configurable timeout and sandbox policy enforcement.\n"
                "Use when: running a compile/test/format command, git operations, quick system queries, any task that is naturally a CLI invocation.\n"
                "Not for: file reads (use read_file), file searches (use search_files/glob_files), pip installs (use pip_install for clear intent), date math (use datetime_calc).\n"
                "Output: stdout + stderr (labeled) + exit code. Commands run in the sandbox root (agent's working_dir).\n"
                "GOTCHA: dangerous commands may be blocked by sandbox policy. Max timeout 600s. Avoid long-running commands that buffer output — no stdout is returned until the process exits or the timeout fires."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds (default 30)",
                    },
                },
                "required": ["command"],
            },
        },
    },
    # run_tests — structured test execution. Block 2 Review loop
    # invokes this automatically after a step completes (when the step
    # declares `verify: {kind: "run_tests"}`), but the LLM can also call
    # it directly for ad-hoc "did my change pass?" checks.
    {
        "type": "function",
        "function": {
            "name": "run_tests",
            "description": (
                "Run the project's test suite and get a STRUCTURED result (pass/fail counts, "
                "failure list, framework detected). Auto-detects pytest / npm / go / cargo.\n"
                "Use when: after writing code (TDD loop), before declaring a step complete, "
                "verifying a bugfix actually landed. This is the canonical 'did it work' check — "
                "prefer it over ad-hoc `bash('pytest')` because the result is parsed, not raw stdout.\n"
                "Not for: starting dev servers or one-off scripts — use bash.\n"
                "Output: JSON with ok/passed/failed/skipped counts + up to 10 failure lines + "
                "trailing stdout for context. Exit code alone is NOT trusted — 0 pass tests also "
                "counts as failure.\n"
                "GOTCHA: for npm/jest, ensure `npm test` is wired in package.json. For go, runs "
                "`./...` by default (whole module). Pass `paths` to narrow scope."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "string",
                        "description": "Space-separated test paths/patterns. Empty = all tests in cwd.",
                    },
                    "framework": {
                        "type": "string",
                        "description": "Force framework: pytest | npm | go | cargo. Empty = auto-detect.",
                    },
                    "extra_args": {
                        "type": "string",
                        "description": "Additional CLI args appended verbatim (e.g. '-k test_foo' for pytest).",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Seconds, clamped to [10, 1800]. Default 600.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Regex-search file contents recursively (like `grep -rn`). Returns matching lines with path and line number.\n"
                "Use when: finding where a symbol is referenced, hunting a string/pattern across the repo, locating TODO/FIXME comments.\n"
                "Not for: finding files by name (use glob_files). Not for reading a specific file's content (use read_file). Hidden dirs and node_modules/__pycache__/.git are skipped automatically.\n"
                "Output: `path:lineno: matching_line` per match, truncated at 200 matches with a notice.\n"
                "GOTCHA: pattern is a Python regex — escape special chars. Very broad patterns return a truncated sample; narrow with `include` glob (e.g. '*.py') when scanning large trees."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regular expression pattern to search for"},
                    "path": {
                        "type": "string",
                        "description": "Directory or file to search in (default: current directory)",
                    },
                    "include": {
                        "type": "string",
                        "description": "Glob pattern to filter files, e.g. '*.py'",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "glob_files",
            "description": (
                "Find files matching a glob pattern by NAME/PATH. Returns sorted list of paths.\n"
                "Use when: listing files by extension (**/*.py), finding all tests (**/test_*.py), enumerating a subdirectory.\n"
                "Not for: searching inside file contents (use search_files). Not for reading (use read_file).\n"
                "Output: newline-separated paths, capped at 500 with a total-count notice.\n"
                "GOTCHA: uses Python pathlib glob semantics — `**` must be a full path segment (src/**/*.py works, src/**.py does not). Hidden directories are filtered out."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob pattern, e.g. '**/*.py' or 'src/**/*.js'",
                    },
                    "path": {
                        "type": "string",
                        "description": "Base directory for the search (default: current directory)",
                    },
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the public web via DuckDuckGo (API + HTML fallback). Returns ranked results with title/URL/snippet.\n"
                "Use when: finding documentation, recent news/events, research sources, third-party APIs.\n"
                "Not for: fetching the body of a specific known URL (use web_fetch). Not for searching the local filesystem (use search_files). No deep research chaining — call web_fetch on top results for details.\n"
                "Output: numbered list with title/URL/snippet blocks, capped at `max_results` (default 8).\n"
                "GOTCHA: DDG may rate-limit on burst — one search then reading several results is usually fine. Snippets are short; for substance always follow with web_fetch on the best 1-3 URLs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"},
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of results to return (default: 8)",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch a specific URL and extract plain text (strips script/style, decodes HTML entities).\n"
                "Use when: reading a documentation page, article, or API reference after finding it via web_search or when the user gives an explicit URL.\n"
                "Not for: discovering new URLs (use web_search). Not for JSON API calls (use http_request — it preserves status codes and headers). Not for PDF/binary URLs.\n"
                "Output: `[Content from URL]` header + extracted plain text, truncated to max_length (default 5000 chars).\n"
                "GOTCHA: default 5000-char cap is deliberate — research sessions that ran 10000+ chars/fetch burned 25k+ tokens of context. Raise max_length only when one URL genuinely needs full capture."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch"},
                    "max_length": {
                        "type": "integer",
                        "description": "Maximum number of characters to return (default: 10000)",
                    },
                },
                "required": ["url"],
            },
        },
    },
    # ---- MCP bridge ----
    {
        "type": "function",
        "function": {
            "name": "mcp_call",
            "description": (
                "🎯 作用对象: **外部服务**(真实邮箱、Slack 工作区、GitHub、数据库、浏览器、第三方 API 等)。"
                "**绝对不是**同进程的 agent 同事 —— 想给小刚/小专这种系统内 agent 派活,用 send_message / @ 提及,不要走这里。\n"
                "\n"
                "Invoke a tool on an external MCP server bound to this agent.\n"
                "Use when: sending emails to **real email addresses** / posting to Slack workspaces / hitting third-party APIs / driving a browser / etc.\n"
                "Not for: builtin tools above (call them directly). Not for talking to teammates inside this system. Not for discovering MCPs — pass list_mcps=true first to enumerate what's bound, then call with mcp_id + tool.\n"
                "Output: raw MCP tool response (JSON or text depending on the server).\n"
                "GOTCHA: `arguments` must be a JSON object — not a string. If you don't know what MCPs are available, call with list_mcps=true BEFORE guessing mcp_id. Errors include the MCP server name — check it's bound."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mcp_id": {
                        "type": "string",
                        "description": "The bound MCP id or name (e.g. 'email', 'slack', 'github')",
                    },
                    "tool": {
                        "type": "string",
                        "description": "The MCP tool name to invoke (e.g. 'send_email', 'send_message')",
                    },
                    "arguments": {
                        "oneOf": [{"type": "object"}, {"type": "string"}],
                        "description": "Arguments object to pass to the MCP tool. A JSON-encoded string is also accepted and auto-parsed.",
                    },
                    "list_mcps": {
                        "type": "boolean",
                        "description": "If true, list bound MCPs instead of calling one",
                    },
                },
            },
        },
    },
    # ---- Coordination tools (Claude Code architecture: TeamCreate / SendMessage / TaskList) ----
    {
        "type": "function",
        "function": {
            "name": "team_create",
            "description": (
                "Spawn a background sub-agent to run an independent task in parallel. Inherits caller's model/provider.\n"
                "Use when: a task splits into 2-3+ independent pieces that can run simultaneously (research → 3 aspects, refactor → multiple modules); total wall-clock is bounded by the longest sub-task.\n"
                "Not for: tasks that need your context/conversation history (the worker starts fresh). Not for simple question-answer delegation (use handoff_request for 1:1 work with visible ack). Not for scheduled jobs (use task_update with run_at).\n"
                "Output: worker label + transient task_id; the sub-agent posts its result back to your task list when done.\n"
                "GOTCHA: the worker is TRANSIENT — it disappears after completing. Its result lands in your task list, not as a chat message. Do NOT use for 'fire and forget logging' — use send_message instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name for the sub-agent"},
                    "role": {
                        "type": "string",
                        "description": "Role preset: coder, reviewer, researcher, tester, devops, writer",
                    },
                    "task": {"type": "string", "description": "Task description for the sub-agent to execute"},
                    "working_dir": {
                        "type": "string",
                        "description": "Working directory for the sub-agent (default: current dir)",
                    },
                },
                "required": ["name", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_message",
            "description": (
                "🎯 作用对象: **同进程内的另一个 agent**(系统内的 AI 同事,如小刚/小专),**不是**真人也**没有**邮箱。\n"
                "\n"
                "Send a structured message to another agent's inbox (in-process, async).\n"
                "Use envelope (summary/key_fields/artifact_refs) over long content. "
                "For blocking handoffs use handoff_request; for scheduled tasks use task_update.\n"
                "Not for: external email — use mcp_call for that. Not for posting to project group chat — write @ in your reply text instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to_agent": {
                        "type": "string",
                        "description": "Agent ID or name to send the message to",
                    },
                    "summary": {
                        "type": "string",
                        "description": "1-3 sentence conclusion. THIS is what the recipient mainly reads. Be concrete.",
                    },
                    "key_fields": {
                        "type": "object",
                        "description": "Structured payload — numbers, decisions, names, URLs, status. Keep it small (a handful of keys). Example: {\"decision\": \"B\", \"risk_level\": \"low\", \"target_env\": \"staging\"}.",
                    },
                    "artifact_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "File paths or artifact IDs pointing at large outputs. Recipient reads them with read_file if needed. Always prefer this over embedding a long body.",
                    },
                    "content": {
                        "type": "string",
                        "description": "(Legacy) optional raw body. Only use if you genuinely need inline detail that won't fit the summary and isn't big enough to warrant an artifact. If omitted, summary alone is fine.",
                    },
                    "msg_type": {
                        "type": "string",
                        "description": "Message type: task | info | result | question (default: task)",
                    },
                },
                "required": ["to_agent"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "handoff_request",
            "description": (
                "🎯 作用对象: **同进程内的另一个 agent**(系统内的 AI 同事),阻塞等结果。**不是**外部邮箱/真人。\n"
                "\n"
                "BLOCKING task transfer with 3-state handshake (pending → acknowledged → completed). "
                "Caller blocks until receiver returns or 600s timeout. "
                "For FYI broadcasts use send_message; for parallel independent work use team_create. "
                "Always include expected_output."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to_agent": {
                        "type": "string",
                        "description": "Target agent ID or name (the teammate picking up the work)",
                    },
                    "task": {
                        "type": "string",
                        "description": "What the receiver should do. Be concrete and self-contained — the receiver may not have your full context.",
                    },
                    "expected_output": {
                        "type": "string",
                        "description": "What the receiver should return (format / acceptance criteria). Optional but strongly recommended.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Any extra background the receiver needs (file paths, links, prior findings). Optional.",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Max wait time before marking the handoff as timed out (default 600).",
                    },
                },
                "required": ["to_agent", "task"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_update",
            "description": (
                "🎯 作用对象: **当前 project 的任务列表**(项目右栏 TASKS 显示的那些);"
                "在 solo / meeting context 下作用于 agent 个人任务。"
                "**不是**给队友派活的工具(派活用 @ 提及 / send_message / "
                "create_milestone with responsible_agent_id)。\n"
                "\n"
                "Create/update/complete/list shared task queue entries; "
                "registers recurring or delayed tasks with the scheduler. "
                "recurrence_spec: daily='HH:MM', weekly='DOW HH:MM', monthly='D HH:MM'. "
                "For your visible execution checklist use plan_update."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Action: create | update | complete | list",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Task ID (required for update/complete)",
                    },
                    "title": {"type": "string", "description": "Task title (for create)"},
                    "description": {"type": "string", "description": "Task description"},
                    "status": {
                        "type": "string",
                        "description": "New status: todo | in_progress | done | blocked",
                    },
                    "result": {"type": "string", "description": "Result summary (for complete)"},
                    "recurrence": {
                        "type": "string",
                        "description": (
                            "Recurrence type: once (default, one-time) | daily | weekly | "
                            "monthly | cron. Use 'daily' for 每天, 'weekly' for 每周, "
                            "'monthly' for 每月."
                        ),
                    },
                    "recurrence_spec": {
                        "type": "string",
                        "description": (
                            "Schedule spec: daily='HH:MM' (e.g. '09:00'), "
                            "weekly='DOW HH:MM' (DOW=SUN|MON|TUE|WED|THU|FRI|SAT, e.g. 'MON 09:00'), "
                            "monthly='D HH:MM' (e.g. '1 09:00'), cron='m h dom mon dow'."
                        ),
                    },
                    "run_at": {
                        "type": "string",
                        "description": (
                            "For delayed one-time tasks: when to execute. "
                            "Accepts '+Nm' (N minutes from now, e.g. '+5m'), "
                            "'+Nh' (N hours from now, e.g. '+2h'), "
                            "or 'HH:MM' (today at specific time, e.g. '18:30'). "
                            "When set, the scheduler will auto-trigger this task at "
                            "the specified time. Use this for '5分钟后', 'in 10 mins', "
                            "'下午3点' etc."
                        ),
                    },
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_inbox",
            "description": (
                "🎯 作用对象: **你自己**的系统内 inbox(其他 agent 通过 send_message / reply_message 发给你的消息)。**不是**真实邮件 inbox。\n"
                "\n"
                "Read your inbox — messages sent to you by other agents via send_message / reply_message.\n"
                "Use when: the plan calls for reviewing incoming handoffs, or you suspect teammates have pinged you since last turn.\n"
                "Not for: sending new messages (use send_message / reply_message). Not for ACKing (use ack_message).\n"
                "Output: compact list of unread messages with id / from / priority / timestamp / preview. Does NOT modify state — reading here doesn't mark messages as acked.\n"
                "GOTCHA: unread messages are ALSO auto-injected at the start of each chat turn, so you may not need to call this explicitly. Use it when you want to re-check mid-turn or include already-read items via include_read."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max messages to return (default 20, max 100).",
                    },
                    "include_read": {
                        "type": "boolean",
                        "description": "If true, also include recent read-but-not-acked messages (default false).",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ack_message",
            "description": (
                "Mark one or more inbox messages as acknowledged (state='acked'). Acked messages stop being surfaced in the auto-injected inbox block at chat start.\n"
                "Use when: you've read a message and either acted on it or decided no action is needed — this prevents it from re-appearing every turn.\n"
                "Not for: deleting messages (they remain queryable). Not for replying (use reply_message).\n"
                "Output: count of acked / skipped. Only YOUR messages can be acked — attempting to ack another agent's messages silently skips them.\n"
                "GOTCHA: messages are NOT auto-acked; merely being read in the auto-injection only transitions new→read. Ack is a deliberate 'I'm done with this' marker."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_ids": {
                        "type": "string",
                        "description": "One message id, or multiple ids separated by commas or whitespace (e.g. 'msg_abc, msg_def').",
                    },
                },
                "required": ["message_ids"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reply_message",
            "description": (
                "Reply to an inbox message (preserves thread_id). "
                "Use envelope (summary/key_fields/artifact_refs) over long content. "
                "For unrelated new pings use send_message; to silently mark done use ack_message."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "The id of the message you are replying to (from check_inbox or the auto-injected inbox block).",
                    },
                    "summary": {
                        "type": "string",
                        "description": "1-3 sentence conclusion / answer. Main thing the recipient reads.",
                    },
                    "key_fields": {
                        "type": "object",
                        "description": "Structured result — numbers, decisions, file paths. Keep it small.",
                    },
                    "artifact_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Paths to any large outputs produced. Recipient reads with read_file.",
                    },
                    "content": {
                        "type": "string",
                        "description": "(Legacy) raw body. Required only if no summary+structured fields provided.",
                    },
                    "priority": {
                        "type": "string",
                        "description": "urgent | normal | low (default normal).",
                    },
                    "ttl_s": {
                        "type": "integer",
                        "description": "Optional seconds-to-live; 0 (default) means never expire.",
                    },
                },
                "required": ["message_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "plan_update",
            "description": (
                "Live execution checklist for tasks with 3+ steps. "
                "Each step needs `acceptance` (concrete artifact/state proving done — "
                "vague '完成报告' is rejected). complete_step's result_summary must "
                "reference the acceptance. For background/scheduled tasks use task_update."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Action: create_plan | start_step | complete_step | add_step | fail_step | replan",
                    },
                    "task_summary": {
                        "type": "string",
                        "description": "Brief summary of the task (for create_plan)",
                    },
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string",
                                           "description": "Short step name, e.g. '搜索云厂商市场数据'"},
                                "detail": {"type": "string",
                                            "description": "Optional longer description"},
                                "acceptance": {"type": "string",
                                                "description": (
                                                    "REQUIRED. What does 'done' look like? One "
                                                    "concrete sentence naming the artifact or state. "
                                                    "Bad: '完成搜索'. Good: '至少 3 条来源且每条附 URL'."
                                                )},
                                "depends_on": {"type": "array", "items": {"type": "string"},
                                                "description": "Step IDs this step depends on"},
                                "llm_purpose": {
                                    "type": "string",
                                    "enum": ["tool-heavy", "multimodal",
                                             "reasoning", "analysis",
                                             "coding", "default"],
                                    "description": (
                                        "STRONGLY RECOMMENDED when auto-route is enabled. "
                                        "Pick the category that best fits this step:\n"
                                        "  - tool-heavy: mostly executes tools "
                                        "(read/write/bash/mcp_call, external I/O)\n"
                                        "  - multimodal: processes image/audio input\n"
                                        "  - reasoning:  deep comparison/weighing/derivation\n"
                                        "  - analysis:   long-form writing / synthesis / reporting\n"
                                        "  - coding:     writing/refactoring/debugging code\n"
                                        "  - default:    everything else\n"
                                        "The system uses this + the injected model-scores table "
                                        "to pick the highest-scoring LLM for this step."
                                    ),
                                },
                                "llm_rationale": {
                                    "type": "string",
                                    "description": (
                                        "Optional one-line justification for the llm_purpose "
                                        "choice (shown in the UI for traceability)."
                                    ),
                                },
                            },
                        },
                        "description": "List of step objects (for create_plan). Each step MUST include title + acceptance, and SHOULD include llm_purpose.",
                    },
                    "step_id": {
                        "type": "string",
                        "description": "Step ID to update (for start_step/complete_step/fail_step)",
                    },
                    "title": {
                        "type": "string",
                        "description": "Step title (for add_step)",
                    },
                    "detail": {
                        "type": "string",
                        "description": "Step detail (for add_step)",
                    },
                    "acceptance": {
                        "type": "string",
                        "description": "Acceptance criterion (for add_step)",
                    },
                    "result_summary": {
                        "type": "string",
                        "description": (
                            "REQUIRED for complete_step/fail_step. Describe what you produced "
                            "with specifics — file paths, counts, identifiers — so the reader "
                            "can verify the acceptance was actually met. Empty string is rejected."
                        ),
                    },
                },
                "required": ["action"],
            },
        },
    },
    # ---- Screenshot tool ----
    {
        "type": "function",
        "function": {
            "name": "web_screenshot",
            "description": (
                "Capture a PNG screenshot of a web page via Playwright (preferred) or CLI fallback (wkhtmltoimage/cutycapt).\n"
                "Use when: capturing visual state of a web page, generating thumbnails for a report, documenting UI.\n"
                "Not for: desktop screenshots (use desktop_screenshot). Not for screenshots of running preview dev servers inside the repo — use browser MCP with a session instead.\n"
                "Output: file path + size + viewport dimensions. The PNG lives at output_path (auto-generated in /tmp if unset).\n"
                "GOTCHA: requires Playwright installed (pip install playwright && playwright install chromium) — else falls back to CLI tools which may not be available. Default viewport 1280x720; `full_page=true` captures the full scroll."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to screenshot"},
                    "output_path": {
                        "type": "string",
                        "description": "File path to save the screenshot (default: auto-generated in workspace)",
                    },
                    "full_page": {
                        "type": "boolean",
                        "description": "Capture the full scrollable page (default: false, viewport only)",
                    },
                    "width": {
                        "type": "integer",
                        "description": "Viewport width in pixels (default: 1280)",
                    },
                    "height": {
                        "type": "integer",
                        "description": "Viewport height in pixels (default: 720)",
                    },
                },
                "required": ["url"],
            },
        },
    },
    # ---- HTTP request tool ----
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": (
                "Make any HTTP request (GET/POST/PUT/DELETE/PATCH) with custom headers, JSON body, and timeout.\n"
                "Use when: calling a REST API, hitting a webhook, testing an endpoint, anything that needs status code + response headers visible.\n"
                "Not for: plain text page fetches (use web_fetch — it strips HTML to text). Not for MCP-bound APIs (use mcp_call — it adds auth from the binding).\n"
                "Output: 'HTTP status METHOD URL' + headers (first 20) + body (capped at MAX_HTTP_RESPONSE_CHARS).\n"
                "GOTCHA: pass request bodies as json_body (dict) — it auto-sets Content-Type. Using `body` (string) requires you to set Content-Type manually. Max timeout 120s."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to request"},
                    "method": {
                        "type": "string",
                        "description": "HTTP method: GET, POST, PUT, DELETE, PATCH (default: GET)",
                    },
                    "headers": {
                        "type": "object",
                        "description": "Request headers as key-value pairs",
                    },
                    "body": {
                        "type": "string",
                        "description": "Request body (string or JSON string)",
                    },
                    "json_body": {
                        "type": "object",
                        "description": "Request body as JSON object (auto-sets Content-Type)",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Request timeout in seconds (default: 30)",
                    },
                },
                "required": ["url"],
            },
        },
    },
    # ---- DateTime calculation tool ----
    {
        "type": "function",
        "function": {
            "name": "datetime_calc",
            "description": (
                "Date/time operations: current time, date differences, add duration, format conversion, timezone conversion.\n"
                "Use when: computing time intervals, converting between timezones, formatting dates for display.\n"
                "Not for: scheduling tasks (use task_update with run_at). Not for parsing relative text like '5分钟后' — that is task_update's job; here `date` must be a concrete date string.\n"
                "Output: human-readable summary plus ISO representation for downstream tool calls.\n"
                "GOTCHA: accepts many date formats (ISO / YYYY-MM-DD / YYYY/MM/DD / etc) but a few like '今天' are NOT parsed — use action=now + timezone for 'current time in zone'. For 'convert', naive dates are assumed UTC."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": (
                            "Action: 'now' (current time), 'diff' (difference between dates), "
                            "'add' (add duration to date), 'format' (reformat a date), "
                            "'convert' (convert timezone)"
                        ),
                    },
                    "date": {
                        "type": "string",
                        "description": "Date string (ISO format preferred, e.g. '2024-03-15T10:30:00')",
                    },
                    "date2": {
                        "type": "string",
                        "description": "Second date for 'diff' action",
                    },
                    "days": {"type": "integer", "description": "Days to add (for 'add' action)"},
                    "hours": {"type": "integer", "description": "Hours to add (for 'add' action)"},
                    "minutes": {"type": "integer", "description": "Minutes to add (for 'add' action)"},
                    "timezone": {
                        "type": "string",
                        "description": "Timezone name (e.g. 'Asia/Shanghai', 'US/Eastern', 'UTC')",
                    },
                    "format": {
                        "type": "string",
                        "description": "Output format string (Python strftime, e.g. '%%Y-%%m-%%d %%H:%%M')",
                    },
                },
                "required": ["action"],
            },
        },
    },
    # ---- JSON process tool ----
    {
        "type": "function",
        "function": {
            "name": "json_process",
            "description": (
                "Parse / extract / transform / validate JSON data. Can read from a string or a file path.\n"
                "Use when: validating a JSON blob, extracting nested fields with a path expression, flattening / merging / converting to CSV.\n"
                "Not for: raw text manipulation (use text_process). Not for writing JSON to disk (use write_file after json.dumps). The file-path mode is read-only.\n"
                "Output: formatted text. Results capped at 10000 chars (MAX_JSON_RESULT_CHARS for extract) — narrow your path if truncated.\n"
                "GOTCHA: path syntax is JSONPath-like but simplified — users[0].name or data.items works; JSONPath filters ($.., [?...]) do NOT. to_csv expects an array of flat objects."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": (
                            "Action: 'parse' (validate & pretty-print), 'extract' (extract field), "
                            "'keys' (list top-level keys), 'flatten' (flatten nested), "
                            "'to_csv' (JSON array to CSV), 'from_csv' (CSV to JSON), "
                            "'merge' (merge two JSON objects), 'count' (count items)"
                        ),
                    },
                    "data": {
                        "type": "string",
                        "description": "JSON string or file path to process",
                    },
                    "path": {
                        "type": "string",
                        "description": "JSONPath-like expression for 'extract' (e.g. 'users[0].name', 'data.items')",
                    },
                    "data2": {
                        "type": "string",
                        "description": "Second JSON string for 'merge' action",
                    },
                },
                "required": ["action", "data"],
            },
        },
    },
    # ---- Text process tool ----
    {
        "type": "function",
        "function": {
            "name": "text_process",
            "description": (
                "Batch text transforms: count / find+replace (regex) / extract / sort / dedup / base64 / url-encode / hash / head / tail / split.\n"
                "Use when: one-off text manipulation that would otherwise need a bash pipeline (grep | sort | uniq).\n"
                "Not for: processing JSON (use json_process). Not for operations on files — pass the file content via read_file first. Regex uses Python syntax.\n"
                "Output: transformed text, capped at 10000 chars. 'count' returns lines/words/chars; 'hash' returns algorithm:hex.\n"
                "GOTCHA: `replace` uses re.sub — backreferences are \\1, not $1. `dedup` keeps insertion order; if you want sort+unique call `sort` then `dedup`."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": (
                            "Action: 'count' (word/line/char count), 'replace' (find & replace), "
                            "'extract' (extract regex matches), 'sort' (sort lines), "
                            "'dedup' (remove duplicates), 'base64_encode', 'base64_decode', "
                            "'url_encode', 'url_decode', 'hash' (md5/sha256), 'head' (first N lines), "
                            "'tail' (last N lines), 'split' (split by delimiter)"
                        ),
                    },
                    "text": {"type": "string", "description": "Input text to process"},
                    "pattern": {
                        "type": "string",
                        "description": "Regex pattern (for replace/extract)",
                    },
                    "replacement": {
                        "type": "string",
                        "description": "Replacement string (for replace)",
                    },
                    "n": {
                        "type": "integer",
                        "description": "Number of lines (for head/tail, default: 10)",
                    },
                    "algorithm": {
                        "type": "string",
                        "description": "Hash algorithm: md5, sha256, sha1 (for hash, default: sha256)",
                    },
                    "delimiter": {
                        "type": "string",
                        "description": "Delimiter (for split, default: newline)",
                    },
                },
                "required": ["action", "text"],
            },
        },
    },
    # ---- Wiki ingest (V2 Karpathy-pattern: markdown wiki layer) ----
    # Replaces save_experience long-term. Writes a markdown page with
    # structured front-matter into the wiki layer; agent retrieves via
    # knowledge_lookup. Each scope has an auto-maintained index.md.
    {
        "type": "function",
        "function": {
            "name": "wiki_ingest",
            "description": (
                "★ PRIMARY tool for saving any reusable knowledge / experience / "
                "methodology / template the agent learns or distills. "
                "Writes a markdown page to the wiki layer (auto-indexed, "
                "queryable via knowledge_lookup, injected into future role "
                "prompts as a title-only index). "
                "kinds: experience (scene + rules) | methodology (workflow / "
                "steps) | template (writing pattern) | pattern (recurring "
                "logic) | reference (specs / wiki). "
                "scope: omit for role-scoped; pass scope='global' for "
                "cross-role sharing. "
                "Use this INSTEAD of save_experience (deprecated)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["experience", "methodology",
                                 "template", "pattern", "reference"],
                        "description": "Page kind. experience=lessons; methodology=how-tos; template=writing/structure; pattern=recurring logic; reference=specs/standards.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Human-readable title (used for slug + index).",
                    },
                    "body": {
                        "type": "string",
                        "description": "Full markdown body. Self-contained — readers won't have other context.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags for search.",
                    },
                    "scope": {
                        "type": "string",
                        "description": "Empty (default) → role-scoped; 'global' → shared across roles.",
                    },
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional: source paths/URLs that informed this page.",
                    },
                    "related": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional: related page slugs (e.g. 'experience/saudi-cloud').",
                    },
                },
                "required": ["kind", "title", "body"],
            },
        },
    },
    # ---- Experience persistence (legacy; use wiki_ingest going forward) ----
    # NOTE: 经验条目(experience) 写入 experience_library 对应角色分桶。
    # 当经验积累到一定程度, agent 可通过 propose_skill 工具提议将经验
    # 锻造为技能(skill), 提交管理员审批后正式导入技能商店。
    {
        "type": "function",
        "function": {
            "name": "save_experience",
            "description": (
                "[DEPRECATED — use `wiki_ingest` instead]. "
                "Legacy tool kept for back-compat only. New code MUST call "
                "wiki_ingest(kind='experience', title, body) which writes a "
                "markdown page to the wiki layer. Calling save_experience "
                "now writes to the legacy JSON store that is no longer "
                "auto-injected into prompts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "scene": {
                        "type": "string",
                        "description": "Trigger scenario / when this experience applies",
                    },
                    "core_knowledge": {
                        "type": "string",
                        "description": "Core insight / knowledge point",
                    },
                    "action_rules": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "1-3 positive action rules (do-this)",
                    },
                    "taboo_rules": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "1-2 taboo rules (avoid-this)",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "Importance; default medium",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional classification tags",
                    },
                    "exp_type": {
                        "type": "string",
                        "enum": ["retrospective", "active_learning"],
                        "description": "retrospective = 复盘产出; active_learning = 主动学习产出",
                    },
                    "source": {
                        "type": "string",
                        "description": "Human-readable origin (e.g. 'POC 贪吃蛇 产品复盘')",
                    },
                    "role": {
                        "type": "string",
                        "description": "Override the role bucket; defaults to the calling agent's role",
                    },
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Citation references pointing to the source of "
                            "truth for this lesson. Conventional formats: "
                            "'path/to/file.py:LINE' (code), "
                            "'docs/SPEC.md#section' (doc anchor), "
                            "or a URL. Listed when the experience is injected "
                            "into future prompts so agents/reviewers can jump "
                            "back to the raw evidence. Dedup + whitespace-trim "
                            "applied automatically."
                        ),
                    },
                },
                "required": ["scene", "core_knowledge"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "knowledge_lookup",
            "description": (
                "Search KB (shared + agent's expert pool). "
                "ONE-SHOT per turn: pack all keywords into a single query (second call rejected). "
                "Modes: search (top-k content, default), count (exact aggregate by source_file), "
                "list (metadata inventory, no content). "
                "Cite hits as [source_file §heading_path]; reason only from retrieved content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keyword / substring. Required for mode=search; optional filter for mode=count and mode=list.",
                    },
                    "entry_id": {
                        "type": "string",
                        "description": "Specific entry ID to read (from a previous search result)",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["search", "count", "list"],
                        "description": "Retrieval mode. search=top-k content chunks; count=exact aggregate by source_file (for how-many questions); list=per-chunk metadata inventory.",
                    },
                },
            },
        },
    },
    # ---- Agent-private L3 memory recall (新 A) ----
    {
        "type": "function",
        "function": {
            "name": "memory_recall",
            "description": (
                "Query YOUR OWN agent-private long-term memory. "
                "ONE-SHOT per turn: pack all keywords in one query (second call rejected). "
                "Returns top-K facts by similarity; check before fresh web_search/fetch. "
                "For cross-role reference use knowledge_lookup."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What are you trying to remember? (topic / keywords / question)",
                    },
                    "category": {
                        "type": "string",
                        "description": "Optional filter: intent | reasoning | outcome | rule | reflection (omit for all).",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Max hits (default 5, max 20).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    # ---- Cross-agent knowledge sharing ----
    {
        "type": "function",
        "function": {
            "name": "share_knowledge",
            "description": (
                "Write a new entry to the shared knowledge base so ALL agents can access it via knowledge_lookup.\n"
                "Use when: you have produced a reusable playbook / template / reference that teams would benefit from — API error handling patterns, PPTX best practices, design conventions.\n"
                "Not for: role-local learnings (use save_experience — that stays in one role's bucket). Not for chat-log content. Not for secrets or sensitive data (the KB is shared).\n"
                "Output: 'Knowledge shared' confirmation with entry id. Source attribution (your agent name/role) is auto-appended to the content.\n"
                "GOTCHA: title is the primary search key — make it descriptive. Write content with retrieval in mind: include trigger keywords someone would search for."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Concise title for the knowledge entry",
                    },
                    "content": {
                        "type": "string",
                        "description": "Detailed knowledge content — include steps, tips, examples, templates as needed",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Tags for categorization, e.g. ['pptx', 'design', 'template']",
                    },
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "learn_from_peers",
            "description": (
                "Import high-quality experiences from another role's library into your own bucket.\n"
                "Use when: you need a capability another role has — e.g. a PM agent learning design heuristics from designer's experiences, a coder learning test patterns from QA's.\n"
                "Not for: one-shot reference lookup (use knowledge_lookup). Not for learning from a specific agent — this is ROLE-level only.\n"
                "Output: imported experiences list with priority / scene / rules / success-rate summary.\n"
                "GOTCHA: imports are FILTERED — only experiences >=75% success rate come through. Returns 0 if the source role has no matching experiences. Topic is a keyword filter, not a semantic query."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source_role": {
                        "type": "string",
                        "description": "The role to learn from, e.g. 'designer', 'coder', 'analyst'",
                    },
                    "topic": {
                        "type": "string",
                        "description": "Specific topic to search for, e.g. 'PPTX creation', 'API design'",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max number of experiences to import (default 5)",
                    },
                },
                "required": ["source_role"],
            },
        },
    },
    # ---- Web login request (human-in-the-loop) ----
    {
        "type": "function",
        "function": {
            "name": "request_web_login",
            "description": (
                "Show the user an interactive login card to authenticate into a specific website before the agent proceeds.\n"
                "Use when: you know upfront the task requires login (e.g. 'help me look at that Jira issue') and want to get credentials/cookies BEFORE navigating.\n"
                "Not for: reactive login walls hit during browsing — those are handled automatically by the browser layer. Not for API key configuration (use the account settings UI).\n"
                "Output: interactive card rendered in the chat; user completes login, then the agent can proceed.\n"
                "GOTCHA: provide a clear `reason` — the card asks the user to trust you with credentials, and an opaque 'I need to log in' message is often declined."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL that requires login",
                    },
                    "site_name": {
                        "type": "string",
                        "description": "Human-readable site name, e.g. 'GitHub', 'Jira', '企业微信'",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why you need access — what task requires this login",
                    },
                    "login_url": {
                        "type": "string",
                        "description": "Optional: the specific login page URL if different from the target URL",
                    },
                },
                "required": ["url", "site_name", "reason"],
            },
        },
    },
    # ---- Package management tool ----
    {
        "type": "function",
        "function": {
            "name": "pip_install",
            "description": (
                "Install or upgrade Python packages via pip (uses --break-system-packages for system Python).\n"
                "Use when: a specific package is missing for a downstream tool (e.g. pptx auto-install already calls this internally; explicit use when you know exactly which package is needed).\n"
                "Not for: generic shell commands (use bash). Not for non-Python deps (use bash with apt/brew).\n"
                "Output: '✓ Successfully installed: names' on success or pip's stderr on failure. Max timeout 300s.\n"
                "GOTCHA: writes to the agent's system Python — affects ALL agents on this node, not just you. Prefer local venv / uv install for reversible installs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "packages": {
                        "type": "string",
                        "description": "Space-separated package names to install (e.g., 'requests numpy pandas')",
                    },
                    "upgrade": {
                        "type": "boolean",
                        "description": "Whether to upgrade packages to the latest version (default: false)",
                    },
                },
                "required": ["packages"],
            },
        },
    },
    # ---- PowerPoint creation tool ----
    {
        "type": "function",
        "function": {
            "name": "create_pptx",
            "description": (
                "Create a simple PowerPoint .pptx with title/content slides. Layout picked from {title, content, title_content, blank}. Auto-installs python-pptx.\n"
                "Use when: the user wants a basic deck — bullet points, section titles, maybe one image per slide.\n"
                "Not for: complex layouts with charts/shapes/tables (use create_pptx_advanced). Not for editing existing pptx files (this overwrites the output_path).\n"
                "Output: file created at output_path; returns '✓ Created presentation: PATH'.\n"
                "GOTCHA: content is rendered as bullet points split on newlines — do not expect markdown formatting. For visual design beyond bullets use create_pptx_advanced."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "output_path": {
                        "type": "string",
                        "description": "Path where the .pptx file will be saved",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional title for the presentation deck",
                    },
                    "slides": {
                        "type": "array",
                        "description": "Array of slide objects, each with title, content, optional layout, and optional images",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {
                                    "type": "string",
                                    "description": "Slide title",
                                },
                                "content": {
                                    "type": "string",
                                    "description": "Slide content (bullet text or paragraphs)",
                                },
                                "layout": {
                                    "type": "string",
                                    "description": "Layout type: 'title', 'content', 'title_content', 'blank' (default: 'title_content')",
                                },
                                "images": {
                                    "type": "array",
                                    "description": "Optional list of images to place on the slide",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "path": {"type": "string", "description": "Image file path"},
                                            "left": {"type": "number", "description": "Left position in inches (default 1)"},
                                            "top": {"type": "number", "description": "Top position in inches (default 2)"},
                                            "width": {"type": "number", "description": "Width in inches (0=auto)"},
                                            "height": {"type": "number", "description": "Height in inches (0=auto)"},
                                        },
                                        "required": ["path"],
                                    },
                                },
                            },
                            "required": ["title", "content"],
                        },
                    },
                },
                "required": ["output_path", "slides"],
            },
        },
    },
    # ---- Advanced PPTX tool ----
    {
        "type": "function",
        "function": {
            "name": "create_pptx_advanced",
            "description": (
                "Create a design-rich PowerPoint with shapes, charts, tables, multi-column layouts, and infographics. Layout types: cover / toc / section / cards / process / kpi / comparison / timeline / chart / table / closing.\n"
                "Use when: building a presentation that needs visual design — cover + TOC + content + charts + closing.\n"
                "Not for: simple bullet-only decks (use create_pptx). Not for editing existing pptx (this overwrites).\n"
                "Output: .pptx saved at output_path; returns '✓ Created advanced presentation (N slides): PATH'.\n"
                "GOTCHA: ❌ do NOT invent layout.type strings (overview/analysis/content/summary all get silently downgraded to 'cards'). ✅ for normal content pages use `cards` (1-9 items). Element x/y/w/h are in INCHES and auto-clamped to slide bounds (10.0 x 5.625)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "output_path": {
                        "type": "string",
                        "description": "输出 .pptx 文件路径",
                    },
                    "theme": {
                        "type": "object",
                        "description": "全局配色主题",
                        "properties": {
                            "primary": {"type": "string", "description": "主色 hex (如 'E8590C')"},
                            "secondary": {"type": "string", "description": "辅色 hex (如 '2B2B2B')"},
                            "accent": {"type": "string", "description": "强调色 hex (如 'F4A261')"},
                            "background": {"type": "string", "description": "默认背景色 hex (如 'FFFFFF')"},
                            "title_font": {"type": "string", "description": "标题字体 (如 'Microsoft YaHei')"},
                            "body_font": {"type": "string", "description": "正文字体 (如 'Microsoft YaHei')"},
                        },
                    },
                    "slides": {
                        "type": "array",
                        "description": "页面数组。推荐用layout自动排版，也可用elements手动控制，或两者结合。",
                        "items": {
                            "type": "object",
                            "properties": {
                                "layout": {
                                    "type": "object",
                                    "description": "智能布局（推荐）。设置type和items，工具自动计算坐标。普通内容页请统一使用 cards。示例: {\"type\":\"process\",\"title\":\"流程\",\"page_num\":3,\"items\":[{\"title\":\"步骤1\",\"detail\":\"说明\"}]}",
                                    "properties": {
                                        "type": {
                                            "type": "string",
                                            "enum": [
                                                "cover", "toc", "section",
                                                "cards", "grid", "grid_2x2",
                                                "grid_2x3", "two_column",
                                                "three_column", "process",
                                                "kpi", "comparison", "timeline",
                                                "chart", "chart_page",
                                                "table", "table_page", "closing",
                                            ],
                                            "description": "布局类型。cover=封面；toc=目录；section=章节分隔；cards=通用内容卡片（普通内容页首选）；process=流程步骤；kpi=关键指标；comparison=左右对比；timeline=时间轴；chart/chart_page=图表页；table/table_page=表格页；closing=结尾页。不要传其它字符串——未注册类型会降级为 cards 并打 warning。",
                                        },
                                        "title": {"type": "string", "description": "页面标题"},
                                        "page_num": {"type": "integer", "description": "页码编号"},
                                        "items": {"type": "array", "description": "内容项数组，结构因布局类型而异"},
                                        "subtitle": {"type": "string", "description": "[cover/closing] 副标题"},
                                        "date": {"type": "string", "description": "[cover] 日期"},
                                        "author": {"type": "string", "description": "[cover] 作者"},
                                        "left": {"type": "object", "description": "[comparison] 左侧 {title, items:[]}"},
                                        "right": {"type": "object", "description": "[comparison] 右侧 {title, items:[]}"},
                                        "headers": {"type": "array", "description": "[table] 表头"},
                                        "rows": {"type": "array", "description": "[table] 数据行"},
                                        "summary": {"type": "string", "description": "底部说明文字"},
                                    },
                                },
                                "background": {
                                    "type": "string",
                                    "description": "页面背景色 hex，覆盖主题默认值",
                                },
                                "elements": {
                                    "type": "array",
                                    "description": "手动元素数组（可与layout组合使用，手动元素追加在layout自动元素之后）。每个元素须有type和x,y,w,h(英寸)。",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "type": {"type": "string", "description": "元素类型: text|shape|chart|table|image|icon_circle|line"},
                                            "x": {"type": "number", "description": "左边距(英寸)"},
                                            "y": {"type": "number", "description": "上边距(英寸)"},
                                            "w": {"type": "number", "description": "宽度(英寸)"},
                                            "h": {"type": "number", "description": "高度(英寸)"},
                                            "content": {"type": "string", "description": "[text] 文本内容，支持\\n换行"},
                                            "font_size": {"type": "number", "description": "[text/icon_circle] 字号(pt)"},
                                            "font_name": {"type": "string", "description": "[text] 字体名"},
                                            "bold": {"type": "boolean", "description": "[text] 是否粗体"},
                                            "italic": {"type": "boolean", "description": "[text] 是否斜体"},
                                            "color": {"type": "string", "description": "[text/icon_circle] 字体颜色 hex"},
                                            "bg_color": {"type": "string", "description": "[text] 文本框背景色 hex"},
                                            "align": {"type": "string", "description": "[text] 对齐: left|center|right"},
                                            "valign": {"type": "string", "description": "[text] 垂直对齐: top|middle|bottom"},
                                            "line_spacing": {"type": "number", "description": "[text] 行间距倍数(如1.5)"},
                                            "shape_type": {"type": "string", "description": "[shape] 形状: rectangle|rounded_rect|oval|triangle|arrow_right|arrow_left|chevron|diamond|pentagon|hexagon|star"},
                                            "fill_color": {"type": "string", "description": "[shape/icon_circle] 填充色 hex"},
                                            "line_color": {"type": "string", "description": "[shape/line] 线条颜色 hex"},
                                            "line_width": {"type": "number", "description": "[shape/line] 线宽(pt)"},
                                            "rotation": {"type": "number", "description": "[shape] 旋转角度(度)"},
                                            "chart_type": {"type": "string", "description": "[chart] 图表类型: bar|column|line|pie|doughnut|radar|area"},
                                            "categories": {"type": "array", "items": {"type": "string"}, "description": "[chart] 分类标签"},
                                            "series": {"type": "array", "description": "[chart] 数据系列 [{name,values}]"},
                                            "colors": {"type": "array", "items": {"type": "string"}, "description": "[chart] 系列颜色数组"},
                                            "show_labels": {"type": "boolean", "description": "[chart] 显示数据标签"},
                                            "show_percent": {"type": "boolean", "description": "[chart] 显示百分比(饼图)"},
                                            "show_legend": {"type": "boolean", "description": "[chart] 显示图例"},
                                            "headers": {"type": "array", "items": {"type": "string"}, "description": "[table] 表头"},
                                            "rows": {"type": "array", "description": "[table] 数据行 [[cell,...],...]"},
                                            "header_color": {"type": "string", "description": "[table] 表头背景色"},
                                            "header_font_color": {"type": "string", "description": "[table] 表头字色"},
                                            "stripe_color": {"type": "string", "description": "[table] 斑马纹颜色"},
                                            "path": {"type": "string", "description": "[image] 图片文件路径"},
                                            "text": {"type": "string", "description": "[icon_circle] 圆内文字"},
                                            "font_color": {"type": "string", "description": "[icon_circle] 文字颜色"},
                                        },
                                        "required": ["type"],
                                    },
                                },
                            },
                            "required": [],
                        },
                    },
                },
                "required": ["output_path", "slides"],
            },
        },
    },
    # ---- Desktop screenshot tool ----
    {
        "type": "function",
        "function": {
            "name": "desktop_screenshot",
            "description": (
                "Capture a screenshot of the local desktop primary monitor. Optional region crop.\n"
                "Use when: the user asks to 'screenshot what is on screen', or agent needs to capture an external app's UI that is not accessible via the browser.\n"
                "Not for: web page screenshots (use web_screenshot). Not for the agent's own Portal UI — the agent cannot see its own browser window meaningfully.\n"
                "Output: '✓ Screenshot saved: PATH' after writing a PNG. Default path auto-generated with timestamp.\n"
                "GOTCHA: requires mss or Pillow installed, else falls back to OS tools (scrot on Linux, screencapture on macOS). On headless machines this tool CANNOT work — no X display."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "output_path": {
                        "type": "string",
                        "description": "Optional path where the PNG will be saved (defaults to auto-generated path in working directory)",
                    },
                    "region": {
                        "type": "object",
                        "description": "Optional region to crop (x, y, w, h coordinates)",
                        "properties": {
                            "x": {"type": "integer", "description": "Top-left X coordinate"},
                            "y": {"type": "integer", "description": "Top-left Y coordinate"},
                            "w": {"type": "integer", "description": "Width in pixels"},
                            "h": {"type": "integer", "description": "Height in pixels"},
                        },
                    },
                },
            },
        },
    },
    # ---- Video creation tool ----
    {
        "type": "function",
        "function": {
            "name": "create_video",
            "description": (
                "Stitch image frames into an MP4 video. Optional audio track. Auto-installs moviepy.\n"
                "Use when: producing a slideshow video from generated or captured images (e.g. time-lapse, animated explainer, tutorial).\n"
                "Not for: recording live video (no capture capability). Not for editing existing videos.\n"
                "Output: .mp4 at output_path. Returns '✓ Video created: PATH'.\n"
                "GOTCHA: each frame needs a `duration` (default 3s) — total video length = sum of durations. Audio track is trimmed to match total video length. moviepy install is SLOW on first use (60s timeout)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "output_path": {
                        "type": "string",
                        "description": "Path where the .mp4 video file will be saved",
                    },
                    "frames": {
                        "type": "array",
                        "description": "Array of frame objects with image_path and optional duration",
                        "items": {
                            "type": "object",
                            "properties": {
                                "image_path": {
                                    "type": "string",
                                    "description": "Path to the image file",
                                },
                                "duration": {
                                    "type": "number",
                                    "description": "Duration in seconds to display this frame (default: 3)",
                                },
                            },
                            "required": ["image_path"],
                        },
                    },
                    "fps": {
                        "type": "integer",
                        "description": "Frames per second for the video (default: 24)",
                    },
                    "audio_path": {
                        "type": "string",
                        "description": "Optional path to audio file to add as soundtrack",
                    },
                },
                "required": ["output_path", "frames"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_skill_guide",
            "description": (
                "Load a granted skill's guide. brief mode (default, ~200 tokens) vs "
                "verbose (full body). cd to returned skill_dir before running scripts. "
                "NOT for MCP — use mcp_call. NOT for registering — use submit_skill."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name (e.g. 'pdf', 'docx', 'xlsx')",
                    },
                    "brief": {
                        "type": "boolean",
                        "description": "Default true — return headings/summary only. Set false to load the full body (much larger).",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "Optional agent ID to resolve agent-local skill path",
                    },
                },
                "required": ["name"],
            },
        },
    },
    # ---- Skill generation (propose a new skill from accumulated experiences) ----
    {
        "type": "function",
        "function": {
            "name": "propose_skill",
            "description": (
                "Scan the experience library for clusterable patterns and auto-generate a skill draft (SKILL.md + manifest.yaml) pending admin approval.\n"
                "Use when: you have accumulated 3+ similar high-success experiences on a specific topic and want to promote them into a reusable skill package.\n"
                "Not for: submitting a hand-written skill package (use submit_skill). Not for viewing existing skills (use get_skill_guide).\n"
                "Output: draft summary with id / description / confidence / export directory / status=pending-approval. The draft is visible to admins in the Portal review queue.\n"
                "GOTCHA: requires >=3 similar experiences with >=75% success rate — returns a 'no patterns found' message if the bar is not met. Drafts need ADMIN APPROVAL before they become real skills — do not assume the skill exists right after calling."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "description": "Limit scan to experiences of this role (empty = all roles)",
                    },
                    "topic": {
                        "type": "string",
                        "description": "Optional topic hint to guide which experience cluster to target",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_skill",
            "description": (
                "Submit a hand-written skill package from your workspace directory for admin approval. Requires manifest.yaml + SKILL.md in the dir.\n"
                "Use when: you have authored a skill (e.g. via writing files in your workspace) and want it reviewed for inclusion in the Skill Store.\n"
                "Not for: auto-proposing from experiences (use propose_skill). Not for installing a granted skill — skills appear automatically after admin approval.\n"
                "Output: draft id + name + runtime + code files list + 'awaiting admin approval' status.\n"
                "GOTCHA: manifest.yaml MUST include name/version/description/runtime/author/entry. runtime must be one of python/shell/markdown. Same name+version combo is rejected — bump version to resubmit. Python skills' entry file must define `def run(ctx, **kwargs)` and cannot use open/exec/eval (sandbox forbids)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dir_name": {
                        "type": "string",
                        "description": "Name of the skill directory in your workspace (e.g. 'pptx_skill')",
                    },
                },
                "required": ["dir_name"],
            },
        },
    },
    # ── Project-scope tools ─────────────────────────────────────────────
    # These tools auto-discover the current project via thread-local
    # context (set by ProjectChatEngine). They will no-op with an error
    # if called from outside a project chat and no project_id is given.
    {
        "type": "function",
        "function": {
            "name": "propose_decomposition",
            "description": (
                "Propose a decomposition of the CURRENT project task into N parallel sub-tasks for multiple agents.\n"
                "Use when: the task is too big for one agent (multi-module code project, multi-chapter report) and can be cleanly split.\n"
                "DOES NOT immediately create or assign tasks — it persists a draft. The user must confirm in the UI before any sub-task gets dispatched. After calling this, STOP and tell the user to confirm; do not start any sub-task work yourself.\n"
                "Output: draft_id + sub_task_count. Each sub_task should have title, role_hint (coder/researcher/general/advisor), output_path (relative to project root), acceptance criteria, and depends_on (list of earlier sub_task ids).\n"
                "GOTCHA: parent_task_id is required and must point to the big task in the current project. Use unique sub_task ids if you specify them; otherwise leave blank (auto-minted). depends_on entries must reference sub_task ids in the same proposal."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "parent_task_id": {"type": "string", "description": "ProjectTask id being decomposed"},
                    "title": {"type": "string", "description": "Short label, e.g. 'Decompose: build admin panel'"},
                    "summary": {"type": "string", "description": "Plain-language pitch of the strategy"},
                    "prd": {"type": "string", "description": "Optional PRD content (markdown). Required when prd_source=agent_generated. Leave empty to use whatever PRD.md the user already uploaded."},
                    "scaffold_dirs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Directories to mkdir under project root before sub-tasks start (e.g., ['backend/auth', 'frontend/pages'])"
                    },
                    "sub_tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "Optional stable id (e.g. 'st_auth'); auto-minted if omitted. Used in depends_on refs."},
                                "title": {"type": "string"},
                                "description": {"type": "string"},
                                "role_hint": {"type": "string", "enum": ["coder", "researcher", "general", "advisor"]},
                                "output_path": {"type": "string", "description": "Relative dir under project root that becomes this agent's wd"},
                                "acceptance": {"type": "string", "description": "One-line 'done when X' criterion"},
                                "order": {"type": "integer"},
                                "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Sub-task ids that must DONE before this can run"}
                            },
                            "required": ["title"]
                        }
                    }
                },
                "required": ["parent_task_id", "sub_tasks"]
            }
        }
    },
    # ── Shared-context tools (project-scoped DB for multi-agent coord) ──
    {
        "type": "function",
        "function": {
            "name": "sc_query",
            "description": (
                "Query the project's shared context database. Token-cheap "
                "alternative to dumping content through chat — pull just the "
                "rows you need.\n"
                "Tables: artifacts (file refs with summary), decisions "
                "(structured choices made), milestones (project goals/phases), "
                "handoffs (agent→agent assignments), pending_qs (open Q&A). "
                "Pass table='summary' (or omit) to get a compact project state "
                "overview.\n"
                "Filters apply per-table: kind+status for artifacts, status for "
                "decisions/milestones, dst_agent+status for handoffs/pending_qs. "
                "Returns JSON {table, count, rows}.\n"
                "Use this BEFORE asking another agent — the answer may already "
                "exist."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {"type": "string", "enum": ["artifacts", "decisions", "milestones", "handoffs", "pending_qs", "summary"]},
                    "kind": {"type": "string", "description": "Filter (artifacts only): document | code | data | image | report | config"},
                    "status": {"type": "string", "description": "Filter by row status (varies by table)"},
                    "dst_agent": {"type": "string", "description": "Filter handoffs/pending_qs by destination agent"},
                    "since_ts": {"type": "number", "description": "Unix ts; only return rows newer than this"},
                    "limit": {"type": "integer", "description": "Max rows (default 10, capped at 50)"},
                    "project_id": {"type": "string", "description": "Optional; inferred from chat context"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sc_register_artifact",
            "description": (
                "Record a workspace file as a sharable artifact reference so "
                "other agents can find it via sc_query(table='artifacts'). "
                "ONLY register the *card* (path + title + ≤200 char summary); "
                "the full file stays in the workspace.\n"
                "Use when: you produced a file other agents will need (a "
                "document draft, code module, data table, image). "
                "DON'T use for ephemeral intermediates.\n"
                "Returns: ``OK · registered artifact id=art_*`` — pass that id "
                "to sc_handoff or include in your text reply."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative file path"},
                    "title": {"type": "string", "description": "Human-readable title (defaults to path)"},
                    "summary": {"type": "string", "description": "≤200 char content preview"},
                    "kind": {"type": "string", "enum": ["document", "code", "data", "image", "report", "config", "other"]},
                    "token_count": {"type": "integer", "description": "Approximate token count of the full file (helps consumers budget)"},
                    "project_id": {"type": "string", "description": "Optional; inferred from chat context"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sc_get_artifact",
            "description": (
                "Fetch the full record (path / title / summary / token_count "
                "/ creator) of an artifact by its ``art_*`` id. Use this when "
                "you got an artifact id from sc_query or a handoff and need "
                "to know what it points to before deciding whether to read "
                "the underlying file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string", "description": "Artifact id, e.g. art_a1b2c3d4"},
                },
                "required": ["artifact_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sc_record_decision",
            "description": (
                "Append a structured decision to the project's decision log. "
                "Use this for team-wide decisions that other agents need to "
                "respect (e.g. 'choose AWS over Azure', 'use REST not GraphQL', "
                "'PPT 用蓝色风格'). Once recorded, future tasks see it in their "
                "shared-context summary and via sc_query(table='decisions').\n"
                "DON'T use for: per-task internal choices, ephemeral preferences. "
                "DO supersede an old decision when overriding — pass supersedes_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "What was being decided"},
                    "decision": {"type": "string", "description": "The chosen answer"},
                    "rationale": {"type": "string", "description": "Why this choice (optional but recommended)"},
                    "supersedes_id": {"type": "string", "description": "If overriding a prior decision, pass its dec_* id"},
                    "project_id": {"type": "string", "description": "Optional; inferred from chat context"},
                },
                "required": ["topic", "decision"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sc_handoff",
            "description": (
                "PULL-MODEL handoff to another agent — writes a row to the "
                "handoffs table; the destination agent discovers it via "
                "sc_query(table='handoffs', dst_agent='self', status='pending'). "
                "Token cost ~0 vs. /handoff which copies content into dst's "
                "messages.\n"
                "Pass artifact_refs as a list of art_* ids the receiver will "
                "need (don't paste file content). Summary should be ≤300 "
                "chars — point, don't narrate.\n"
                "Returns the handoff id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "dst_agent": {"type": "string", "description": "Receiver agent id (or name — resolver tolerates both)"},
                    "intent": {"type": "string", "description": "What the receiver should do, ≤500 chars"},
                    "summary": {"type": "string", "description": "Optional 1-2 line context, ≤300 chars"},
                    "artifact_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of art_* ids the receiver will need. AVOID pasting file content here.",
                    },
                    "project_id": {"type": "string", "description": "Optional; inferred from chat context"},
                },
                "required": ["dst_agent", "intent"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_deliverable",
            "description": (
                "Register a concrete artifact as a project deliverable and mark it SUBMITTED (enters review queue).\n"
                "Use when: you produced a document / code file / design / analysis for the current project and it is ready for review. Works only inside a project chat context.\n"
                "Not for: intermediate drafts (wait until ready). Not for agents' internal tool outputs that the user does not need to see. Outside a project context this returns an error.\n"
                "Output: deliverable id + title + kind + project id + resolved file path. If content_text is given without file_path, content is auto-written to the project's shared workspace dir.\n"
                "GOTCHA: project auto-discovered from chat context — if you are not in a project chat, pass project_id. Files outside the shared dir are copied in automatically (so the deliverables UI can find them)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short title for the deliverable"},
                    "file_path": {"type": "string", "description": "Absolute or relative path to the artifact file"},
                    "content_text": {"type": "string", "description": "Inline content (for text-only deliverables)"},
                    "url": {"type": "string", "description": "External URL (for hosted artifacts)"},
                    "kind": {"type": "string", "description": "document | code | design | analysis | other (default: document)"},
                    "milestone_id": {"type": "string", "description": "Optional milestone id to link this deliverable to"},
                    "task_id": {"type": "string", "description": "Optional task id that produced this deliverable"},
                    "project_id": {"type": "string", "description": "Project id (optional; inferred from chat context)"},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_goal",
            "description": (
                "Create a measurable project goal (numeric count/percent or qualitative text).\n"
                "Use when: the user says 'add a goal', 'we want to hit X by Y', 'track progress on Z'.\n"
                "Not for: individual tasks (use task_update). Not for milestones (those bundle deliverables — use create_milestone). Only works inside a project context.\n"
                "Output: goal id + name + metric + target + project id.\n"
                "GOTCHA: metric='count' needs target_value (a number); metric='text' needs target_text. Mixing them silently ignores the unused field — double-check which one the user meant."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Goal name (short)"},
                    "description": {"type": "string", "description": "Longer description / rationale"},
                    "metric": {"type": "string", "description": "count | percent | text (default: count)"},
                    "target_value": {"type": "number", "description": "Numeric target for count/percent metrics"},
                    "target_text": {"type": "string", "description": "Qualitative target for text metrics"},
                    "owner_agent_id": {"type": "string", "description": "Optional owner agent id (default: calling agent)"},
                    "project_id": {"type": "string", "description": "Project id (optional; inferred from chat context)"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_goal_progress",
            "description": (
                "Update a goal's current value or mark it as done. Persists progress to the project.\n"
                "Use when: progress is made toward a goal you or teammates previously created — e.g. 'closed 3 more tickets', 'goal reached'.\n"
                "Not for: creating goals (use create_goal). Not for milestones (use update_milestone_status).\n"
                "Output: goal id + new current_value + done state (+ optional note).\n"
                "GOTCHA: current_value must be numeric — for text-metric goals only `done=true/false` and `note` matter. Unknown goal_id returns an error (goals are project-scoped)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string", "description": "The goal id to update"},
                    "current_value": {"type": "number", "description": "New current value (for count/percent metrics)"},
                    "done": {"type": "boolean", "description": "Mark as complete"},
                    "note": {"type": "string", "description": "Optional progress note"},
                    "project_id": {"type": "string", "description": "Project id (optional; inferred from chat context)"},
                },
                "required": ["goal_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_milestone",
            "description": (
                "Create a project milestone AND optionally delegate it to another agent.\n"
                "Use when: structuring a project into checkpoints; or assigning a chunk of work to a specific teammate.\n"
                "Not for: individual tasks (use task_update). Not for goals (use create_goal — milestones are checkpoints, goals are metrics).\n"
                "\n"
                "⭐ DELEGATION (the main reason to set responsible_agent_id):\n"
                "  - Pass another agent's id (NOT your own) → the system AUTO-FIRES a chat\n"
                "    message into the project group: '@<that agent> 你被指派负责里程碑「X」...',\n"
                "    AND that agent immediately starts working on it (no need for you to also\n"
                "    call send_message — that would be a duplicate).\n"
                "  - Get teammate ids from the team list at the top of your prompt:\n"
                "    each line shows  `<role>-<name> [id=<agent_id>]: <responsibility>` — copy the\n"
                "    id= value into responsible_agent_id.\n"
                "  - Omit it (or pass your own id) → the milestone is yours; nobody else triggered.\n"
                "\n"
                "Output: milestone id + name + responsible agent + due date + project id; if delegated, also `assigned to <name>`.\n"
                "GOTCHA: due_date accepts 'YYYY-MM-DD' or natural form — prefer ISO for unambiguous parsing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Milestone name"},
                    "responsible_agent_id": {
                        "type": "string",
                        "description": (
                            "Agent id of the responsible teammate. Pass ANOTHER agent's id "
                            "to delegate (auto-fires a chat message + triggers them to start "
                            "work). Default = caller's own id (self-assignment, no trigger). "
                            "Look up ids in the [项目群聊] team list."
                        ),
                    },
                    "description": {
                        "type": "string",
                        "description": (
                            "Optional one-paragraph context shown to the responsible agent in the "
                            "delegation message. Helps them understand scope. Skipped if omitted."
                        ),
                    },
                    "due_date": {"type": "string", "description": "Due date in YYYY-MM-DD or natural form"},
                    "project_id": {"type": "string", "description": "Project id (optional; inferred from chat context)"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_milestone_responsibility",
            "description": (
                "Reassign an existing milestone to a different agent AND auto-notify them.\n"
                "Use when: redistributing work after the initial create_milestone — e.g. user asks "
                "'把模块④ 从小刚移给小专,小专更熟悉行业需求'.\n"
                "Not for: creating new milestones (use create_milestone). Not for status updates "
                "(use update_milestone_status).\n"
                "\n"
                "⭐ Effect:\n"
                "  - Updates milestone.responsible_agent_id to the new owner.\n"
                "  - AUTO-FIRES a chat message to the new owner: '@<new owner> 你接手了里程碑「X」...',\n"
                "    AND triggers them to start working on it (same delegation path as create_milestone\n"
                "    with responsible_agent_id of another agent).\n"
                "  - Also posts a courtesy notice to the old owner so they know they no longer own it\n"
                "    (skip via notify_old=false if that adds noise).\n"
                "\n"
                "Get teammate ids from the [项目群聊] team list at the top of your prompt: each line shows\n"
                "`<role>-<name> [id=<agent_id>]: <responsibility>` — copy the id= value into "
                "new_responsible_agent_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "milestone_id": {"type": "string", "description": "The milestone id to reassign"},
                    "new_responsible_agent_id": {
                        "type": "string",
                        "description": (
                            "Agent id of the NEW responsible owner (look up in the team list)."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "description": (
                            "One-line reason for the reassignment (shown in both the new-owner trigger "
                            "message and the old-owner release notice). Helps the recipients understand "
                            "context. Optional but recommended."
                        ),
                    },
                    "notify_old": {
                        "type": "boolean",
                        "description": (
                            "Whether to send a courtesy notice to the previous responsible. "
                            "Default true. Set false when self-reassigning or when noise is unwanted."
                        ),
                    },
                    "project_id": {"type": "string", "description": "Project id (optional; inferred from chat context)"},
                },
                "required": ["milestone_id", "new_responsible_agent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_milestone_status",
            "description": (
                "Update a milestone's status or attach evidence of completion. Typical transitions: pending → in_progress → done.\n"
                "Use when: you or your team completed work toward a milestone and want to record progress / evidence.\n"
                "Not for: creating milestones (use create_milestone). Not for reassigning ownership (use update_milestone_responsibility). Not for admin confirm/reject — that is a separate endpoint.\n"
                "Output: milestone id + new status + optional evidence length.\n"
                "GOTCHA: attach `evidence` when flipping to done — the admin reviewer uses it to verify. Empty status + empty evidence returns an error ('provide at least one')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "milestone_id": {"type": "string", "description": "The milestone id"},
                    "status": {"type": "string", "description": "pending | in_progress | done"},
                    "evidence": {"type": "string", "description": "Evidence text (e.g. links, summary of what was completed)"},
                    "project_id": {"type": "string", "description": "Project id (optional; inferred from chat context)"},
                },
                "required": ["milestone_id"],
            },
        },
    },
    # ---- UI block tools (rich interactive messages) ----
    {
        "type": "function",
        "function": {
            "name": "emit_ui_block",
            "description": (
                "Render an interactive UI block (choice buttons or checklist) inline in chat. "
                "Max 8 choices / 20 checklist items; unique item IDs. "
                "For free-form Q use prose; for execution steps use plan_update."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["choice", "checklist"],
                        "description": "Block type: 'choice' = clickable buttons, 'checklist' = display-only list.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "The question or header text shown at the top of the block (max 400 chars).",
                    },
                    "options": {
                        "type": "array",
                        "description": "For kind='choice'. Each item: a string label OR {id, label}.",
                        "items": {
                            "oneOf": [
                                {"type": "string"},
                                {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "label": {"type": "string"},
                                    },
                                    "required": ["label"],
                                },
                            ]
                        },
                    },
                    "items": {
                        "type": "array",
                        "description": "For kind='checklist'. Each item: a string text OR {id, text, done}.",
                        "items": {
                            "oneOf": [
                                {"type": "string"},
                                {
                                    "type": "object",
                                    "properties": {
                                        "id": {"type": "string"},
                                        "text": {"type": "string"},
                                        "done": {"type": "boolean"},
                                    },
                                    "required": ["text"],
                                },
                            ]
                        },
                    },
                },
                "required": ["kind", "prompt"],
            },
        },
    },
    # ---- Handoff payload (structured baton-pass between agents) ----
    {
        "type": "function",
        "function": {
            "name": "emit_handoff",
            "description": (
                "Structured baton-pass to the next agent (summary + deliverable + followups). "
                "AT MOST ONE per task completion. "
                "For status updates use send_message; for blocking handoffs use handoff_request."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "One-paragraph what-I-did. Max 500 chars.",
                    },
                    "deliverable_path": {
                        "type": "string",
                        "description": (
                            "Relative path of the artifact in the shared workspace, if any "
                            "(e.g. 'report.pptx', 'analysis/findings.md'). Empty if no file deliverable."
                        ),
                    },
                    "highlights": {
                        "type": "array",
                        "description": "Key findings / decisions / data points (up to 6). String or {text}.",
                        "items": {
                            "oneOf": [
                                {"type": "string"},
                                {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}},
                                    "required": ["text"],
                                },
                            ]
                        },
                    },
                    "followups": {
                        "type": "array",
                        "description": (
                            "Suggested next steps for other agents (up to 8). Each: "
                            "{for: target_agent_name_or_role, task: concrete_action}."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "for": {"type": "string"},
                                "task": {"type": "string"},
                            },
                            "required": ["for", "task"],
                        },
                    },
                },
                "required": ["summary"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

# Filesystem tools (read_file / write_file / edit_file / search_files /
# glob_files) moved to app/tools_split/fs.py. Schemas still live in
# TOOL_DEFINITIONS above; handlers re-exported here so the dispatcher
# and any external importers of `tools._tool_*` keep working.
from .tools_split.fs import (  # noqa: E402,F401
    _tool_read_file,
    _tool_write_file,
    _tool_edit_file,
    _tool_search_files,
    _tool_glob_files,
)


# System / exec tools (bash / pip_install / desktop_screenshot) moved
# to app/tools_split/system.py. bash lives in this block because it
# shares the sandbox policy; the other two joined the cluster for
# coherence. Schemas still in TOOL_DEFINITIONS above.
from .tools_split.system import (  # noqa: E402,F401
    _tool_bash,
    _tool_pip_install,
    _tool_desktop_screenshot,
)

# Test runner — powers Block 2 Review loop; also LLM-callable on its own.
from .tools_split.test_runner import _tool_run_tests  # noqa: E402,F401


# ---------------------------------------------------------------------------
# Coordination tools — TeamCreate / SendMessage / TaskUpdate
# ---------------------------------------------------------------------------
# Handlers + _parse_run_at helper moved to app/tools_split/coordination.py.
from .tools_split.coordination import (  # noqa: E402,F401
    _tool_team_create,
    _tool_send_message,
    _tool_task_update,
    _tool_check_inbox,
    _tool_ack_message,
    _tool_reply_message,
)

# _get_hub re-exported for backwards compat with any external importer.
from .tools_split._common import _get_hub  # noqa: E402,F401


# Project-management tools (submit_deliverable, create_goal,
# update_goal_progress, create_milestone, update_milestone_status) +
# scope helpers moved to app/tools_split/project.py.
from .tools_split.project import (  # noqa: E402,F401
    _get_current_scope,
    _resolve_project,
    _save_projects_silently,
    _tool_submit_deliverable,
    _tool_create_goal,
    _tool_update_goal_progress,
    _tool_create_milestone,
    _tool_update_milestone_responsibility,
    _tool_update_milestone_status,
    _tool_propose_decomposition,
)

# MCP call + builtin audio TTS/STT handler moved to
# app/tools_split/mcp.py. That module registers the builtin handler
# with the dispatcher at import time — keep this import unconditional
# so the registration side effect always runs.
from .tools_split.mcp import (  # noqa: E402,F401
    _tool_mcp_call,
    _handle_builtin_mcp,
    _push_audio_event,
    get_audio_events,
)

# Data-processing tools — datetime / json / text transforms.
from .tools_split.data import (  # noqa: E402,F401
    _tool_datetime_calc,
    _tool_json_process,
    _tool_text_process,
)

# Knowledge + experience library tools.
from .tools_split.knowledge import (  # noqa: E402,F401
    _tool_save_experience,
    _tool_knowledge_lookup,
    _tool_share_knowledge,
    _tool_learn_from_peers,
    _tool_memory_recall,
    _tool_wiki_ingest,
)

# Media tools — pptx and video creation.
from .tools_split.media import (  # noqa: E402,F401
    _tool_create_pptx,
    _tool_create_pptx_advanced,
    _tool_create_video,
)

# Skill-package tools — guide loader / proposer / submitter.
from .tools_split.skills import (  # noqa: E402,F401
    _tool_get_skill_guide,
    _tool_propose_skill,
    _tool_submit_skill,
)

# Shared-context tools — agents query/write the project shared context
# database (artifacts, decisions, milestones, handoffs, pending Q&A)
# instead of pushing content through messages. Token-efficient by design.
from .tools_split.shared_context import (  # noqa: E402,F401
    _tool_sc_query,
    _tool_sc_register_artifact,
    _tool_sc_get_artifact,
    _tool_sc_record_decision,
    _tool_sc_handoff,
)

# UI-block tools — interactive choice + display-only checklist.
from .tools_split.ui import (  # noqa: E402,F401
    _tool_emit_ui_block,
    _tool_emit_handoff,
    build_ui_block,
    build_handoff_payload,
)

# Web / network tools (already extracted in an earlier commit; import
# here so the dispatcher below can reference the handlers by name).
from .tools_split.web import (  # noqa: E402,F401
    _tool_web_search,
    _tool_web_fetch,
    _tool_web_screenshot,
    _tool_http_request,
)


# ---------------------------------------------------------------------------
# Tool dispatcher
# ---------------------------------------------------------------------------

_TOOL_FUNCS: dict[str, callable] = {
    "read_file": _tool_read_file,
    "write_file": _tool_write_file,
    "edit_file": _tool_edit_file,
    "bash": _tool_bash,
    "run_tests": _tool_run_tests,
    "search_files": _tool_search_files,
    "glob_files": _tool_glob_files,
    "web_search": _tool_web_search,
    "web_fetch": _tool_web_fetch,
    # New daily-work tools
    "web_screenshot": _tool_web_screenshot,
    "http_request": _tool_http_request,
    "datetime_calc": _tool_datetime_calc,
    "json_process": _tool_json_process,
    "text_process": _tool_text_process,
    # Coordination tools
    "team_create": _tool_team_create,
    "send_message": _tool_send_message,
    "task_update": _tool_task_update,
    "check_inbox": _tool_check_inbox,
    "ack_message": _tool_ack_message,
    "reply_message": _tool_reply_message,
    # Project-scope tools (auto-discover project from thread-local context)
    "submit_deliverable": _tool_submit_deliverable,
    "create_goal": _tool_create_goal,
    "update_goal_progress": _tool_update_goal_progress,
    "create_milestone": _tool_create_milestone,
    "update_milestone_responsibility": _tool_update_milestone_responsibility,
    "update_milestone_status": _tool_update_milestone_status,
    # Long-task subsystem (app/long_task) — propose decomposition draft
    # for user confirmation; does NOT immediately create sub-tasks.
    "propose_decomposition": _tool_propose_decomposition,
    "mcp_call": _tool_mcp_call,
    # Experience persistence + skill generation
    "save_experience": _tool_save_experience,
    "propose_skill": _tool_propose_skill,
    "submit_skill": _tool_submit_skill,
    # Knowledge management tools
    "knowledge_lookup": _tool_knowledge_lookup,
    "memory_recall": _tool_memory_recall,
    "share_knowledge": _tool_share_knowledge,
    "learn_from_peers": _tool_learn_from_peers,
    "wiki_ingest": _tool_wiki_ingest,
    # UI-block tools (choice buttons / checklist). Handler validates;
    # agent_execution.py emits the ui_block event after.
    "emit_ui_block": _tool_emit_ui_block,
    # Structured baton-pass between agents. Handler validates; agent_execution.py
    # emits the typed 'handoff' event so the UI can render a distinct card and
    # the next agent's system prompt can ingest it.
    "emit_handoff": _tool_emit_handoff,
    # Human-in-the-loop tools (handled specially by agent, not dispatched here)
    "request_web_login": lambda **kw: "ERROR: request_web_login must be handled by agent directly",
    # Inter-agent handoff with 3-state handshake (handled specially by agent)
    "handoff_request": lambda **kw: "ERROR: handoff_request must be handled by agent directly",
    # System and productivity tools
    "pip_install": _tool_pip_install,
    "create_pptx": _tool_create_pptx,
    "create_pptx_advanced": _tool_create_pptx_advanced,
    "desktop_screenshot": _tool_desktop_screenshot,
    "create_video": _tool_create_video,
    "get_skill_guide": _tool_get_skill_guide,
    # Shared-context (project-scoped DB for multi-agent collaboration)
    "sc_query": _tool_sc_query,
    "sc_register_artifact": _tool_sc_register_artifact,
    "sc_get_artifact": _tool_sc_get_artifact,
    "sc_record_decision": _tool_sc_record_decision,
    "sc_handoff": _tool_sc_handoff,
}


# Tool name aliases (LLMs sometimes call with different names)
_TOOL_ALIASES: dict[str, str] = {
    "exec": "bash",
    "execute": "bash",
    "shell": "bash",
    "run_command": "bash",
    "cmd": "bash",
    "run_bash": "bash",
    "read": "read_file",
    "write": "write_file",
    "edit": "edit_file",
    "search": "search_files",
    "grep": "search_files",
    "glob": "glob_files",
    "find": "glob_files",
    "fetch": "web_fetch",
    "fetch_url": "web_fetch",
    "screenshot": "web_screenshot",
    "capture": "web_screenshot",
    "http": "http_request",
    "request": "http_request",
    "api_call": "http_request",
    "curl": "http_request",
    "datetime": "datetime_calc",
    "date": "datetime_calc",
    "time": "datetime_calc",
    "json": "json_process",
    "parse_json": "json_process",
    "text": "text_process",
    "string": "text_process",
    "knowledge": "knowledge_lookup",
    "look_up_knowledge": "knowledge_lookup",
    "search_knowledge": "knowledge_lookup",
    "share": "share_knowledge",
    "publish_knowledge": "share_knowledge",
    "learn_peers": "learn_from_peers",
    "cross_role_learn": "learn_from_peers",
    "pip": "pip_install",
    "install": "pip_install",
    "pptx": "create_pptx",
    "pptx_advanced": "create_pptx_advanced",
    "advanced_pptx": "create_pptx_advanced",
    "powerpoint": "create_pptx",
    "presentation": "create_pptx",
    "screenshot": "desktop_screenshot",
    "snap": "desktop_screenshot",
    "screen_capture": "desktop_screenshot",
    "video": "create_video",
    "make_video": "create_video",
    "stitch_frames": "create_video",
    "skill_guide": "get_skill_guide",
    "load_skill": "get_skill_guide",
    "read_skill": "get_skill_guide",
    "generate_skill": "propose_skill",
    "create_skill": "propose_skill",
    "forge_skill": "propose_skill",
    "submit_skill_package": "submit_skill",
    "publish_skill": "submit_skill",
}


# ---------------------------------------------------------------------------
# ToolRegistry initialization
# ---------------------------------------------------------------------------

def _init_registry() -> ToolRegistry:
    """
    Initialize the module-level tool registry from existing TOOL_DEFINITIONS
    and _TOOL_FUNCS. This is called once to populate the singleton.
    """
    registry = ToolRegistry()

    # Map tool names to their toolset categories
    toolset_map = {
        # Core file operations
        "read_file": "core",
        "write_file": "core",
        "edit_file": "core",
        "bash": "core",
        "search_files": "core",
        "glob_files": "core",

        # Web tools
        "web_search": "web",
        "web_fetch": "web",
        "web_screenshot": "web",
        "http_request": "web",

        # Data processing
        "json_process": "data",
        "text_process": "data",
        "datetime_calc": "data",

        # Coordination / messaging
        "team_create": "coordination",
        "send_message": "coordination",
        "task_update": "coordination",
        "check_inbox": "coordination",
        "ack_message": "coordination",
        "reply_message": "coordination",
        "mcp_call": "coordination",

        # Skill management
        "save_experience": "coordination",
        "propose_skill": "skill",
        "submit_skill": "skill",

        # Knowledge management
        "knowledge_lookup": "coordination",
        "memory_recall": "coordination",
        "share_knowledge": "coordination",
        "learn_from_peers": "coordination",

        # Human-in-the-loop
        "request_web_login": "coordination",
        # Inter-agent handoff
        "handoff_request": "coordination",
        # System and productivity tools
        "pip_install": "system",
        "create_pptx": "productivity",
        "create_pptx_advanced": "productivity",
        "desktop_screenshot": "system",
        "create_video": "productivity",
        "get_skill_guide": "skill",
    }

    # Find tool schema definitions by name
    schema_map = {}
    for tool_def in TOOL_DEFINITIONS:
        if tool_def.get("type") == "function":
            tool_name = tool_def["function"].get("name")
            if tool_name:
                schema_map[tool_name] = tool_def["function"]

    # Register each tool from _TOOL_FUNCS
    for tool_name, handler in _TOOL_FUNCS.items():
        toolset = toolset_map.get(tool_name, "other")
        schema = schema_map.get(tool_name, {})
        description = schema.get("description", "")

        # Determine risk level
        if tool_name in ("bash", "write_file", "edit_file"):
            risk = "dangerous"
        elif tool_name in ("web_fetch", "web_search", "http_request", "pip_install"):
            risk = "moderate"
        else:
            risk = "safe"

        registry.register(
            name=tool_name,
            toolset=toolset,
            schema=schema,
            handler=handler,
            description=description,
            risk_level=risk,
        )

    # Register aliases
    for alias, canonical in _TOOL_ALIASES.items():
        try:
            registry.add_alias(alias, canonical)
        except ValueError:
            logger.warning(f"Failed to register alias '{alias}' → '{canonical}'")

    return registry


# Module-level singleton instance
tool_registry = _init_registry()


def execute_tool(name: str, arguments: dict) -> str:
    """
    Execute a tool by name with the given arguments.
    Delegates to tool_registry.dispatch() but maintains backward compatibility
    with existing code that calls execute_tool() directly.
    Returns the result string.
    """
    return tool_registry.dispatch(name, arguments)


def get_tool_definitions() -> list[dict]:
    """
    Return tool definitions in function-calling JSON schema format.
    Delegates to tool_registry.get_definitions() for available tools.
    """
    return tool_registry.get_definitions()
