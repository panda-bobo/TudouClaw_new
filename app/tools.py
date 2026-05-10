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
            "description": "Read UTF-8 text from a file. Optional line range. Not for binary or content search.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or relative file path."},
                    "offset": {
                        "type": "integer",
                        "description": "Start line (0-based). Default 0.",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max lines to read. Default: all.",
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
            "description": "Create or overwrite a file with UTF-8 content. Path must be inside workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Required. Relative or abs path inside workspace; sandbox rejects outside paths.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Required. Full UTF-8 content. For >20KB use edit_file to avoid truncation.",
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
            "description": "Replace exact substring in a file. old_string must appear exactly once.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to edit."},
                    "old_string": {"type": "string", "description": "Exact string to find. Must match byte-for-byte and be unique."},
                    "new_string": {"type": "string", "description": "Replacement string."},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command. Foreground or background. Use background for dev servers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command. cd is per-call; chain with &&. Each call is a fresh shell."},
                    "timeout": {
                        "type": "integer",
                        "description": "Foreground timeout seconds (default 30, max 600). Ignored if background.",
                    },
                    "run_in_background": {
                        "type": "boolean",
                        "description": "Run async, return process_id. Use for dev servers/watchers. Pull output via bash_logs.",
                    },
                    "background_log_lines": {
                        "type": "integer",
                        "description": "Initial log lines to return when background (default 30, max 500).",
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_logs",
            "description": "Pull recent log lines from a background bash process. Returns last N lines tail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "process_id": {"type": "integer", "description": "Pid returned by bash(run_in_background=true)."},
                    "lines": {"type": "integer", "description": "Log lines to return (default 30, max 500)."},
                },
                "required": ["process_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bash_kill",
            "description": "Terminate a background bash process. SIGTERM then SIGKILL after 2s. Idempotent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "process_id": {"type": "integer", "description": "Pid returned by bash(run_in_background=true)."},
                },
                "required": ["process_id"],
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
            "description": "Run test suite, return structured pass/fail counts. Auto-detects pytest/npm/go/cargo.",
            "parameters": {
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "string",
                        "description": "Space-separated test paths/patterns. Empty = all tests in cwd.",
                    },
                    "framework": {
                        "type": "string",
                        "description": "Force: pytest | npm | go | cargo. Empty = auto-detect.",
                    },
                    "extra_args": {
                        "type": "string",
                        "description": "CLI args appended verbatim (e.g. '-k test_foo').",
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
            "description": "Regex-search file contents recursively (grep -rn). Returns path:line: match.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Python regex; escape special chars."},
                    "path": {
                        "type": "string",
                        "description": "Directory or file (default: cwd).",
                    },
                    "include": {
                        "type": "string",
                        "description": "Glob filter, e.g. '*.py'.",
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
            "description": "Find files by name/path glob pattern. Sorted list, max 500.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Glob, e.g. '**/*.py'. ** must be full path segment.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Base directory for search (default: cwd).",
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
            "description": "Search the public web via DuckDuckGo. Returns title/URL/snippet results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                    "max_results": {
                        "type": "integer",
                        "description": "Max results (default 8).",
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
            "description": "Fetch a URL and extract plain text. Strips HTML. Not for JSON APIs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch."},
                    "max_length": {
                        "type": "integer",
                        "description": "Max chars to return (default 5000).",
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
            "description": "Invoke a tool on an external MCP (email/Slack/GitHub/etc). Not for in-system agents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "mcp_id": {
                        "type": "string",
                        "description": "Bound MCP id/name (e.g. 'email', 'slack', 'github').",
                    },
                    "tool": {
                        "type": "string",
                        "description": "MCP tool name to invoke (e.g. 'send_email').",
                    },
                    "arguments": {
                        "oneOf": [{"type": "object"}, {"type": "string"}],
                        "description": "Args object for the tool. JSON string also accepted.",
                    },
                    "list_mcps": {
                        "type": "boolean",
                        "description": "If true, list bound MCPs instead of calling.",
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
            "description": "Spawn a transient sub-agent to run an independent task in parallel.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name for the sub-agent."},
                    "role": {
                        "type": "string",
                        "description": "Role: coder | reviewer | researcher | tester | devops | writer.",
                    },
                    "task": {"type": "string", "description": "Task for the sub-agent to execute."},
                    "working_dir": {
                        "type": "string",
                        "description": "Working dir (default: cwd).",
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
            "description": "Async message to another in-system agent's inbox. Not for external email or chat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to_agent": {
                        "type": "string",
                        "description": "Agent ID or name.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "1-3 sentence conclusion. Main thing the recipient reads.",
                    },
                    "key_fields": {
                        "type": "object",
                        "description": "Small structured payload (numbers, decisions, URLs, status).",
                    },
                    "artifact_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Paths/artifact IDs. Prefer over inline long body.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Optional legacy raw body. Use only if summary is insufficient.",
                    },
                    "msg_type": {
                        "type": "string",
                        "description": "task | info | result | question (default task).",
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
            "description": "Blocking task transfer to another in-system agent. Caller waits for result.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to_agent": {
                        "type": "string",
                        "description": "Target agent ID or name.",
                    },
                    "task": {
                        "type": "string",
                        "description": "What the receiver should do. Concrete and self-contained.",
                    },
                    "expected_output": {
                        "type": "string",
                        "description": "Format/acceptance criteria for the return. Strongly recommended.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional extra background (paths, links, prior findings).",
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Max wait before timeout (default 600).",
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
            "description": "Create/update/complete/list project tasks; supports recurring or delayed scheduling.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "create | update | complete | list.",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Task ID (required for update/complete).",
                    },
                    "title": {"type": "string", "description": "Task title (for create)."},
                    "description": {"type": "string", "description": "Task description."},
                    "status": {
                        "type": "string",
                        "description": "todo | in_progress | done | blocked.",
                    },
                    "result": {"type": "string", "description": "Result summary (for complete)."},
                    "recurrence": {
                        "type": "string",
                        "description": "once (default) | daily | weekly | monthly | cron.",
                    },
                    "recurrence_spec": {
                        "type": "string",
                        "description": "daily='HH:MM'; weekly='DOW HH:MM'; monthly='D HH:MM'; cron='m h dom mon dow'.",
                    },
                    "run_at": {
                        "type": "string",
                        "description": "Delayed one-time exec: '+Nm', '+Nh', or 'HH:MM' (today).",
                    },
                },
                "required": ["action"],
            },
        },
    },
    # Day 5 (2026-05-05) — Structured handoff tools
    {
        "type": "function",
        "function": {
            "name": "dispatch_task",
            "description": "PM-side: assign a structured task (brief + deliverables) to another agent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "to_agent": {"type": "string",
                                 "description": "Recipient agent_id."},
                    "brief": {"type": "string",
                              "description": "1-3 sentences, max 500 chars. What and why; no how."},
                    "context_refs": {
                        "type": "array",
                        "description": "Files to read: [{path, why_relevant, expected_section}].",
                        "items": {"type": "object"},
                    },
                    "deliverables": {
                        "type": "array",
                        "description": "Required, min 1: [{path, kind, must_contain, min_lines, max_lines, acceptance_cmd}].",
                        "items": {"type": "object"},
                    },
                    "project_id": {"type": "string",
                                   "description": "Project (auto from scope if omitted)."},
                    "project_task_id": {"type": "string",
                                        "description": "Optional ProjectTask.id link."},
                    "priority": {"type": "integer",
                                 "description": "0 normal, 1 high, 2 urgent."},
                    "deadline": {"type": "string",
                                 "description": "Optional ISO timestamp or epoch seconds."},
                },
                "required": ["to_agent", "brief", "deliverables"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "accept_task",
            "description": "Receiver: pop a task assignment from inbox. Returns highest-priority by default.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ta_id": {"type": "string",
                              "description": "Optional assignment id; defaults to highest-priority pending."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "inbox_assignments",
            "description": "List structured task assignments in your inbox. Distinct from check_inbox (chat).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    # Phase 3 P3-4 (2026-05-06) — Close the handoff loop
    {
        "type": "function",
        "function": {
            "name": "report_back",
            "description": "Receiver: report task completion or blocker to PM. Closes the dispatch loop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ta_id": {"type": "string",
                              "description": "Optional; defaults to most recently accepted."},
                    "status": {"type": "string",
                               "description": "done | blocked | needs_clarification | cancelled."},
                    "summary": {"type": "string",
                                "description": "1-3 sentences: what you did or what blocked you."},
                    "actual_deliverables": {"type": "array",
                                            "description": "Paths actually produced (workspace-relative).",
                                            "items": {"type": "string"}},
                    "blocker": {"type": "string",
                                "description": "If blocked, what's blocking you."},
                },
                "required": ["status"],
            },
        },
    },
    # Phase 2 P2-6 (2026-05-06) — Team status query
    {
        "type": "function",
        "function": {
            "name": "query_team_status",
            "description": "List current activity of every agent in a project. PM use before dispatching.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string",
                                   "description": "Project ID (auto from scope if omitted)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_agent_status",
            "description": "Get one agent's current task and last reported status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string",
                                 "description": "Target agent_id."},
                    "project_id": {"type": "string",
                                   "description": "Optional, restrict to one project."},
                },
                "required": ["agent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_inbox",
            "description": "Read your in-system inbox (messages from other agents). Not external email.",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Max messages (default 20, max 100).",
                    },
                    "include_read": {
                        "type": "boolean",
                        "description": "Also include read-but-not-acked messages (default false).",
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
            "description": "Mark inbox messages as acknowledged. Stops them re-surfacing each turn.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_ids": {
                        "type": "string",
                        "description": "One id, or multiple comma/whitespace-separated (e.g. 'msg_abc, msg_def').",
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
            "description": "Reply to an inbox message (preserves thread_id). Use summary envelope.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message_id": {
                        "type": "string",
                        "description": "Id of the message being replied to.",
                    },
                    "summary": {
                        "type": "string",
                        "description": "1-3 sentence answer. Main thing the recipient reads.",
                    },
                    "key_fields": {
                        "type": "object",
                        "description": "Small structured result (numbers, decisions, paths).",
                    },
                    "artifact_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Paths to large outputs. Recipient reads with read_file.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Legacy raw body. Required only if summary not provided.",
                    },
                    "priority": {
                        "type": "string",
                        "description": "urgent | normal | low (default normal).",
                    },
                    "ttl_s": {
                        "type": "integer",
                        "description": "Optional seconds-to-live; 0 (default) = never expire.",
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
            "description": "Live execution checklist for 3+ step tasks. Each step needs concrete acceptance.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "create_plan | start_step | complete_step | add_step | fail_step | replan.",
                    },
                    "task_summary": {
                        "type": "string",
                        "description": "Brief task summary (for create_plan).",
                    },
                    "steps": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string",
                                           "description": "Short step name."},
                                "detail": {"type": "string",
                                            "description": "Optional longer description."},
                                "acceptance": {"type": "string",
                                                "description": "Required. Concrete artifact/state proving done. Vague text rejected."},
                                "depends_on": {"type": "array", "items": {"type": "string"},
                                                "description": "Step IDs this depends on."},
                                "llm_purpose": {
                                    "type": "string",
                                    "enum": ["tool-heavy", "multimodal",
                                             "reasoning", "analysis",
                                             "coding", "default"],
                                    "description": "Step category for LLM auto-routing. Strongly recommended.",
                                },
                                "llm_rationale": {
                                    "type": "string",
                                    "description": "Optional one-line justification for llm_purpose.",
                                },
                            },
                        },
                        "description": "Step objects for create_plan. Each needs title + acceptance.",
                    },
                    "step_id": {
                        "type": "string",
                        "description": "Step ID (for start_step/complete_step/fail_step).",
                    },
                    "title": {
                        "type": "string",
                        "description": "Step title (for add_step).",
                    },
                    "detail": {
                        "type": "string",
                        "description": "Step detail (for add_step).",
                    },
                    "acceptance": {
                        "type": "string",
                        "description": "Acceptance criterion (for add_step).",
                    },
                    "result_summary": {
                        "type": "string",
                        "description": "Required for complete/fail. Specific paths/counts/ids verifying acceptance.",
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
            "description": "Capture a PNG screenshot of a web page via Playwright or CLI fallback.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to screenshot."},
                    "output_path": {
                        "type": "string",
                        "description": "Save path (default: auto-generated in workspace).",
                    },
                    "full_page": {
                        "type": "boolean",
                        "description": "Capture the full scrollable page (default false).",
                    },
                    "width": {
                        "type": "integer",
                        "description": "Viewport width in px (default 1280).",
                    },
                    "height": {
                        "type": "integer",
                        "description": "Viewport height in px (default 720).",
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
            "description": "Make HTTP request (GET/POST/PUT/DELETE/PATCH) with headers, body, timeout.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to request."},
                    "method": {
                        "type": "string",
                        "description": "GET | POST | PUT | DELETE | PATCH (default GET).",
                    },
                    "headers": {
                        "type": "object",
                        "description": "Headers as key-value pairs.",
                    },
                    "body": {
                        "type": "string",
                        "description": "Body string. Set Content-Type yourself.",
                    },
                    "json_body": {
                        "type": "object",
                        "description": "Body as JSON object (auto-sets Content-Type).",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout seconds (default 30, max 120).",
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
            "description": "Date/time ops: now, diff, add duration, format, timezone convert.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "now | diff | add | format | convert.",
                    },
                    "date": {
                        "type": "string",
                        "description": "Date string, ISO preferred (e.g. '2024-03-15T10:30:00').",
                    },
                    "date2": {
                        "type": "string",
                        "description": "Second date (for diff).",
                    },
                    "days": {"type": "integer", "description": "Days to add (for add)."},
                    "hours": {"type": "integer", "description": "Hours to add (for add)."},
                    "minutes": {"type": "integer", "description": "Minutes to add (for add)."},
                    "timezone": {
                        "type": "string",
                        "description": "Timezone name (e.g. 'Asia/Shanghai', 'UTC').",
                    },
                    "format": {
                        "type": "string",
                        "description": "strftime format (e.g. '%%Y-%%m-%%d %%H:%%M').",
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
            "description": "Parse/extract/transform/validate JSON. Reads from string or file path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "parse | extract | keys | flatten | to_csv | from_csv | merge | count.",
                    },
                    "data": {
                        "type": "string",
                        "description": "JSON string or file path to process.",
                    },
                    "path": {
                        "type": "string",
                        "description": "Simplified path for extract (e.g. 'users[0].name'). No JSONPath filters.",
                    },
                    "data2": {
                        "type": "string",
                        "description": "Second JSON string (for merge).",
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
            "description": "Text transforms: count, regex replace/extract, sort, dedup, encode, hash, head/tail.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "count|replace|extract|sort|dedup|base64_*|url_*|hash|head|tail|split.",
                    },
                    "text": {"type": "string", "description": "Input text."},
                    "pattern": {
                        "type": "string",
                        "description": "Python regex (for replace/extract).",
                    },
                    "replacement": {
                        "type": "string",
                        "description": "Replacement string. Backrefs are \\1.",
                    },
                    "n": {
                        "type": "integer",
                        "description": "Number of lines (head/tail, default 10).",
                    },
                    "algorithm": {
                        "type": "string",
                        "description": "md5 | sha256 | sha1 (default sha256).",
                    },
                    "delimiter": {
                        "type": "string",
                        "description": "Split delimiter (default newline).",
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
            "description": "Primary tool to save reusable knowledge/methodology to wiki layer (auto-indexed).",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["experience", "methodology",
                                 "template", "pattern", "reference"],
                        "description": "experience | methodology | template | pattern | reference.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Human-readable title (used for slug + index).",
                    },
                    "body": {
                        "type": "string",
                        "description": "Full markdown body. Self-contained.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags for search.",
                    },
                    "scope": {
                        "type": "string",
                        "description": "Empty = role-scoped; 'global' = shared across roles.",
                    },
                    "sources": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional source paths/URLs that informed this page.",
                    },
                    "related": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional related page slugs.",
                    },
                    "domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional 1-3 domain tags (e.g. security, frontend, devops).",
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
            "description": "Deprecated. Use wiki_ingest(kind='experience') instead. Legacy JSON store.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scene": {
                        "type": "string",
                        "description": "Trigger scenario / when this experience applies.",
                    },
                    "core_knowledge": {
                        "type": "string",
                        "description": "Core insight / knowledge point.",
                    },
                    "action_rules": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "1-3 positive action rules.",
                    },
                    "taboo_rules": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "1-2 taboo rules.",
                    },
                    "priority": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                        "description": "Importance (default medium).",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional classification tags.",
                    },
                    "exp_type": {
                        "type": "string",
                        "enum": ["retrospective", "active_learning"],
                        "description": "retrospective | active_learning.",
                    },
                    "source": {
                        "type": "string",
                        "description": "Human-readable origin string.",
                    },
                    "role": {
                        "type": "string",
                        "description": "Override role bucket; defaults to caller's role.",
                    },
                    "evidence": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Citations: 'path:LINE', 'doc.md#section', or URL.",
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
            "description": "Search KB (shared + expert pool). Same-mode ONE-SHOT per turn.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keyword. Required for search; optional for count/list.",
                    },
                    "entry_id": {
                        "type": "string",
                        "description": "Specific entry ID from a prior search result.",
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["search", "count", "list", "outline"],
                        "description": "search=top-k chunks; count=chunks by file; list=metadata; outline=heading_paths.",
                    },
                    "source_file": {
                        "type": "string",
                        "description": "Outline only. Substring filter on source_file path.",
                    },
                    "heading_pattern": {
                        "type": "string",
                        "description": "Outline only. Regex on heading_path.",
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
            "description": "Query your agent-private long-term memory. ONE-SHOT per turn.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Topic / keywords / question to recall.",
                    },
                    "category": {
                        "type": "string",
                        "description": "Optional: intent | reasoning | outcome | rule | reflection.",
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
            "description": "Write entry to shared KB so all agents can access via knowledge_lookup.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Concise title; this is the primary search key.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Detailed content with steps/tips/examples and trigger keywords.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Categorization tags, e.g. ['pptx', 'design'].",
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
            "description": "Import high-quality experiences from another role's library into yours.",
            "parameters": {
                "type": "object",
                "properties": {
                    "source_role": {
                        "type": "string",
                        "description": "Role to learn from, e.g. 'designer', 'coder'.",
                    },
                    "topic": {
                        "type": "string",
                        "description": "Topic keyword filter (not semantic).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max experiences to import (default 5).",
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
            "description": "Show user an interactive login card to authenticate into a website.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "URL that requires login.",
                    },
                    "site_name": {
                        "type": "string",
                        "description": "Human-readable site name, e.g. 'GitHub'.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Why access is needed; task requiring this login.",
                    },
                    "login_url": {
                        "type": "string",
                        "description": "Optional specific login page URL if different from target.",
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
            "description": "Install/upgrade Python packages via pip. Affects shared system Python.",
            "parameters": {
                "type": "object",
                "properties": {
                    "packages": {
                        "type": "string",
                        "description": "Space-separated package names (e.g. 'requests numpy').",
                    },
                    "upgrade": {
                        "type": "boolean",
                        "description": "Upgrade to latest (default false).",
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
            "description": "Create a simple .pptx with title/content slides. For rich layouts use create_pptx_advanced.",
            "parameters": {
                "type": "object",
                "properties": {
                    "output_path": {
                        "type": "string",
                        "description": "Path where .pptx will be saved.",
                    },
                    "title": {
                        "type": "string",
                        "description": "Optional deck title.",
                    },
                    "slides": {
                        "type": "array",
                        "description": "Slide objects with title, content, optional layout and images.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {
                                    "type": "string",
                                    "description": "Slide title.",
                                },
                                "content": {
                                    "type": "string",
                                    "description": "Slide content; newlines split into bullets.",
                                },
                                "layout": {
                                    "type": "string",
                                    "description": "title | content | title_content | blank (default title_content).",
                                },
                                "images": {
                                    "type": "array",
                                    "description": "Optional images on the slide.",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "path": {"type": "string", "description": "Image file path."},
                                            "left": {"type": "number", "description": "Left position in inches (default 1)."},
                                            "top": {"type": "number", "description": "Top position in inches (default 2)."},
                                            "width": {"type": "number", "description": "Width in inches (0=auto)."},
                                            "height": {"type": "number", "description": "Height in inches (0=auto)."},
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
            "description": "Create design-rich PPTX with charts/tables/layouts. Use 'cards' for normal content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "output_path": {
                        "type": "string",
                        "description": "Output .pptx file path.",
                    },
                    "theme": {
                        "type": "object",
                        "description": "Global color theme.",
                        "properties": {
                            "primary": {"type": "string", "description": "Primary color hex (e.g. 'E8590C')."},
                            "secondary": {"type": "string", "description": "Secondary color hex."},
                            "accent": {"type": "string", "description": "Accent color hex."},
                            "background": {"type": "string", "description": "Default background hex."},
                            "title_font": {"type": "string", "description": "Title font (e.g. 'Microsoft YaHei')."},
                            "body_font": {"type": "string", "description": "Body font name."},
                        },
                    },
                    "slides": {
                        "type": "array",
                        "description": "Slides. Use layout for auto-layout or elements for manual control.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "layout": {
                                    "type": "object",
                                    "description": "Auto-layout (recommended). Set type+items; coords auto-computed. Use 'cards' for normal content.",
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
                                            "description": "Layout type. Unknown values fall back to 'cards' with a warning.",
                                        },
                                        "title": {"type": "string", "description": "Slide title."},
                                        "page_num": {"type": "integer", "description": "Page number."},
                                        "items": {"type": "array", "description": "Content items; shape varies by layout type."},
                                        "subtitle": {"type": "string", "description": "Subtitle (cover/closing)."},
                                        "date": {"type": "string", "description": "Date (cover)."},
                                        "author": {"type": "string", "description": "Author (cover)."},
                                        "left": {"type": "object", "description": "Left side (comparison): {title, items}."},
                                        "right": {"type": "object", "description": "Right side (comparison): {title, items}."},
                                        "headers": {"type": "array", "description": "Table headers."},
                                        "rows": {"type": "array", "description": "Table data rows."},
                                        "summary": {"type": "string", "description": "Bottom caption text."},
                                    },
                                },
                                "background": {
                                    "type": "string",
                                    "description": "Slide background hex, overrides theme default.",
                                },
                                "elements": {
                                    "type": "array",
                                    "description": "Manual elements appended after auto-layout. Each needs type + x,y,w,h (inches).",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "type": {"type": "string", "description": "text | shape | chart | table | image | icon_circle | line."},
                                            "x": {"type": "number", "description": "Left in inches."},
                                            "y": {"type": "number", "description": "Top in inches."},
                                            "w": {"type": "number", "description": "Width in inches."},
                                            "h": {"type": "number", "description": "Height in inches."},
                                            "content": {"type": "string", "description": "Text content; \\n for line break."},
                                            "font_size": {"type": "number", "description": "Font size in pt."},
                                            "font_name": {"type": "string", "description": "Font name (text)."},
                                            "bold": {"type": "boolean", "description": "Bold (text)."},
                                            "italic": {"type": "boolean", "description": "Italic (text)."},
                                            "color": {"type": "string", "description": "Text/icon font color hex."},
                                            "bg_color": {"type": "string", "description": "Text box background hex."},
                                            "align": {"type": "string", "description": "Text align: left | center | right."},
                                            "valign": {"type": "string", "description": "Vertical align: top | middle | bottom."},
                                            "line_spacing": {"type": "number", "description": "Line spacing multiplier (e.g. 1.5)."},
                                            "shape_type": {"type": "string", "description": "rectangle|rounded_rect|oval|triangle|arrow_right|arrow_left|chevron|diamond|pentagon|hexagon|star."},
                                            "fill_color": {"type": "string", "description": "Shape/icon fill hex."},
                                            "line_color": {"type": "string", "description": "Shape/line color hex."},
                                            "line_width": {"type": "number", "description": "Shape/line width in pt."},
                                            "rotation": {"type": "number", "description": "Rotation in degrees (shape)."},
                                            "chart_type": {"type": "string", "description": "bar | column | line | pie | doughnut | radar | area."},
                                            "categories": {"type": "array", "items": {"type": "string"}, "description": "Chart category labels."},
                                            "series": {"type": "array", "description": "Chart data series [{name, values}]."},
                                            "colors": {"type": "array", "items": {"type": "string"}, "description": "Chart series colors."},
                                            "show_labels": {"type": "boolean", "description": "Show data labels (chart)."},
                                            "show_percent": {"type": "boolean", "description": "Show percentages (pie)."},
                                            "show_legend": {"type": "boolean", "description": "Show legend (chart)."},
                                            "headers": {"type": "array", "items": {"type": "string"}, "description": "Table headers."},
                                            "rows": {"type": "array", "description": "Table data rows [[cell,...],...]."},
                                            "header_color": {"type": "string", "description": "Table header background."},
                                            "header_font_color": {"type": "string", "description": "Table header font color."},
                                            "stripe_color": {"type": "string", "description": "Table zebra-stripe color."},
                                            "path": {"type": "string", "description": "Image file path."},
                                            "text": {"type": "string", "description": "Text inside icon_circle."},
                                            "font_color": {"type": "string", "description": "Icon_circle text color."},
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
            "description": "Screenshot the desktop primary monitor. Optional region crop. Not for web pages.",
            "parameters": {
                "type": "object",
                "properties": {
                    "output_path": {
                        "type": "string",
                        "description": "Optional PNG save path (default auto-generated).",
                    },
                    "region": {
                        "type": "object",
                        "description": "Optional crop region (x, y, w, h).",
                        "properties": {
                            "x": {"type": "integer", "description": "Top-left X coordinate."},
                            "y": {"type": "integer", "description": "Top-left Y coordinate."},
                            "w": {"type": "integer", "description": "Width in pixels."},
                            "h": {"type": "integer", "description": "Height in pixels."},
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
            "description": "Stitch image frames into an MP4. Optional audio. Auto-installs moviepy.",
            "parameters": {
                "type": "object",
                "properties": {
                    "output_path": {
                        "type": "string",
                        "description": "Path where the .mp4 will be saved.",
                    },
                    "frames": {
                        "type": "array",
                        "description": "Frame objects with image_path and optional duration.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "image_path": {
                                    "type": "string",
                                    "description": "Image file path.",
                                },
                                "duration": {
                                    "type": "number",
                                    "description": "Display duration in seconds (default 3).",
                                },
                            },
                            "required": ["image_path"],
                        },
                    },
                    "fps": {
                        "type": "integer",
                        "description": "Frames per second (default 24).",
                    },
                    "audio_path": {
                        "type": "string",
                        "description": "Optional soundtrack file path.",
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
            "description": "Load a granted skill's guide. Default brief mode. Not for MCP or registering.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name (e.g. 'pdf', 'docx').",
                    },
                    "brief": {
                        "type": "boolean",
                        "description": "Default true; return headings/summary only.",
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "Optional agent ID to resolve agent-local skill path.",
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
            "description": "Auto-generate skill draft from experience patterns. Needs admin approval.",
            "parameters": {
                "type": "object",
                "properties": {
                    "role": {
                        "type": "string",
                        "description": "Limit scan to this role (empty = all roles).",
                    },
                    "topic": {
                        "type": "string",
                        "description": "Optional topic hint for the experience cluster.",
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
            "description": "Submit a hand-written skill package for admin approval. Needs manifest.yaml + SKILL.md.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dir_name": {
                        "type": "string",
                        "description": "Skill directory name in your workspace (e.g. 'pptx_skill').",
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
            "description": "Propose splitting current project task into parallel sub-tasks. Persists a draft only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "parent_task_id": {"type": "string", "description": "ProjectTask id being decomposed."},
                    "title": {"type": "string", "description": "Short label."},
                    "summary": {"type": "string", "description": "Plain-language pitch of the strategy."},
                    "prd": {"type": "string", "description": "Optional PRD markdown. Empty = use existing PRD.md."},
                    "scaffold_dirs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Dirs to mkdir under project root (e.g. 'backend/auth')."
                    },
                    "sub_tasks": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "Optional stable id; used in depends_on refs."},
                                "title": {"type": "string"},
                                "description": {"type": "string"},
                                "role_hint": {"type": "string", "enum": ["coder", "researcher", "general", "advisor"]},
                                "output_path": {"type": "string", "description": "Relative dir under project root; becomes agent's wd."},
                                "acceptance": {"type": "string", "description": "One-line 'done when X' criterion."},
                                "order": {"type": "integer"},
                                "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Sub-task ids that must be done first."}
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
            "description": "Query project shared context DB. Tables: artifacts/decisions/milestones/handoffs/etc.",
            "parameters": {
                "type": "object",
                "properties": {
                    "table": {"type": "string", "enum": ["artifacts", "decisions", "milestones", "handoffs", "pending_qs", "summary"]},
                    "kind": {"type": "string", "description": "Artifacts only: document|code|data|image|report|config."},
                    "status": {"type": "string", "description": "Filter by row status (varies by table)."},
                    "dst_agent": {"type": "string", "description": "Filter handoffs/pending_qs by destination agent."},
                    "since_ts": {"type": "number", "description": "Unix ts; only rows newer than this."},
                    "limit": {"type": "integer", "description": "Max rows (default 10, capped 50)."},
                    "project_id": {"type": "string", "description": "Optional; inferred from chat context."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sc_register_artifact",
            "description": "Register a workspace file as sharable artifact (card only; file stays in workspace).",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Workspace-relative file path."},
                    "title": {"type": "string", "description": "Human-readable title (defaults to path)."},
                    "summary": {"type": "string", "description": "Content preview, max 200 chars."},
                    "kind": {"type": "string", "enum": ["document", "code", "data", "image", "report", "config", "other"]},
                    "token_count": {"type": "integer", "description": "Approx token count of the file."},
                    "project_id": {"type": "string", "description": "Optional; inferred from chat context."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sc_get_artifact",
            "description": "Fetch full record of an artifact (path/title/summary/creator) by its art_* id.",
            "parameters": {
                "type": "object",
                "properties": {
                    "artifact_id": {"type": "string", "description": "Artifact id, e.g. art_a1b2c3d4."},
                },
                "required": ["artifact_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sc_record_decision",
            "description": "Append a team-wide decision to the project log. Use supersedes_id when overriding.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "What was being decided."},
                    "decision": {"type": "string", "description": "The chosen answer."},
                    "rationale": {"type": "string", "description": "Why this choice (recommended)."},
                    "supersedes_id": {"type": "string", "description": "Prior dec_* id this overrides."},
                    "project_id": {"type": "string", "description": "Optional; inferred from chat context."},
                },
                "required": ["topic", "decision"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sc_handoff",
            "description": "Pull-model handoff: write to handoffs table; receiver polls it. Token-cheap.",
            "parameters": {
                "type": "object",
                "properties": {
                    "dst_agent": {"type": "string", "description": "Receiver agent id or name."},
                    "intent": {"type": "string", "description": "What the receiver should do, max 500 chars."},
                    "summary": {"type": "string", "description": "Optional 1-2 line context, max 300 chars."},
                    "artifact_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "art_* ids the receiver needs. Do not paste file content.",
                    },
                    "project_id": {"type": "string", "description": "Optional; inferred from chat context."},
                },
                "required": ["dst_agent", "intent"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_deliverable",
            "description": "Register an artifact as project deliverable; enters review queue. Project chat only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short title for the deliverable."},
                    "file_path": {"type": "string", "description": "Absolute or relative path to the artifact file."},
                    "content_text": {"type": "string", "description": "Inline content for text-only deliverables."},
                    "url": {"type": "string", "description": "External URL for hosted artifacts."},
                    "kind": {"type": "string", "description": "document | code | design | analysis | other (default document)."},
                    "milestone_id": {"type": "string", "description": "Optional milestone id link."},
                    "task_id": {"type": "string", "description": "Optional task id that produced this."},
                    "project_id": {"type": "string", "description": "Optional; inferred from chat context."},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_goal",
            "description": "Create a measurable project goal (count/percent number or text). Project context only.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Short goal name."},
                    "description": {"type": "string", "description": "Longer description / rationale."},
                    "metric": {"type": "string", "description": "count | percent | text (default count)."},
                    "target_value": {"type": "number", "description": "Numeric target for count/percent metrics."},
                    "target_text": {"type": "string", "description": "Qualitative target for text metric."},
                    "owner_agent_id": {"type": "string", "description": "Optional owner agent id (default caller)."},
                    "project_id": {"type": "string", "description": "Optional; inferred from chat context."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_goal_progress",
            "description": "Update a goal's current value or mark done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string", "description": "Goal id to update."},
                    "current_value": {"type": "number", "description": "New current value (count/percent)."},
                    "done": {"type": "boolean", "description": "Mark as complete."},
                    "note": {"type": "string", "description": "Optional progress note."},
                    "project_id": {"type": "string", "description": "Optional; inferred from chat context."},
                },
                "required": ["goal_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_milestone",
            "description": "Create a milestone; optionally delegate to another agent (auto-fires @-mention).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Milestone name."},
                    "responsible_agent_id": {
                        "type": "string",
                        "description": "Agent id. Pass another's id to delegate; default = self.",
                    },
                    "description": {
                        "type": "string",
                        "description": "Optional context shown to responsible agent on delegation.",
                    },
                    "due_date": {"type": "string", "description": "Due date YYYY-MM-DD or natural form."},
                    "project_id": {"type": "string", "description": "Optional; inferred from chat context."},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_milestone_responsibility",
            "description": "Reassign milestone to a new agent; auto-notifies new and old owner.",
            "parameters": {
                "type": "object",
                "properties": {
                    "milestone_id": {"type": "string", "description": "Milestone id to reassign."},
                    "new_responsible_agent_id": {
                        "type": "string",
                        "description": "Agent id of the new responsible owner.",
                    },
                    "reason": {
                        "type": "string",
                        "description": "One-line reason; shown to new and old owner. Recommended.",
                    },
                    "notify_old": {
                        "type": "boolean",
                        "description": "Send courtesy notice to old owner (default true).",
                    },
                    "project_id": {"type": "string", "description": "Optional; inferred from chat context."},
                },
                "required": ["milestone_id", "new_responsible_agent_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_milestone_status",
            "description": "Update milestone status (pending|in_progress|done) or attach evidence.",
            "parameters": {
                "type": "object",
                "properties": {
                    "milestone_id": {"type": "string", "description": "Milestone id."},
                    "status": {"type": "string", "description": "pending | in_progress | done."},
                    "evidence": {"type": "string", "description": "Evidence text (links, completion summary)."},
                    "project_id": {"type": "string", "description": "Optional; inferred from chat context."},
                },
                "required": ["milestone_id"],
            },
        },
    },
    # Phase 3 (2026-05-06) — Issue / risk tracking
    {
        "type": "function",
        "function": {
            "name": "report_issue",
            "description": "Report a project issue/risk/blocker. Surfaces in Issues tab and chat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "1-line summary, max 200 chars."},
                    "description": {"type": "string", "description": "Details: what happened, what tried, what needed."},
                    "severity": {"type": "string", "description": "low | medium | high | critical (default medium)."},
                    "related_task_id": {"type": "string", "description": "Optional ProjectTask id."},
                    "related_milestone_id": {"type": "string", "description": "Optional milestone id."},
                    "project_id": {"type": "string", "description": "Auto from scope if omitted."},
                },
                "required": ["title"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_issue",
            "description": "Update an issue: status, resolution, reassign, severity.",
            "parameters": {
                "type": "object",
                "properties": {
                    "issue_id": {"type": "string", "description": "Issue id."},
                    "status": {"type": "string", "description": "open | investigating | resolved | wontfix."},
                    "resolution": {"type": "string", "description": "Resolution text; required when status=resolved."},
                    "severity": {"type": "string", "description": "Override severity."},
                    "assigned_to": {"type": "string", "description": "Reassign to another agent_id."},
                    "project_id": {"type": "string", "description": "Auto from scope."},
                },
                "required": ["issue_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_issues",
            "description": "List project issues filtered by status (default 'open'). Pass 'all' for all.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {"type": "string", "description": "open (default) | investigating | resolved | wontfix | all."},
                    "project_id": {"type": "string", "description": "Auto from scope."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "define_project_blueprint",
            "description": "PM-only: declare folder layout, milestone acceptance, anti-pattern rules.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {"type": "string",
                                    "description": "Project this blueprint applies to."},
                    "folders": {
                        "type": "array",
                        "description": "Per-folder rules: {path, writers, purpose}.",
                    },
                    "acceptance": {
                        "type": "array",
                        "description": "Per-milestone acceptance: {milestone_id, must_have_files}.",
                    },
                    "no_glob_in_chat": {
                        "type": "boolean",
                        "description": "Warn rule against glob_files/search_files in chat (default true).",
                    },
                    "tool_budget_per_turn": {
                        "type": "integer",
                        "description": "Advisory cap (informational).",
                    },
                    "revision_note": {
                        "type": "string",
                        "description": "Audit trail note for this change.",
                    },
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finalize_step",
            "description": "Atomic step closure: register deliverables, close step, optional milestone done.",
            "parameters": {
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "description": "File specs: {local_path required, title?, kind? (default code), milestone_id?}.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "local_path": {"type": "string",
                                                "description": "Absolute path (typically what you passed to write_file)."},
                                "title": {"type": "string",
                                           "description": "Short label; defaults to file basename."},
                                "kind": {"type": "string",
                                          "description": "document | code | design | analysis | other (default code)."},
                                "milestone_id": {"type": "string",
                                                  "description": "Per-file milestone link; overrides top-level."},
                            },
                            "required": ["local_path"],
                        },
                    },
                    "step_id": {
                        "type": "string",
                        "description": "Plan step id to close. Empty = skip plan_update.",
                    },
                    "milestone_id": {
                        "type": "string",
                        "description": "Optional milestone to mark done after register.",
                    },
                    "step_summary": {
                        "type": "string",
                        "description": "Stamp on closed step/milestone evidence; auto-built if empty.",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Optional; inferred from chat context.",
                    },
                },
                "required": ["files"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_review",
            "description": "Atomic milestone review: register report, file issues, transition status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "milestone_id": {
                        "type": "string",
                        "description": "Milestone being reviewed. Required.",
                    },
                    "decision": {
                        "type": "string",
                        "description": "approve | request_changes | reject (maps to done|blocked|cancelled).",
                    },
                    "summary": {
                        "type": "string",
                        "description": "Short review summary, max 200 chars.",
                    },
                    "issues": {
                        "type": "array",
                        "description": "Optional issues to file alongside the review.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "Issue title. Required."},
                                "severity": {"type": "string", "description": "low | medium | high | critical (default medium)."},
                                "description": {"type": "string", "description": "Issue body / repro steps."},
                                "milestone_id": {"type": "string", "description": "Per-issue milestone link (defaults to top-level)."},
                            },
                            "required": ["title"],
                        },
                    },
                    "deliverable_path": {
                        "type": "string",
                        "description": "Optional path to pre-written review report.",
                    },
                    "deliverable_title": {
                        "type": "string",
                        "description": "Title for the review report deliverable.",
                    },
                    "deliverable_content": {
                        "type": "string",
                        "description": "Inline content; written into shared dir if no path.",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Optional; inferred from chat context.",
                    },
                },
                "required": ["milestone_id", "decision"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bootstrap_project",
            "description": "Atomic project skeleton: blueprint + milestones + goals + tasks in one call.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project to bootstrap. Required.",
                    },
                    "blueprint": {
                        "type": "object",
                        "description": "Optional: folders, acceptance, no_glob_in_chat, tool_budget_per_turn.",
                    },
                    "milestones": {
                        "type": "array",
                        "description": "List of milestone specs.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Milestone name. Required."},
                                "responsible_agent_id": {"type": "string", "description": "Agent id of responsible owner."},
                                "description": {"type": "string", "description": "Longer milestone description."},
                                "due_date": {"type": "string", "description": "ISO date string."},
                            },
                            "required": ["name"],
                        },
                    },
                    "goals": {
                        "type": "array",
                        "description": "List of goal specs.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string", "description": "Goal name. Required."},
                                "description": {"type": "string", "description": "Goal description."},
                                "metric": {"type": "string", "description": "count | percent | text (default count)."},
                                "target_value": {"type": "number", "description": "Target for count/percent metrics."},
                                "target_text": {"type": "string", "description": "Target text for text metric."},
                                "owner_agent_id": {"type": "string", "description": "Owner agent id (default caller)."},
                            },
                            "required": ["name"],
                        },
                    },
                    "tasks": {
                        "type": "array",
                        "description": "List of task-dispatch specs.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string", "description": "Task title. Required."},
                                "assigned_to": {"type": "string", "description": "Agent id. Required."},
                                "milestone_id": {"type": "string", "description": "Optional milestone link."},
                                "description": {"type": "string", "description": "Task description/acceptance."},
                                "priority": {"type": "string", "description": "low | normal | high | urgent."},
                                "due_date": {"type": "string", "description": "ISO date string."},
                                "llm_label": {"type": "string", "description": "Optional LLM-router slot hint."},
                            },
                            "required": ["title", "assigned_to"],
                        },
                    },
                    "revision_note": {
                        "type": "string",
                        "description": "Audit-trail note for blueprint.",
                    },
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agent_todo",
            "description": "Your private in-memory todo list across turns. Cap 20. Only one in_progress.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "get | set | update_one | clear (default 'get').",
                    },
                    "todos": {
                        "type": "array",
                        "description": "For action='set': full replacement list, max 20.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string", "description": "Short id you choose; auto-numbered if omitted."},
                                "content": {"type": "string", "description": "What to do, max 200 chars. Required."},
                                "status": {"type": "string", "description": "pending | in_progress | completed (default pending)."},
                                "activeForm": {"type": "string", "description": "Gerund display form (e.g. 'Implementing X')."},
                            },
                            "required": ["content"],
                        },
                    },
                    "todo_id": {
                        "type": "string",
                        "description": "For action='update_one': which item's status to flip.",
                    },
                    "status": {
                        "type": "string",
                        "description": "For action='update_one': new status.",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_explore_subagent",
            "description": "Spawn ephemeral subagent for focused read-only exploration. Keeps your context lean.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Specific bounded task for the subagent.",
                    },
                    "return_format": {
                        "type": "string",
                        "description": "summary (default, max 500) | full | list.",
                    },
                    "read_only_tools": {
                        "type": "boolean",
                        "description": "Restrict to read-only primitives (default true).",
                    },
                    "timeout_s": {
                        "type": "integer",
                        "description": "Wait timeout in seconds (default 180, clamped 10-600).",
                    },
                    "role": {
                        "type": "string",
                        "description": "Optional role hint (default: inherit parent).",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "init_project_context",
            "description": "Generate or refresh PROJECT_CONTEXT.md via init subagent. Idempotent unless force=true.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Project to initialise. Required.",
                    },
                    "force": {
                        "type": "boolean",
                        "description": "Overwrite existing PROJECT_CONTEXT.md (default false).",
                    },
                    "timeout_s": {
                        "type": "integer",
                        "description": "Wait timeout seconds (default 300, clamped 60-900).",
                    },
                    "extra_focus": {
                        "type": "string",
                        "description": "Optional area for subagent to focus on (e.g. 'auth flow').",
                    },
                },
                "required": ["project_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "project_state",
            "description": "Structured project state snapshot. Prefer over glob/search for status in projects.",
            "parameters": {
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "description": "my (default) | team | step | milestone | all.",
                    },
                    "project_id": {
                        "type": "string",
                        "description": "Project id. Required.",
                    },
                    "step_id": {
                        "type": "string",
                        "description": "Required for scope='step'. Partial prefix accepted.",
                    },
                    "milestone_id": {
                        "type": "string",
                        "description": "Required for scope='milestone'. Partial prefix accepted.",
                    },
                },
                "required": ["project_id"],
            },
        },
    },
    # ---- UI block tools (rich interactive messages) ----
    {
        "type": "function",
        "function": {
            "name": "emit_ui_block",
            "description": "Render interactive UI block (choice buttons or checklist) in chat.",
            "parameters": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": ["choice", "checklist"],
                        "description": "choice = clickable buttons; checklist = display-only.",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "Header text at top of block, max 400 chars.",
                    },
                    "options": {
                        "type": "array",
                        "description": "For choice. Each: string label or {id, label}.",
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
                        "description": "For checklist. Each: string text or {id, text, done}.",
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
            "description": "Structured baton-pass to next agent. At most one per task completion.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "One-paragraph what-I-did, max 500 chars.",
                    },
                    "deliverable_path": {
                        "type": "string",
                        "description": "Relative path of artifact in shared workspace; empty if none.",
                    },
                    "highlights": {
                        "type": "array",
                        "description": "Up to 6 key findings/decisions. String or {text}.",
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
                        "description": "Up to 8 next-step suggestions: {for, task}.",
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
    _tool_bash_logs,
    _tool_bash_kill,
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
    _tool_dispatch_task,
    _tool_accept_task,
    _tool_inbox_assignments,
    _tool_report_back,
    _tool_query_team_status,
    _tool_query_agent_status,
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
    _tool_report_issue,
    _tool_update_issue,
    _tool_list_issues,
    _tool_project_state,
    _tool_define_project_blueprint,
    _auto_report_issue,
)

# Composite tools — fold multi-step rituals into a single LLM round-trip.
#   finalize_step      — coder/researcher step closure
#   submit_review      — reviewer milestone closure
#   bootstrap_project  — PM project skeleton creation
from .tools_split.finalize import (  # noqa: E402,F401
    _tool_finalize_step,
    _tool_submit_review,
    _tool_bootstrap_project,
)

# Agent's private todo list (Claude-Code-style TodoWrite). In-memory,
# per-agent, not persisted. Complements plan_update (project-step
# level) and create_milestone (project checkpoint level).
from .tools_split.agent_todo import _tool_agent_todo  # noqa: E402,F401

# Ephemeral subagent spawn (Claude-Code-style Task). Off-loads focused
# read-only research/exploration to a stateless subagent so the parent's
# context stays lean.
from .tools_split.subagent import _tool_spawn_explore_subagent  # noqa: E402,F401

# Project-context bootstrap (Claude-Code-style /init). Spawns an init
# subagent that explores the project's shared dir and writes a
# PROJECT_CONTEXT.md so future agents skip the rediscovery cost.
from .tools_split.project_init import _tool_init_project_context  # noqa: E402,F401

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
    "bash_logs": _tool_bash_logs,
    "bash_kill": _tool_bash_kill,
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
    # Day 5 (2026-05-05) — Structured handoff
    "report_back": _tool_report_back,
    "dispatch_task": _tool_dispatch_task,
    "accept_task": _tool_accept_task,
    "inbox_assignments": _tool_inbox_assignments,
    # Phase 2 P2-6 (2026-05-06) — Team status query
    "query_team_status": _tool_query_team_status,
    "query_agent_status": _tool_query_agent_status,
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
    # Phase 3 (2026-05-06) — Issues / risks tracking
    "report_issue": _tool_report_issue,
    "update_issue": _tool_update_issue,
    "list_issues": _tool_list_issues,
    # L2 (2026-05-06) — structured state query, replaces glob_files for status
    "project_state": _tool_project_state,
    # L3 (2026-05-06) — PM one-shot blueprint that generates engine rules
    "define_project_blueprint": _tool_define_project_blueprint,
    # Composite step closure — submit_deliverable × N + plan_update +
    # update_milestone_status in one call (Claude-Code-style macro).
    "finalize_step": _tool_finalize_step,
    # Reviewer composite — submit review report + report issues +
    # transition milestone in one call.
    "submit_review": _tool_submit_review,
    # PM composite — define blueprint + create milestones / goals +
    # dispatch initial tasks in one call.
    "bootstrap_project": _tool_bootstrap_project,
    # Per-agent private todo list (Claude-Code-style TodoWrite).
    "agent_todo": _tool_agent_todo,
    # Ephemeral subagent spawn (Claude-Code-style Task tool).
    "spawn_explore_subagent": _tool_spawn_explore_subagent,
    # Project-context bootstrap (Claude-Code-style /init).
    "init_project_context": _tool_init_project_context,
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
