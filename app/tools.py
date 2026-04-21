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

        if canonical_name not in self._tools:
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
                "Read the contents of a file. Returns the file text. "
                "Supports optional offset (start line, 0-based) and limit (number of lines)."
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
            "description": "Write content to a file. Creates parent directories if needed. Overwrites existing content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path to write to"},
                    "content": {"type": "string", "description": "Content to write"},
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
                "Edit a file by performing an exact string replacement. "
                "Finds old_string in the file and replaces it with new_string. "
                "The old_string must appear exactly once for a unique match."
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
            "description": "Execute a shell command (also aliased as 'exec', 'shell', 'run_command'). Returns stdout/stderr. Dangerous commands require human approval. Configurable timeout (default 30s).",
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
    {
        "type": "function",
        "function": {
            "name": "search_files",
            "description": (
                "Search file contents using a regular expression (like grep -rn). "
                "Returns matching lines with file path and line number."
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
            "description": "Find files matching a glob pattern. Returns a list of matching file paths.",
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
                "Search the internet using DuckDuckGo. Returns search results with titles, "
                "URLs, and snippets. Use this to find up-to-date information from the web."
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
                "Fetch the text content of a web page URL. Returns the page content as plain text. "
                "Useful for reading articles, documentation, or any web page after finding it via web_search."
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
                "Invoke a tool on an external MCP server bound to this agent. "
                "Use this to call email (send_email), slack (send_message), github "
                "(create_pr), browser, postgres, or any other bound MCP. "
                "First call with list_mcps=true to see what MCPs are available and "
                "what tools each one provides."
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
                        "type": "object",
                        "description": "Arguments object to pass to the MCP tool",
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
                "Spawn a sub-agent for parallel task execution. The sub-agent runs "
                "independently with its own context window and tools. Use this when a "
                "task can be decomposed into independent sub-tasks that run in parallel "
                "(e.g., 3 sub-agents running in parallel = ~1 min vs 3 min serial)."
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
                "Send a one-way message to another agent (broadcast / FYI / share status). "
                "Use this for non-blocking communication — announcements, progress pings, "
                "sharing intermediate findings — where you do NOT expect a structured result back. "
                "DO NOT use this for work handoffs: if you are transferring a task and expect the "
                "receiver to produce output, call `handoff_request` instead (it gives a visible "
                "3-state ack/complete handshake). Saying \"I'll ask X to do Y\" is not a substitute "
                "for calling one of these tools — pick the right one and call it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to_agent": {
                        "type": "string",
                        "description": "Agent ID or name to send the message to",
                    },
                    "content": {"type": "string", "description": "Message content"},
                    "msg_type": {
                        "type": "string",
                        "description": "Message type: task | info | result | question (default: task)",
                    },
                },
                "required": ["to_agent", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "handoff_request",
            "description": (
                "Hand off a task to another agent with a visible 3-state handshake "
                "(pending → acknowledged → completed). Use this (not send_message) "
                "whenever you are transferring work to a teammate and expect them "
                "to produce a result — e.g. coder → tester for verification, "
                "researcher → writer for drafting. The user sees a badge "
                "that flips ⏳ → ✅ → ✔️ as the receiver picks it up and "
                "finishes. This call blocks until the receiver returns or fails. "
                "Do NOT use send_message + @mention for handoffs — that is a "
                "one-way broadcast with no acknowledgement."
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
                "Create / update / complete entries in the shared task list. Call this DIRECTLY — "
                "do not describe the task in prose and then stop; the call IS the record.\n"
                "• RECURRING: When the user asks for periodic work (\"每天9点\", \"每周一\", "
                "\"daily\", \"weekly\"), call action=create with recurrence + recurrence_spec.\n"
                "• DELAYED ONE-TIME: When the user asks \"5分钟后\", \"in 10 mins\", "
                "\"下午3点做X\", call action=create with run_at (e.g. run_at='+5m').\n"
                "The scheduler fires these tasks automatically at the configured time. "
                "Do NOT reply that you cannot run scheduled tasks."
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
            "name": "plan_update",
            "description": (
                "Write / advance your execution plan — the visible step-by-step checklist "
                "the user sees as your progress indicator. At the START of any multi-step "
                "task, CALL this with action=create_plan BEFORE doing the work — do not "
                "describe the plan in prose and then stop (the checklist IS the plan). "
                "As you execute, call start_step → complete_step for each item so the "
                "user watches progress live. "
                "Actions: create_plan (set task_summary + steps array), "
                "start_step (mark a step as in_progress), "
                "complete_step (mark done with result_summary), "
                "add_step (add new step during execution), "
                "fail_step (mark as failed), "
                "replan (keep completed steps, replace pending with new steps)."
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
                                "title": {"type": "string"},
                                "detail": {"type": "string"},
                                "depends_on": {"type": "array", "items": {"type": "string"}, "description": "Step IDs this step depends on"},
                            },
                        },
                        "description": "List of step objects with title and optional detail (for create_plan)",
                    },
                    "step_id": {
                        "type": "string",
                        "description": "Step ID to update (for start_step/complete_step/fail_step)",
                    },
                    "title": {
                        "type": "string",
                        "description": "Step title (for add_step)",
                    },
                    "result_summary": {
                        "type": "string",
                        "description": "Brief result description (for complete_step/fail_step)",
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
                "Take a screenshot of a web page. Returns the screenshot as a base64-encoded "
                "PNG image saved to a file. Requires Playwright or falls back to a simple "
                "HTML-to-image approach. Useful for capturing visual state of web pages, "
                "generating thumbnails, or documenting UI."
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
                "Make an HTTP request (GET, POST, PUT, DELETE, PATCH) to any URL. "
                "Supports custom headers, JSON body, and form data. Useful for calling "
                "REST APIs, webhooks, or testing endpoints. Returns status code, headers, and body."
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
                "Perform date/time calculations. Get current time in any timezone, "
                "calculate date differences, add/subtract durations, format dates, "
                "convert between timezones. Use this instead of bash for date operations."
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
                "Process JSON data: parse, format/pretty-print, extract fields using "
                "JSONPath-like expressions, transform, validate, or convert between "
                "JSON/CSV/YAML. Useful for data manipulation tasks."
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
                "Process and transform text: count words/lines/chars, find & replace with regex, "
                "extract patterns, sort lines, deduplicate, base64 encode/decode, URL encode/decode, "
                "generate hash (md5/sha256), convert encoding. Batch text operations without bash."
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
    # ---- Experience persistence ----
    # NOTE: 经验条目(experience) 写入 experience_library 对应角色分桶。
    # 当经验积累到一定程度, agent 可通过 propose_skill 工具提议将经验
    # 锻造为技能(skill), 提交管理员审批后正式导入技能商店。
    {
        "type": "function",
        "function": {
            "name": "save_experience",
            "description": (
                "Persist a retrospective / active-learning finding as a reusable experience entry "
                "in the calling agent's role-based experience library. Use this for lessons learned, "
                "do/don't rules, and scene-specific action playbooks. "
                "After accumulating enough experiences on a topic, use propose_skill to "
                "crystallize them into a reusable skill package for admin approval."
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
                "Look up shared knowledge base entries. Use this when you need reference information "
                "like design guidelines, tech stack standards, website lists, coding conventions, etc. "
                "Pass a search query to find relevant entries, or pass an entry_id to read a specific entry."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search keyword or entry title",
                    },
                    "entry_id": {
                        "type": "string",
                        "description": "Specific entry ID to read (from a previous search result)",
                    },
                },
            },
        },
    },
    # ---- Cross-agent knowledge sharing ----
    {
        "type": "function",
        "function": {
            "name": "share_knowledge",
            "description": (
                "Share knowledge, best practices, or experience insights with all agents "
                "via the shared Knowledge Base. Use this when you've learned something valuable "
                "that other agents could benefit from — e.g., 'How to create professional PPTXs', "
                "'Best practices for API error handling', etc. All agents can read shared knowledge."
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
                "Learn from other agents' experiences. Browse and import high-quality experiences "
                "from other roles to expand your own capabilities. For example, a PM agent can learn "
                "design skills from a designer agent's experience pool."
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
                "Explicitly request the user to log into a website. "
                "Shows an interactive login card (iframe, credential form, cookie/token). "
                "Note: login walls encountered during browser navigation are handled automatically — "
                "you only need this tool to proactively request login before navigating."
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
            "description": "Install or upgrade Python packages using pip. Supports space-separated package names.",
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
            "description": "Create a PowerPoint presentation (.pptx) file with custom slides. Auto-installs python-pptx if needed.",
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
                "创建高级精美PowerPoint演示文稿。支持形状、图表、表格、多栏布局、信息图表等专业元素。"
                "每页通过 layout（智能布局）或 elements 数组（手动控制）来定义内容。"
                "\n\n**重要 — layout.type 必须是下列已注册类型之一**：cover（封面）/ "
                "toc（目录）/ section（章节分隔页）/ cards（通用内容卡片，**做普通内容页首选**）/ "
                "process（流程步骤）/ kpi（关键指标）/ comparison（左右对比）/ "
                "timeline（时间轴）/ chart / table / closing（结尾页）。"
                "\n\n❌ 绝不要编造新类型（如 overview / analysis / content / status / summary 等）——"
                "未注册类型会自动降级为 cards，但会丢失语义。"
                "\n✅ 普通内容页、列表页、要点页统一用 `cards`（items 支持 1-9 个）。"
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
            "description": "Take a screenshot of the local desktop. Can specify a region to crop. Returns PNG image path.",
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
            "description": "Create a video file from image frames. Auto-installs moviepy if needed. Can add audio track.",
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
                "Load the full operating guide for a granted skill. "
                "Returns the complete SKILL.md instructions, install_dir path, "
                "and ancillary file list. Use this when you need detailed "
                "step-by-step instructions or scripts for a specific skill "
                "(e.g. pdf, docx, xlsx, pptx). Run scripts from skill_dir."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name (e.g. 'pdf', 'docx', 'xlsx')",
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
                "Scan the experience library for recurring patterns and propose a new skill draft. "
                "When enough similar, high-success-rate experiences exist (≥3, ≥75% success), "
                "SkillForge clusters them and generates a skill package (SKILL.md + manifest.yaml). "
                "The draft enters a pending-approval queue visible to admin in the portal. "
                "Use this after accumulating experiences on a topic (e.g. PPTX best practices, "
                "code review checklists) to crystallize them into a reusable skill."
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
                "Submit a skill package directory you created in your workspace for admin approval. "
                "The directory MUST contain a valid manifest.yaml with ALL required fields, and a SKILL.md.\n\n"
                "manifest.yaml REQUIRED fields:\n"
                "  name: string          # skill name, kebab-case (e.g. pptx-maker)\n"
                "  version: string       # semver (e.g. 1.0.0)\n"
                "  description: string   # one-line description\n"
                "  runtime: string       # MUST be one of: python, shell, markdown\n"
                "  author: string        # your name\n"
                "  entry: string         # entry file (e.g. main.py for python, SKILL.md for markdown)\n\n"
                "manifest.yaml OPTIONAL fields:\n"
                "  display_name: string  # human-friendly name\n"
                "  tags: [string]        # classification tags\n"
                "  triggers: [string]    # activation keywords\n"
                "  depends_on: object    # MCP or other dependencies\n"
                "  inputs: [{name, type, required, description}]  # input parameters\n"
                "  outputs: [{name, type}]  # output fields\n"
                "  hint: {sensitive: bool}  # security hint\n\n"
                "SKILL.md should document: what the skill does, how to use it, examples, and caveats.\n\n"
                "IMPORTANT for python runtime skills:\n"
                "  - The entry file (e.g. main.py) MUST define: def run(ctx, **kwargs)\n"
                "  - ctx provides: ctx.env('KEY') for environment variables, ctx.log(...), ctx.output(...)\n"
                "  - Do NOT use open(), exec(), eval(), __import__() — these are forbidden by the sandbox\n"
                "  - Use ctx.output(name, value) to return results instead of print()\n\n"
                "After submission the draft appears in the SkillForge review queue for admin approval."
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
            "name": "submit_deliverable",
            "description": (
                "Register a deliverable for the current project and mark it "
                "SUBMITTED (ready for review). Use this when you have produced "
                "a concrete artifact (document / code / analysis / design). "
                "Provide at least one of file_path, content_text, or url. "
                "If content_text is supplied WITHOUT file_path, the content is "
                "automatically written as a file in the project's shared "
                "workspace (~/.tudou_claw/workspaces/shared/<project_id>/), so "
                "it appears in the project deliverables directory. "
                "Auto-discovers project from chat context; pass project_id to override."
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
                "Create a measurable goal for the current project. "
                "Use metric='count' with target_value for numeric goals, "
                "or metric='text' with target_text for qualitative goals. "
                "Auto-discovers project from chat context."
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
                "Update a goal's current value and/or mark it done. "
                "Call this when progress is made toward a goal you or your "
                "teammates previously created."
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
                "Create a milestone for the current project. Milestones mark "
                "major checkpoints and typically bundle multiple deliverables. "
                "Auto-discovers project from chat context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Milestone name"},
                    "responsible_agent_id": {"type": "string", "description": "Optional responsible agent id (default: calling agent)"},
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
            "name": "update_milestone_status",
            "description": (
                "Update a milestone's status or attach evidence. "
                "Status transitions are typically: pending → in_progress → done. "
                "Admin confirm/reject happens via a separate flow."
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


# ---------------------------------------------------------------------------
# Coordination tools — TeamCreate / SendMessage / TaskUpdate
# ---------------------------------------------------------------------------
# Handlers + _parse_run_at helper moved to app/tools_split/coordination.py.
from .tools_split.coordination import (  # noqa: E402,F401
    _tool_team_create,
    _tool_send_message,
    _tool_task_update,
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
    _tool_update_milestone_status,
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
    # Project-scope tools (auto-discover project from thread-local context)
    "submit_deliverable": _tool_submit_deliverable,
    "create_goal": _tool_create_goal,
    "update_goal_progress": _tool_update_goal_progress,
    "create_milestone": _tool_create_milestone,
    "update_milestone_status": _tool_update_milestone_status,
    "mcp_call": _tool_mcp_call,
    # Experience persistence + skill generation
    "save_experience": _tool_save_experience,
    "propose_skill": _tool_propose_skill,
    "submit_skill": _tool_submit_skill,
    # Knowledge management tools
    "knowledge_lookup": _tool_knowledge_lookup,
    "share_knowledge": _tool_share_knowledge,
    "learn_from_peers": _tool_learn_from_peers,
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
        "mcp_call": "coordination",

        # Skill management
        "save_experience": "coordination",
        "propose_skill": "skill",
        "submit_skill": "skill",

        # Knowledge management
        "knowledge_lookup": "coordination",
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
