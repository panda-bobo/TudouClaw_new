"""
Agent — encapsulates an AI agent with its own conversation, tools, config & lifecycle.

Each Agent has a rich profile: personality, communication style, expertise,
skills, language, and configurable tool permissions. Supports policy-based
approval for dangerous tool executions.

Task system tracks per-agent work items (todo/in_progress/done/blocked).
MCP integration allows each agent to connect to external Model Context Protocol servers.

NOTE: This module has been partially refactored. Standalone types live in
``agent_types.py``, chat task management in ``chat_task.py``, and Agent
method groups in mixin modules (``agent_llm.py``, ``agent_execution.py``,
``agent_growth.py``).  All public names are re-exported from here for full
backward compatibility — existing ``from app.agent import X`` statements
continue to work unchanged.
"""
from __future__ import annotations
import concurrent.futures
import json
import logging
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

# ── Re-export extracted types for backward compatibility ──
from .agent_types import (                                        # noqa: F401
    _ensure_str_content,
    AgentStatus, AgentPhase,
    TaskStatus, TaskSource, AgentTask,
    StepStatus, ExecutionStep, ExecutionPlan,
    MCPServerConfig, AgentProfile, AgentEvent,
)
from .chat_task import (                                          # noqa: F401
    ChatTaskStatus, ChatTask, ChatTaskManager,
    get_chat_task_manager,
)
# ── Import mixins (Agent class inherits from these) ──
from .agent_llm import AgentLLMMixin                              # noqa: F401
from .agent_execution import AgentExecutionMixin                  # noqa: F401
from .agent_growth import AgentGrowthMixin                        # noqa: F401

def _ensure_str_content(content) -> str:
    """Normalize message content to string.

    OpenAI-compatible APIs may return content as a string, a list of content
    blocks (multimodal format), a dict, or None.  This helper guarantees a
    plain string so downstream code never hits 'list + str' TypeError.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # list of content blocks – extract text parts
        text_parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif "text" in block:
                    text_parts.append(block["text"])
                else:
                    text_parts.append(json.dumps(block, ensure_ascii=False))
            elif isinstance(block, str):
                text_parts.append(block)
            else:
                text_parts.append(str(block))
        return "\n".join(text_parts)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    return str(content)


_IMAGE_TYPES = frozenset({"image_url", "image", "input_image"})


def _strip_old_images(messages: list[dict]) -> list[dict]:
    """Replace base64 image data in all but the last user message.

    Keeps only the current turn's images; older images become a short
    text placeholder like ``[image from earlier turn]``. This prevents
    runaway token usage from accumulated base64 data URIs and avoids
    confusing the model with stale images.

    Returns a **new** list — never mutates the input.
    """
    # Find the index of the last user message that has multimodal content
    last_mm_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") == "user" and isinstance(m.get("content"), list):
            if any(isinstance(p, dict) and p.get("type") in _IMAGE_TYPES
                   for p in m["content"]):
                last_mm_idx = i
                break

    if last_mm_idx < 0:
        return messages  # no multimodal messages at all

    result = []
    for i, m in enumerate(messages):
        if (i != last_mm_idx
                and m.get("role") == "user"
                and isinstance(m.get("content"), list)):
            # Check if this message has image parts
            has_img = any(isinstance(p, dict) and p.get("type") in _IMAGE_TYPES
                         for p in m["content"])
            if has_img:
                # Keep text parts, replace images with placeholder
                new_parts = []
                img_count = 0
                for p in m["content"]:
                    if isinstance(p, dict) and p.get("type") in _IMAGE_TYPES:
                        img_count += 1
                    else:
                        new_parts.append(p)
                new_parts.append({
                    "type": "text",
                    "text": f"[{img_count} image(s) from earlier turn — omitted]",
                })
                result.append({**m, "content": new_parts})
                continue
        result.append(m)
    return result


# Three-layer memory
try:
    from .memory import get_memory_manager, MemoryManager, MemoryConfig
except ImportError:
    try:
        from app.core.memory import get_memory_manager, MemoryManager, MemoryConfig
    except ImportError:
        get_memory_manager = None  # type: ignore
        MemoryManager = None  # type: ignore
        MemoryConfig = None  # type: ignore

logger = logging.getLogger("tudou.agent")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-5s %(name)s | %(message)s",
        datefmt="%H:%M:%S"))
    logger.addHandler(_h)
    logger.setLevel(logging.DEBUG)

from . import llm, tools, security
from .tools import PARALLEL_SAFE_TOOLS, MAX_PARALLEL_WORKERS
from .enhancement import (AgentEnhancer, build_enhancer, build_multi_enhancer,
                           list_enhancement_presets)
from .template_library import get_template_library
from .core.execution_analyzer import ExecutionAnalyzer, analyze_and_grow
from .core.prompt_enhancer import get_prompt_pack_registry
from .core.role_growth_path import RoleGrowthPath, ROLE_GROWTH_PATHS

# --- src package integration ---
import sys as _sys
_src_root = str(Path(__file__).resolve().parent.parent / "src")
if _src_root not in _sys.path:
    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.session_store import StoredSession, save_session, load_session
from src.cost_tracker import CostTracker
from src.costHook import apply_cost_hook
from src.history import HistoryLog, HistoryEvent
from src.context import PortContext, build_port_context, render_context
from src.tools import (build_tool_backlog, find_tools as src_find_tools,
                       render_tool_index, execute_tool as src_execute_tool,
                       get_tools as src_get_tools, ToolExecution)
from src.commands import (execute_command as src_execute_command,
                          find_commands as src_find_commands, CommandExecution)
from src.transcript import TranscriptStore
from src.query_engine import QueryEnginePort, QueryEngineConfig, TurnResult
from src.execution_registry import (ExecutionRegistry, MirroredCommand,
                                     MirroredTool, build_execution_registry)
from src.tool_pool import ToolPool, assemble_tool_pool
from src.permissions import ToolPermissionContext
from src.runtime import PortRuntime, RuntimeSession, RoutedMatch
from src.models import PermissionDenial, UsageSummary, PortingModule


# ---------------------------------------------------------------------------
# Agent status
# ---------------------------------------------------------------------------

class AgentStatus(str, Enum):
    IDLE = "idle"
    BUSY = "busy"
    WAITING_APPROVAL = "waiting_approval"
    ERROR = "error"
    OFFLINE = "offline"


class AgentPhase(str, Enum):
    """State machine phases for task continuity.

    Controls how the agent routes incoming messages:
      IDLE      → no active task, new messages go to LLM normally
      PLANNING  → agent has decomposed a task into milestones/steps;
                   queries about plan/progress → local memory, no LLM
      EXECUTING → actively working through steps; interrupted tasks
                   resume from checkpoint via L3 memory injection
      REVIEWING → post-execution review/QA phase
      BLOCKED   → waiting for external input (user / another agent)
    """
    IDLE = "idle"
    PLANNING = "planning"
    EXECUTING = "executing"
    REVIEWING = "reviewing"
    BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# Task system
# ---------------------------------------------------------------------------

class TaskStatus(str, Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


class TaskSource(str, Enum):
    ADMIN = "admin"
    AGENT = "agent"
    SYSTEM = "system"
    USER = "user"


@dataclass
class AgentTask:
    """A trackable work item for an agent."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    title: str = ""
    description: str = ""
    status: TaskStatus = TaskStatus.TODO
    # priority: -1 = background growth (lowest, only run when no other task);
    #            0 = normal; 1 = high; 2 = urgent
    priority: int = 0
    parent_id: str = ""        # for sub-tasks
    assigned_by: str = ""      # who/what created it (hub, user, another agent)
    source: str = "admin"      # admin | agent | system | user
    source_agent_id: str = ""  # if source=agent, which agent created it
    result: str = ""           # summary when done
    deadline: float = 0.0      # unix timestamp, 0 = no deadline
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    tags: list[str] = field(default_factory=list)
    notified: bool = False     # whether agent has been notified of this task
    # ── Per-task LLM routing (override agent's default provider/model) ──
    # When set, the agent will use these for any LLM call made WHILE this task
    # is the currently executing task (see Agent._task_model_context).
    # Empty string = inherit agent's default provider/model.
    provider: str = ""
    model: str = ""
    # ── 方案乙: extra_llms 路由 label ──
    # 当 task 指定 llm_label 时，_resolve_effective_provider_model 会在
    # agent.extra_llms 里查找 label 或 purpose 相同的 slot，命中就用那
    # 个 provider/model。label 不命中会回退到默认 provider/model。
    llm_label: str = ""
    # Subkey of self_improvement._learning_queue if this is a growth task.
    learning_goal: str = ""
    knowledge_gap: str = ""
    # Recurrence: "once" | "daily" | "weekly" | "monthly" | "cron"
    recurrence: str = "once"
    # For daily: "HH:MM" (e.g. "09:00"). For weekly: "MON HH:MM".
    # For monthly: "D HH:MM" (e.g. "15 09:00"). For cron: raw cron string.
    recurrence_spec: str = ""
    # Unix timestamp of next scheduled run (0 = not scheduled)
    next_run_at: float = 0.0
    # Number of times this recurring task has fired
    run_count: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "priority": self.priority,
            "parent_id": self.parent_id,
            "assigned_by": self.assigned_by,
            "source": self.source,
            "source_agent_id": self.source_agent_id,
            "result": self.result,
            "deadline": self.deadline,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tags": self.tags,
            "notified": self.notified,
            "provider": self.provider,
            "model": self.model,
            "llm_label": self.llm_label,
            "learning_goal": self.learning_goal,
            "knowledge_gap": self.knowledge_gap,
            "recurrence": self.recurrence,
            "recurrence_spec": self.recurrence_spec,
            "next_run_at": self.next_run_at,
            "run_count": self.run_count,
        }

    @staticmethod
    def from_dict(d: dict) -> AgentTask:
        return AgentTask(
            id=d.get("id", ""),
            title=d.get("title", ""),
            description=d.get("description", ""),
            status=TaskStatus(d.get("status", "todo")),
            priority=d.get("priority", 0),
            parent_id=d.get("parent_id", ""),
            assigned_by=d.get("assigned_by", ""),
            source=d.get("source", "admin"),
            source_agent_id=d.get("source_agent_id", ""),
            result=d.get("result", ""),
            deadline=d.get("deadline", 0.0),
            created_at=d.get("created_at", 0),
            updated_at=d.get("updated_at", 0),
            tags=d.get("tags", []),
            notified=d.get("notified", False),
            provider=d.get("provider", "") or "",
            model=d.get("model", "") or "",
            llm_label=d.get("llm_label", "") or "",
            learning_goal=d.get("learning_goal", "") or "",
            knowledge_gap=d.get("knowledge_gap", "") or "",
            recurrence=d.get("recurrence", "once"),
            recurrence_spec=d.get("recurrence_spec", ""),
            next_run_at=float(d.get("next_run_at", 0.0) or 0.0),
            run_count=int(d.get("run_count", 0) or 0),
        )

    @property
    def deadline_str(self) -> str:
        if not self.deadline:
            return ""
        from datetime import datetime
        return datetime.fromtimestamp(self.deadline).strftime("%Y-%m-%d %H:%M")

    @property
    def is_overdue(self) -> bool:
        return self.deadline > 0 and time.time() > self.deadline and self.status not in (TaskStatus.DONE, TaskStatus.CANCELLED)


# ---------------------------------------------------------------------------
# Execution Steps — 执行步骤分解 (类似 Claude 的 TodoList)
# ---------------------------------------------------------------------------

class StepStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ExecutionStep:
    """A single step in an agent's execution plan.

    Used to track real-time task decomposition — the agent breaks down
    a user request into steps, and marks them as it progresses.
    Similar to Claude's Todo widget.
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    title: str = ""               # short description: "Read existing code"
    detail: str = ""              # longer description when needed
    status: StepStatus = StepStatus.PENDING
    order: int = 0                # display order
    parent_step_id: str = ""      # for nested sub-steps
    depends_on: list[str] = field(default_factory=list)  # step IDs this step depends on
    started_at: float = 0.0
    completed_at: float = 0.0
    result_summary: str = ""      # brief result after completion

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title,
            "detail": self.detail,
            "status": self.status.value,
            "order": self.order,
            "parent_step_id": self.parent_step_id,
            "depends_on": self.depends_on,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result_summary": self.result_summary,
        }

    @staticmethod
    def from_dict(d: dict) -> "ExecutionStep":
        return ExecutionStep(
            id=d.get("id", uuid.uuid4().hex[:8]),
            title=d.get("title", ""),
            detail=d.get("detail", ""),
            status=StepStatus(d.get("status", "pending")),
            order=d.get("order", 0),
            parent_step_id=d.get("parent_step_id", ""),
            depends_on=d.get("depends_on", []),
            started_at=d.get("started_at", 0),
            completed_at=d.get("completed_at", 0),
            result_summary=d.get("result_summary", ""),
        )


@dataclass
class ExecutionPlan:
    """A plan containing multiple execution steps for a task.

    Each chat message that triggers tool usage creates a new plan.
    The agent decomposes the task into steps and updates them in real-time.
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    task_summary: str = ""        # what the user asked for
    steps: list[ExecutionStep] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    completed_at: float = 0.0
    status: str = "active"        # active | completed | failed

    def add_step(self, title: str, detail: str = "",
                 parent_step_id: str = "") -> ExecutionStep:
        step = ExecutionStep(
            title=title, detail=detail,
            order=len(self.steps),
            parent_step_id=parent_step_id,
        )
        self.steps.append(step)
        return step

    def start_step(self, step_id: str):
        for s in self.steps:
            if s.id == step_id:
                s.status = StepStatus.IN_PROGRESS
                s.started_at = time.time()
                return s
        return None

    def complete_step(self, step_id: str, result_summary: str = ""):
        step_found = None
        for s in self.steps:
            if s.id == step_id:
                s.status = StepStatus.COMPLETED
                s.completed_at = time.time()
                s.result_summary = result_summary
                step_found = s
                break
        # After completing a step, check if all steps are done
        if all(s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED)
               for s in self.steps):
            self.status = "completed"
            self.completed_at = time.time()
        return step_found

    def fail_step(self, step_id: str, error: str = ""):
        for s in self.steps:
            if s.id == step_id:
                s.status = StepStatus.FAILED
                s.completed_at = time.time()
                s.result_summary = error
                return s
        return None

    def get_progress(self) -> dict:
        total = len(self.steps)
        done = sum(1 for s in self.steps
                   if s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED))
        in_progress = sum(1 for s in self.steps
                          if s.status == StepStatus.IN_PROGRESS)
        return {
            "total": total, "done": done,
            "in_progress": in_progress,
            "pending": total - done - in_progress,
            "percent": int(done / total * 100) if total else 0,
        }

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_summary": self.task_summary,
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "progress": self.get_progress(),
        }

    @staticmethod
    def from_dict(d: dict) -> "ExecutionPlan":
        plan = ExecutionPlan(
            id=d.get("id", uuid.uuid4().hex[:10]),
            task_summary=d.get("task_summary", ""),
            created_at=d.get("created_at", time.time()),
            completed_at=d.get("completed_at", 0),
            status=d.get("status", "active"),
        )
        for sd in d.get("steps", []):
            plan.steps.append(ExecutionStep.from_dict(sd))
        return plan


# ---------------------------------------------------------------------------
# MCP server config (per agent)
# ---------------------------------------------------------------------------

@dataclass
class MCPServerConfig:
    """Configuration for an external MCP server connection."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = ""
    transport: str = "stdio"    # "stdio" | "sse" | "streamable-http"
    command: str = ""           # for stdio: e.g. "npx @modelcontextprotocol/server-filesystem /tmp"
    url: str = ""               # for sse/http: e.g. "http://localhost:3000/mcp"
    env: dict = field(default_factory=dict)
    enabled: bool = True
    # ── 作用域 ──
    scope: str = "node"               # "global" (API类) | "node" (需本地安装)
    # ── 安装状态 ──
    install_status: str = "unknown"   # "unknown"|"not_installed"|"installing"|"installed"|"failed"
    install_error: str = ""           # 安装失败时的错误信息
    install_command: str = ""         # 记录对应的安装命令
    installed_at: float = 0           # 安装成功的时间戳

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "url": self.url,
            "env": self.env,
            "enabled": self.enabled,
            "scope": self.scope,
            "install_status": self.install_status,
            "install_error": self.install_error,
            "install_command": self.install_command,
            "installed_at": self.installed_at,
        }

    @staticmethod
    def from_dict(d: dict) -> MCPServerConfig:
        return MCPServerConfig(
            id=d.get("id", ""),
            name=d.get("name", ""),
            transport=d.get("transport", "stdio"),
            command=d.get("command", ""),
            url=d.get("url", ""),
            env=d.get("env", {}),
            enabled=d.get("enabled", True),
            scope=d.get("scope", "node"),
            install_status=d.get("install_status", "unknown"),
            install_error=d.get("install_error", ""),
            install_command=d.get("install_command", ""),
            installed_at=d.get("installed_at", 0),
        )


# ---------------------------------------------------------------------------
# Agent profile — personality, style, expertise
# ---------------------------------------------------------------------------

@dataclass
class AgentProfile:
    """Rich agent configuration beyond just role."""
    agent_class: str = "enterprise"
    # Agent classification: "advisor" (专业领域顾问), "enterprise" (企业办公),
    # "personal" (个人应用).  Determines default capabilities, memory, and UI grouping.
    memory_mode: str = "full"
    # Memory persistence mode:
    #   "full"  — all 5 memory layers active (intent/reasoning/outcome/rule/reflection)
    #   "light" — L1 working memory + L2 recent N entries only
    #   "off"   — no persistent memory (stateless per session)
    rag_mode: str = "shared"
    # RAG knowledge retrieval mode:
    #   "shared"  — query the global shared knowledge base (enterprise default)
    #   "private" — query agent's own private knowledge collection (advisor default)
    #   "both"    — query private first, fall back to shared
    #   "none"    — no RAG retrieval (personal default)
    rag_provider_id: str = ""
    # RAG provider to use. Empty = local ChromaDB.
    # Can reference a registered RAG provider (e.g. a remote node endpoint,
    # third-party vector DB API, etc.) via the RAG provider registry.
    rag_collection_ids: list[str] = field(default_factory=list)
    # Additional knowledge collection IDs to query (for fine-grained control).
    # Advisor agents auto-get a private collection named "advisor_{agent_id}".
    # Users can also manually bind extra collections here.
    personality: str = "helpful"
    # e.g. "friendly", "formal", "concise", "patient", "strict"
    communication_style: str = "technical"
    # e.g. "technical", "casual", "detailed", "brief", "educational"
    expertise: list[str] = field(default_factory=list)
    # e.g. ["python", "rust", "kubernetes", "database", "security"]
    skills: list[str] = field(default_factory=list)
    # e.g. ["code_review", "testing", "refactoring", "documentation", "debugging"]
    language: str = "auto"
    # e.g. "zh-CN", "en", "ja", "auto" (follow user's language)
    max_context_messages: int = 50
    # Max messages to keep in context window
    allowed_tools: list[str] = field(default_factory=list)
    # Empty = all tools; non-empty = only these tools
    denied_tools: list[str] = field(default_factory=list)
    # Tools this agent is not allowed to use
    auto_approve_tools: list[str] = field(default_factory=list)
    # Tools that skip approval for this agent (e.g. coder can auto-approve write_file)
    temperature: float = 0.7
    # LLM temperature for this agent
    custom_instructions: str = ""
    # Extra instructions appended to system prompt
    exec_policy: str = "ask"
    # 'full' = auto-approve all, 'deny' = block all, 'ask' = prompt user
    exec_blacklist: list[str] = field(default_factory=list)
    # Commands that are always blocked for this agent
    exec_whitelist: list[str] = field(default_factory=list)
    # Commands that are always allowed for this agent
    mcp_servers: list = field(default_factory=list)
    # List of MCPServerConfig dicts for this agent
    sandbox_mode: str = ""
    # "" (use global default), "off", "restricted", or "strict"
    sandbox_allow_commands: list[str] = field(default_factory=list)
    # Command allowlist (first-token basenames) for strict sandbox mode
    skill_capabilities: list[str] = field(default_factory=list)
    # Permanently granted skill capabilities, e.g. ["pdf:rw", "docx:rw"]
    # Populated automatically when a skill is granted to the agent.

    def to_dict(self) -> dict:
        return {
            "agent_class": self.agent_class,
            "memory_mode": self.memory_mode,
            "rag_mode": self.rag_mode,
            "rag_provider_id": self.rag_provider_id,
            "rag_collection_ids": self.rag_collection_ids,
            "personality": self.personality,
            "communication_style": self.communication_style,
            "expertise": self.expertise,
            "skills": self.skills,
            "language": self.language,
            "max_context_messages": self.max_context_messages,
            "allowed_tools": self.allowed_tools,
            "denied_tools": self.denied_tools,
            "auto_approve_tools": self.auto_approve_tools,
            "temperature": self.temperature,
            "custom_instructions": self.custom_instructions,
            "exec_policy": self.exec_policy,
            "exec_blacklist": self.exec_blacklist,
            "exec_whitelist": self.exec_whitelist,
            "mcp_servers": [s.to_dict() if hasattr(s, 'to_dict') else s
                           for s in self.mcp_servers],
            "sandbox_mode": self.sandbox_mode,
            "sandbox_allow_commands": self.sandbox_allow_commands,
            "skill_capabilities": self.skill_capabilities,
        }

    @staticmethod
    def from_dict(d: dict) -> AgentProfile:
        mcp_servers = []
        for s in d.get("mcp_servers", []):
            if isinstance(s, dict):
                mcp_servers.append(MCPServerConfig.from_dict(s))
            elif isinstance(s, MCPServerConfig):
                mcp_servers.append(s)
        return AgentProfile(
            agent_class=d.get("agent_class", "enterprise"),
            memory_mode=d.get("memory_mode", "full"),
            rag_mode=d.get("rag_mode", "shared"),
            rag_provider_id=d.get("rag_provider_id", ""),
            rag_collection_ids=d.get("rag_collection_ids", []),
            personality=d.get("personality", "helpful"),
            communication_style=d.get("communication_style", "technical"),
            expertise=d.get("expertise", []),
            skills=d.get("skills", []),
            language=d.get("language", "auto"),
            max_context_messages=d.get("max_context_messages", 50),
            allowed_tools=d.get("allowed_tools", []),
            denied_tools=d.get("denied_tools", []),
            auto_approve_tools=d.get("auto_approve_tools", []),
            temperature=d.get("temperature", 0.7),
            custom_instructions=d.get("custom_instructions", ""),
            exec_policy=d.get("exec_policy", "ask"),
            exec_blacklist=d.get("exec_blacklist", []),
            exec_whitelist=d.get("exec_whitelist", []),
            mcp_servers=mcp_servers,
            sandbox_mode=d.get("sandbox_mode", ""),
            sandbox_allow_commands=d.get("sandbox_allow_commands", []),
            skill_capabilities=d.get("skill_capabilities", []),
        )


# ---------------------------------------------------------------------------
# Event log entry
# ---------------------------------------------------------------------------

@dataclass
class AgentEvent:
    timestamp: float
    kind: str   # message | tool_call | tool_result | error | delegate | status | approval
    data: dict

    def to_dict(self) -> dict:
        return {"timestamp": self.timestamp, "kind": self.kind, "data": self.data}


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

@dataclass
class Agent:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    name: str = "Agent"
    role: str = "general"
    model: str = ""
    provider: str = ""
    # ── Learning model: cheap/local LLM used for self-growth tasks ──
    # If empty, falls back to the agent's main provider/model.
    learning_provider: str = ""
    learning_model: str = ""
    # ── Multimodal model: vision/audio-capable LLM used when the input
    #    contains image or audio parts. Falls back to primary when empty. ──
    multimodal_provider: str = ""
    multimodal_model: str = ""
    # Whether the multimodal model supports tool calling.
    # False (default): tools disabled during vision calls (e.g. llama3.2-vision)
    # True: tools kept enabled (e.g. gpt-4o, claude-3.5-sonnet)
    multimodal_supports_tools: bool = False
    # ── Coding model: code-optimized LLM for tool-calling & code generation ──
    # If empty, falls back to the agent's main provider/model.
    coding_provider: str = ""
    coding_model: str = ""
    # ── Extra LLM slots (N-labeled models) ──
    # 每一项: {"label": "code_review", "provider": "openai", "model": "gpt-4o",
    #          "purpose": "code_review", "note": ""}
    # task 运行时可以通过 ChatTask.llm_label 指定走哪个 slot；
    # 没指定就走默认的 provider/model（或 auto_route 的启发式）。
    extra_llms: list[dict] = field(default_factory=list)

    # ── 方案乙(b): 启发式自动路由 ──
    # 按输入类型自动挑 extra_llms 里的某个 label。结构：
    #   {
    #     "enabled": true,
    #     "default":    "",              # 日常交流，空=用 agent.provider/model
    #     "complex":    "big_model",     # 长/复杂任务 → big_model slot
    #     "multimodal": "vision_model",  # 图片/音频输入 → vision_model slot
    #     "coding":     "code_model",  # 工具调用/代码生成 → code_model slot
    #     "complex_threshold_chars": 2000,  # prompt 超过多少字算复杂
    #   }
    # 所有字段都是可选的。没有 extra_llms 或没命中规则时回退到默认 LLM。
    # task 显式指定 llm_label 的优先级仍然高于 auto_route。
    auto_route: dict = field(default_factory=dict)
    working_dir: str = ""
    system_prompt: str = ""
    profile: AgentProfile = field(default_factory=AgentProfile)
    status: AgentStatus = AgentStatus.IDLE
    agent_phase: AgentPhase = AgentPhase.IDLE  # State machine phase for task continuity
    node_id: str = "local"
    shared_workspace: str = ""  # Shared project workspace directory (if part of a project)
    project_id: str = ""  # Project ID if agent belongs to a project
    project_name: str = ""  # Project name for prompt context
    parent_id: str = ""  # If set, this is a sub-agent (hidden from UI by default)
    priority_level: int = 3  # 1=CXO (highest), 2=PM, 3=Team Member (default)
    role_title: str = ""  # e.g. "CXO", "PM", "Developer", etc.
    authorized_workspaces: list[str] = field(default_factory=list)  # List of agent IDs whose workspaces this agent can access
    soul_md: str = ""  # SOUL.md personality/persona in markdown
    robot_avatar: str = ""  # Robot avatar ID e.g. "robot_ceo"
    messages: list[dict] = field(default_factory=list)
    events: list[AgentEvent] = field(default_factory=list)
    tasks: list[AgentTask] = field(default_factory=list)
    channel_ids: list[str] = field(default_factory=list)  # bound channel IDs
    granted_skills: list[str] = field(default_factory=list)  # Skill IDs granted to this agent
    created_at: float = field(default_factory=time.time)
    # --- src integration ---
    cost_tracker: CostTracker = field(default_factory=CostTracker)
    history_log: HistoryLog = field(default_factory=HistoryLog)
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    # --- src memory engine (Claude Code architecture) ---
    transcript: TranscriptStore = field(default_factory=TranscriptStore)
    _query_engine: QueryEnginePort | None = field(default=None, repr=False)
    _execution_registry: ExecutionRegistry | None = field(default=None, repr=False)
    _port_runtime: PortRuntime | None = field(default=None, repr=False)
    _tool_pool: ToolPool | None = field(default=None, repr=False)
    _permission_ctx: ToolPermissionContext | None = field(default=None, repr=False)
    turn_count: int = 0
    max_turns: int = 20
    max_budget_tokens: int = 200000
    # --- Enhancement module ---
    enhancer: AgentEnhancer | None = field(default=None, repr=False)
    # --- Three-layer memory ---
    _memory_manager: Any = field(default=None, repr=False)
    _memory_consolidator: Any = field(default=None, repr=False)
    _memory_turn_counter: int = 0  # 累计轮次，用于 L1→L2 压缩判断
    # --- Active Thinking engine ---
    active_thinking: Any = field(default=None, repr=False)
    # --- Self-Improvement engine (experience library) ---
    self_improvement: Any = field(default=None, repr=False)
    # --- Growth Tracker (养成量化) ---
    growth_tracker: Any = field(default=None, repr=False)
    # --- Execution Analyzer (自动分析) ---
    _execution_analyzer: ExecutionAnalyzer | None = field(default=None, repr=False)
    # --- Role Growth Path (角色成长路径) ---
    growth_path: RoleGrowthPath | None = field(default=None, repr=False)
    # --- Skill System (技能绑定) ---
    bound_prompt_packs: list[str] = field(default_factory=list)
    _active_skill_ids: list[str] = field(default_factory=list, repr=False)
    _chat_start_time: float = field(default=0.0, repr=False)
    # --- Execution Plans (real-time task decomposition) ---
    execution_plans: list[ExecutionPlan] = field(default_factory=list)
    _current_plan: ExecutionPlan | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last_save_time: float = field(default_factory=time.time, repr=False)  # For auto-save
    # --- Context compression state ---
    _previous_compression_summary: str = ""  # Iterative summary from previous compression
    _compression_cooldown: float = 0.0  # Cooldown until next LLM summarization attempt
    # --- Sub-Agent delegation (Hermes-style depth tracking & parallel execution) ---
    _delegate_depth: int = field(default=0, repr=False)  # 0 = top-level agent
    _max_delegate_depth: int = field(default=5, repr=False)  # configurable max depth
    _active_children: list[tuple[str, Any]] = field(default_factory=list, repr=False)  # List of (agent_id, Agent) tuples
    # ── Evolution goals: measurable targets for self-improvement ──
    # Each: {"id": "...", "description": "PPTX quality reaches professional level",
    #        "target_score": 90, "current_score": 0, "reference": "optional reference text or URL"}
    evolution_goals: list[dict] = field(default_factory=list)
    _active_children_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)  # Thread-safe access
    _cancellation_event: threading.Event = field(default_factory=threading.Event, repr=False)  # Signal children to stop
    # --- System prompt caching (avoid unnecessary rebuilds) ---
    _cached_static_prompt: str = field(default="", repr=False)
    _static_prompt_hash: str = field(default="", repr=False)  # Hash of inputs that affect static prompt
    _cached_git_context: str = field(default="", repr=False)
    _git_context_ts: float = field(default=0.0, repr=False)  # Timestamp of last git context fetch
    _GIT_CONTEXT_COOLDOWN: float = field(default=60.0, repr=False)  # seconds between git context refreshes
    _dynamic_context_tag: str = field(default="__DYNAMIC_CONTEXT__", repr=False, init=False)
    # --- Per-task LLM routing: while a task is executing, its provider/model
    #     (if set) override the agent's default for any LLM call. ---
    _current_task: AgentTask | None = field(default=None, repr=False)
    # --- Self-growth scheduling ---
    _last_growth_tick: float = field(default=0.0, repr=False)
    # --- Credential vault: runtime-only, never serialized or sent to LLM ---
    # Maps placeholder key (e.g. "CRED_abc123") → real credential value.
    # Used by request_web_login to keep passwords out of LLM context.
    _credential_vault: dict = field(default_factory=dict, repr=False)

    # ---- persistence serialisation ----

    def to_persist_dict(self) -> dict:
        """Serialise agent config + memory for disk persistence."""
        return {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "model": self.model,
            "provider": self.provider,
            "learning_provider": self.learning_provider,
            "learning_model": self.learning_model,
            "multimodal_provider": self.multimodal_provider,
            "multimodal_model": self.multimodal_model,
            "multimodal_supports_tools": self.multimodal_supports_tools,
            "coding_provider": self.coding_provider,
            "coding_model": self.coding_model,
            "extra_llms": list(self.extra_llms),
            "auto_route": dict(self.auto_route or {}),
            "working_dir": self.working_dir,
            "agent_phase": self.agent_phase.value,
            "system_prompt": self.system_prompt,
            "profile": self.profile.to_dict(),
            "node_id": self.node_id,
            "shared_workspace": self.shared_workspace,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "parent_id": self.parent_id,
            "priority_level": self.priority_level,
            "role_title": self.role_title,
            "authorized_workspaces": self.authorized_workspaces,
            "soul_md": self.soul_md,
            "robot_avatar": self.robot_avatar,
            "channel_ids": self.channel_ids,
            "granted_skills": list(self.granted_skills),
            "created_at": self.created_at,
            # --- src integration: persist memory ---
            "session_id": self.session_id,
            "messages": self.messages[-200:],  # last 200 messages for memory
            # Chat UI events (user/assistant bubbles, tool calls, approvals).
            # The UI replays these on load so the conversation history is
            # preserved across app restarts. Keep last 500 to bound file size.
            "events": [e.to_dict() for e in self.events[-500:]],
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "cost_events": self.cost_tracker.events[-200:],
            "cost_total_units": self.cost_tracker.total_units,
            "history_events": [
                {"title": e.title, "detail": e.detail}
                for e in self.history_log.events[-100:]
            ],
            # --- src memory engine persistence ---
            "transcript_entries": list(self.transcript.replay()),
            "turn_count": self.turn_count,
            "max_turns": self.max_turns,
            "max_budget_tokens": self.max_budget_tokens,
            # --- Enhancement module persistence ---
            "enhancer": self.enhancer.to_dict() if self.enhancer else None,
            # --- Active Thinking persistence ---
            "active_thinking": self.active_thinking.to_dict() if self.active_thinking else None,
            # --- Self-Improvement persistence ---
            "self_improvement": self.self_improvement.to_dict() if self.self_improvement else None,
            # --- Skill system persistence ---
            "bound_prompt_packs": self.bound_prompt_packs,
            # --- Role Growth Path persistence ---
            "growth_path": self.growth_path.to_dict() if self.growth_path else None,
            # --- Execution analyzer persistence ---
            "execution_analyzer": self._execution_analyzer.to_dict() if self._execution_analyzer else None,
            # --- Execution plans persistence (keep last 20) ---
            "execution_plans": [p.to_dict() for p in self.execution_plans[-20:]],
            "evolution_goals": self.evolution_goals,
        }

    @staticmethod
    def from_persist_dict(d: dict) -> "Agent":
        """Restore agent from persisted dict (including memory)."""
        profile = AgentProfile.from_dict(d.get("profile", {}))
        # Restore cost tracker
        ct = CostTracker()
        ct.total_units = d.get("cost_total_units", 0)
        ct.events = d.get("cost_events", [])
        # Restore history log
        hl = HistoryLog()
        for he in d.get("history_events", []):
            hl.add(he.get("title", ""), he.get("detail", ""))
        agent = Agent(
            id=d.get("id", uuid.uuid4().hex[:12]),
            name=d.get("name", "Agent"),
            role=d.get("role", "general"),
            model=d.get("model", ""),
            provider=d.get("provider", ""),
            learning_provider=d.get("learning_provider", "") or "",
            learning_model=d.get("learning_model", "") or "",
            multimodal_provider=d.get("multimodal_provider", "") or "",
            multimodal_model=d.get("multimodal_model", "") or "",
            multimodal_supports_tools=bool(d.get("multimodal_supports_tools", False)),
            coding_provider=d.get("coding_provider", "") or "",
            coding_model=d.get("coding_model", "") or "",
            extra_llms=list(d.get("extra_llms", []) or []),
            auto_route=dict(d.get("auto_route", {}) or {}),
            working_dir=d.get("working_dir", ""),
            agent_phase=AgentPhase(d.get("agent_phase", "idle")),
            system_prompt=d.get("system_prompt", ""),
            profile=profile,
            node_id=d.get("node_id", "local"),
            shared_workspace=d.get("shared_workspace", ""),
            project_id=d.get("project_id", ""),
            project_name=d.get("project_name", ""),
            parent_id=d.get("parent_id", ""),
            priority_level=d.get("priority_level", 3),
            role_title=d.get("role_title", ""),
            authorized_workspaces=d.get("authorized_workspaces", []),
            soul_md=d.get("soul_md", ""),
            robot_avatar=d.get("robot_avatar", ""),
            channel_ids=d.get("channel_ids", []),
            granted_skills=list(d.get("granted_skills", []) or []),
            evolution_goals=list(d.get("evolution_goals", []) or []),
            created_at=d.get("created_at", time.time()),
            # --- src integration: restore memory ---
            cost_tracker=ct,
            history_log=hl,
            session_id=d.get("session_id", uuid.uuid4().hex),
            messages=d.get("messages", []),
            total_input_tokens=d.get("total_input_tokens", 0),
            total_output_tokens=d.get("total_output_tokens", 0),
            # --- src memory engine restore ---
            turn_count=d.get("turn_count", 0),
            max_turns=d.get("max_turns", 20),
            max_budget_tokens=d.get("max_budget_tokens", 200000),
        )
        # Restore transcript entries
        for entry in d.get("transcript_entries", []):
            agent.transcript.append(entry)
        # Restore chat events so the UI can show history after a restart
        for ed in d.get("events", []):
            try:
                agent.events.append(AgentEvent(
                    timestamp=ed.get("timestamp", 0.0),
                    kind=ed.get("kind", "message"),
                    data=ed.get("data", {}) or {},
                ))
            except Exception as e:
                logger.debug("Failed to restore event: %s", e)
        # Restore enhancement module
        if d.get("enhancer"):
            agent.enhancer = AgentEnhancer.from_dict(d["enhancer"])
        # Restore active thinking engine
        if d.get("active_thinking"):
            try:
                from .active_thinking import ActiveThinkingEngine
                agent.active_thinking = ActiveThinkingEngine.from_dict(
                    d["active_thinking"], agent=agent)
            except Exception as e:
                logger.debug("Failed to restore active_thinking: %s", e)
        # Restore self-improvement engine
        if d.get("self_improvement"):
            try:
                from .experience_library import SelfImprovementEngine
                agent.self_improvement = SelfImprovementEngine.from_dict(
                    d["self_improvement"], agent=agent)
            except Exception as e:
                logger.debug("Failed to restore self_improvement: %s", e)
        # Restore skill bindings
        agent.bound_prompt_packs = d.get("bound_prompt_packs", d.get("bound_skill_ids", []))
        # Restore role growth path
        if d.get("growth_path"):
            agent.growth_path = RoleGrowthPath.from_dict(d["growth_path"])
        # Restore execution analyzer
        if d.get("execution_analyzer"):
            agent._execution_analyzer = ExecutionAnalyzer.from_dict(d["execution_analyzer"])
        # Restore execution plans
        for pd in d.get("execution_plans", []):
            agent.execution_plans.append(ExecutionPlan.from_dict(pd))
        return agent

    # ---- API serialisation ----

    def to_dict(self, include_messages: bool = False,
                include_events: bool = False) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "model": self.model or llm.get_config().get("model", ""),
            "provider": self.provider or llm.get_config().get("provider", ""),
            # --- Additional LLM slots for 方案甲(learning+multimodal) + 乙(extra_llms) ---
            "learning_provider": self.learning_provider,
            "learning_model": self.learning_model,
            "multimodal_provider": self.multimodal_provider,
            "multimodal_model": self.multimodal_model,
            "multimodal_supports_tools": self.multimodal_supports_tools,
            "coding_provider": self.coding_provider,
            "coding_model": self.coding_model,
            "extra_llms": list(self.extra_llms),
            "auto_route": dict(self.auto_route or {}),
            "working_dir": self.working_dir or str(self._effective_working_dir()),
            "status": self.status.value,
            "node_id": self.node_id,
            "parent_id": self.parent_id,
            "priority_level": self.priority_level,
            "role_title": self.role_title,
            "robot_avatar": self.robot_avatar,
            "created_at": self.created_at,
            "message_count": len(self.messages),
            "event_count": len(self.events),
            "task_count": len(self.tasks),
            "tasks_summary": {
                "todo": sum(1 for t in self.tasks if t.status == TaskStatus.TODO),
                "in_progress": sum(1 for t in self.tasks if t.status == TaskStatus.IN_PROGRESS),
                "done": sum(1 for t in self.tasks if t.status == TaskStatus.DONE),
                "blocked": sum(1 for t in self.tasks if t.status == TaskStatus.BLOCKED),
            },
            "agent_class": self.profile.agent_class,
            "memory_mode": self.profile.memory_mode,
            "profile": self.profile.to_dict(),
            "channel_ids": self.channel_ids,
            # --- src integration: cost & usage ---
            "cost_summary": {
                "total_units": self.cost_tracker.total_units,
                "input_tokens": self.total_input_tokens,
                "output_tokens": self.total_output_tokens,
            },
            "session_id": self.session_id,
            # --- src memory engine stats ---
            "engine": {
                "turn_count": self.turn_count,
                "max_turns": self.max_turns,
                "max_budget_tokens": self.max_budget_tokens,
                "transcript_size": len(self.transcript.entries),
                "has_query_engine": self._query_engine is not None,
                "has_execution_registry": self._execution_registry is not None,
                "has_tool_pool": self._tool_pool is not None,
            },
            # --- Enhancement module ---
            "enhancement": self.enhancer.get_stats() if self.enhancer else None,
            # --- Active Thinking ---
            "active_thinking": self.active_thinking.get_stats() if self.active_thinking else None,
            # --- Self-Improvement ---
            "self_improvement": self.self_improvement.get_stats() if self.self_improvement else None,
            # --- Execution plans (current + recent) ---
            "current_plan": self._current_plan.to_dict() if self._current_plan else None,
            "plan_count": len(self.execution_plans),
            # --- Skill System (技能绑定) ---
            # granted_skills: authoritative skill grants from the skill
            #   registry (runtime skills like take_screenshot). This is
            #   what the Capability panel's SKILLS column should reflect.
            # bound_prompt_packs: prompt-enhancer packs — a separate
            #   class of capability, surfaced in the detail dialog.
            "granted_skills": list(self.granted_skills),
            "bound_prompt_packs": self.bound_prompt_packs,
            "active_skill_count": len(self._active_skill_ids),
            # --- Role Growth Path (角色成长路径) ---
            "growth_path": self.growth_path.get_summary() if self.growth_path else None,
            # --- Execution Analyzer (最近分析) ---
            "recent_analyses": (
                [a.to_dict() for a in self._execution_analyzer.get_recent_analyses(5)]
                if self._execution_analyzer else []
            ),
            # --- Workspace ---
            "shared_workspace": self.shared_workspace,
            "project_id": self.project_id,
            "project_name": self.project_name,
            # --- Evolution goals ---
            "evolution_goals": self.evolution_goals,
        }
        # --- Authoritative capability view (live, not cached profile) ---
        #
        # These three fields are what the portal UI reads to render the
        # Capability panel (Skills / MCPs / Tools). They MUST reflect live
        # state, not whatever `self.profile.mcp_servers` happens to hold
        # from the last workspace layout regeneration — otherwise the
        # count stays at 0 until the agent next boots, which is exactly
        # the bug we're fixing.
        #
        # Invariant: the agent serializer consults the authoritative
        # source at serialization time.
        try:
            from .mcp.manager import get_mcp_manager as _gmm
            _mgr = _gmm()
            if _mgr is not None:
                _live_mcps = _mgr.get_agent_effective_mcps(
                    getattr(self, "node_id", "local") or "local", self.id
                ) or []
                d["mcp_servers"] = [
                    (m.to_dict() if hasattr(m, "to_dict") else m)
                    for m in _live_mcps
                ]
            else:
                d["mcp_servers"] = []
        except Exception as e:
            logger.warning("Failed to get mcp_servers: %s", e)
            d["mcp_servers"] = []
        # Tools (capability view): tools the agent has GAINED from its
        # MCP bindings. We deliberately do NOT count the ~180 built-in
        # tools (read/write/bash/git/web_fetch/…) here — every agent has
        # those, so including them would make the Capability panel
        # useless for telling agents apart.
        #
        # Authoritative source: ToolManifestCache. For each bound MCP,
        # look up its discovered tool list and emit one entry per tool.
        # If an MCP hasn't been probed yet, it contributes 0 here — the
        # count will fill in as the background preloader finishes.
        try:
            _mcp_tools: list[dict] = []
            if _mgr is not None and d.get("mcp_servers"):
                for _m in d["mcp_servers"]:
                    _mid = _m.get("id") if isinstance(_m, dict) else getattr(_m, "id", "")
                    if not _mid:
                        continue
                    try:
                        _entry = _mgr.get_tool_manifest(_mid)
                    except Exception as e:
                        logger.debug("Failed to get tool manifest for %s: %s", _mid, e)
                        _entry = None
                    if _entry is None or not _entry.tools:
                        continue
                    for _t in _entry.tools:
                        _tname = _t.get("name") if isinstance(_t, dict) else None
                        if not _tname:
                            continue
                        _mcp_tools.append({
                            "name": _tname,
                            "mcp_id": _mid,
                            "source_hint": f"mcp:{_mid}",
                        })
            d["tools"] = _mcp_tools
        except Exception as e:
            logger.warning("Failed to build mcp_tools list: %s", e)
            d["tools"] = []
        # Full runtime tool pool size (builtin + mcp + skill) — exposed
        # separately so the UI / debug views can still see it without
        # confusing it with the Capability panel's "MCP tools" count.
        try:
            if self._tool_pool is not None:
                d["tool_pool_size"] = len(self._tool_pool.tools)
            else:
                d["tool_pool_size"] = None  # not yet assembled
        except Exception as e:
            logger.debug("Failed to get tool_pool_size: %s", e)
            d["tool_pool_size"] = None
        if include_messages:
            d["messages"] = self.messages
        if include_events:
            d["events"] = [e.to_dict() for e in self.events[-200:]]
        return d

    # ---- Role Growth Path helpers ----

    def ensure_growth_path(self) -> RoleGrowthPath | None:
        """Ensure agent has a growth path. Auto-creates from template if role matches."""
        if self.growth_path:
            return self.growth_path
        import copy
        template = ROLE_GROWTH_PATHS.get(self.role)
        if template:
            self.growth_path = copy.deepcopy(template)
            logger.info("Agent %s: auto-created growth path for role=%s", self.id[:8], self.role)
        return self.growth_path

    def get_next_learning_objective(self):
        """Get the next uncompleted learning objective from current growth stage."""
        gp = self.ensure_growth_path()
        if not gp:
            return None
        objs = gp.get_next_objectives(limit=1)
        return objs[0] if objs else None

    # ---- src integration: session persistence ----

    def save_memory(self, directory: Path | None = None) -> str:
        """Persist conversation memory using src.SessionStore."""
        stored = StoredSession(
            session_id=self.session_id,
            messages=tuple(
                json.dumps(m, ensure_ascii=False) for m in self.messages[-100:]
            ),
            input_tokens=self.total_input_tokens,
            output_tokens=self.total_output_tokens,
        )
        path = save_session(stored, directory)
        self.history_log.add("session_saved", str(path))
        return str(path)

    def load_memory(self, directory: Path | None = None) -> bool:
        """Restore conversation memory from src.SessionStore."""
        try:
            stored = load_session(self.session_id, directory)
            restored = []
            for raw in stored.messages:
                try:
                    restored.append(json.loads(raw))
                except (json.JSONDecodeError, TypeError):
                    restored.append({"role": "user", "content": str(raw)})
            self.messages = restored
            self.total_input_tokens = stored.input_tokens
            self.total_output_tokens = stored.output_tokens
            self.history_log.add("session_loaded",
                                 f"msgs={len(restored)} tokens_in={stored.input_tokens}")
            return True
        except Exception as e:
            logger.warning("Failed to load memory: %s", e)
            return False

    def _auto_save_check(self):
        """Auto-save conversation periodically (every 60 s during active chat).

        Flushes both the session memory AND the agent events/messages to
        disk so chat history survives a server restart.
        """
        # Skip during scheduled execution — messages are temporarily isolated
        # and saving here would persist the wrong (scheduled) context.
        if getattr(self, '_scheduled_context', False):
            return
        try:
            current_time = time.time()
            elapsed = current_time - self._last_save_time
            if elapsed > 60:  # flush events at least once per minute
                self.save_memory()
                self._last_save_time = current_time
                # Also flush agent events to disk via the hub so that
                # chat history (events) survives a crash/restart.
                try:
                    from .hub import get_hub as _get_hub
                    _hub = _get_hub()
                    if _hub is not None:
                        _hub._save_agent_workspace(self)
                except Exception:
                    pass
        except Exception as e:
            logger.debug("Auto-save failed: %s", e)

    def get_memory_usage_stats(self) -> dict:
        """
        返回 agent 的记忆使用比例统计。
        - last_mem_chars: 最近一次注入的记忆字符数
        - last_budget: 动态上下文预算上限（字符）
        - last_total_chars: 最近一次动态上下文实际总字符数
        - last_ratio: mem_chars / budget （0~1）
        - last_in_dynamic_ratio: mem_chars / total_chars （0~1，记忆在动态上下文里占比）
        - ema_ratio: 近期 ratio 的指数移动平均
        - samples: 累计样本数
        """
        stats = getattr(self, "_memory_usage_stats", None) or {}
        mem_chars = stats.get("last_mem_chars", 0)
        budget = stats.get("last_budget", 0)
        total = stats.get("last_total_chars", 0)
        in_dyn = (mem_chars / total) if total > 0 else 0.0
        hits_counts = getattr(self, "_memory_hit_counts", None) or {"hits": 0, "misses": 0}
        h = hits_counts.get("hits", 0)
        m = hits_counts.get("misses", 0)
        hit_rate = h / (h + m) if (h + m) > 0 else 0.0
        return {
            "last_mem_chars": mem_chars,
            "last_total_chars": total,
            "last_budget": budget,
            "last_ratio": stats.get("last_ratio", 0.0),
            "last_in_dynamic_ratio": in_dyn,
            "ema_ratio": stats.get("ema_ratio", 0.0),
            "samples": stats.get("samples", 0),
            "last_query_ts": stats.get("last_query_ts", 0.0),
            "hits": h,
            "misses": m,
            "hit_rate": hit_rate,
        }

    def get_token_stats(self) -> dict:
        """累计 LLM token 使用（由 llm._log_token_usage 写入）。"""
        stats = getattr(self, "_token_stats", None) or {
            "in": 0, "out": 0, "calls": 0,
        }
        return {
            "prompt_tokens": stats.get("in", 0),
            "completion_tokens": stats.get("out", 0),
            "total_tokens": stats.get("in", 0) + stats.get("out", 0),
            "calls": stats.get("calls", 0),
        }

    def get_cost_summary(self) -> dict:
        """Return cost/usage summary from CostTracker."""
        return {
            "total_units": self.cost_tracker.total_units,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "event_count": len(self.cost_tracker.events),
            "recent_events": self.cost_tracker.events[-20:],
        }

    def get_history_markdown(self) -> str:
        """Return activity history as markdown from HistoryLog."""
        return self.history_log.as_markdown()

    def get_workspace_context(self) -> str:
        """Build workspace context using src.PortContext."""
        try:
            wd = self._effective_working_dir()
            ctx = build_port_context(wd)
            return render_context(ctx)
        except Exception as e:
            logger.debug("Failed to build workspace context: %s", e)
            return ""

    def get_tool_surface(self, query: str = "", limit: int = 20) -> str:
        """Query the mirrored tool surface from src.tools."""
        return render_tool_index(limit=limit, query=query or None)

    # ---- src memory engine: lazy initialization (Claude Code architecture) ----

    def _ensure_permission_ctx(self) -> ToolPermissionContext:
        """Build permission context from agent profile."""
        if self._permission_ctx is None:
            deny_names = list(self.profile.denied_tools) if self.profile.denied_tools else []
            self._permission_ctx = ToolPermissionContext.from_iterables(
                deny_names=deny_names or None,
                deny_prefixes=None,
            )
        return self._permission_ctx

    def _ensure_execution_registry(self) -> ExecutionRegistry:
        """Lazy-init the ExecutionRegistry (mirrored commands + tools)."""
        if self._execution_registry is None:
            self._execution_registry = build_execution_registry()
            self.history_log.add("engine_init", "ExecutionRegistry built")
        return self._execution_registry

    def _ensure_tool_pool(self) -> ToolPool:
        """Lazy-init the ToolPool with permission filtering."""
        if self._tool_pool is None:
            perm = self._ensure_permission_ctx()
            self._tool_pool = assemble_tool_pool(
                simple_mode=False,
                include_mcp=True,
                permission_context=perm,
            )
            self.history_log.add("engine_init",
                                 f"ToolPool assembled: {len(self._tool_pool.tools)} tools")
        return self._tool_pool

    def _ensure_port_runtime(self) -> PortRuntime:
        """Lazy-init the PortRuntime for prompt routing."""
        if self._port_runtime is None:
            self._port_runtime = PortRuntime()
            self.history_log.add("engine_init", "PortRuntime initialized")
        return self._port_runtime

    def _ensure_query_engine(self) -> QueryEnginePort:
        """Lazy-init QueryEnginePort for session/turn management."""
        if self._query_engine is None:
            config = QueryEngineConfig(
                max_turns=self.max_turns,
                max_budget_tokens=self.max_budget_tokens,
                compact_after_turns=max(12, self.profile.max_context_messages // 3),
            )
            self._query_engine = QueryEnginePort.from_workspace()
            self._query_engine.config = config
            self._query_engine.session_id = self.session_id
            self.history_log.add("engine_init",
                                 f"QueryEnginePort created session={self.session_id[:8]}")
        return self._query_engine

    def route_prompt(self, prompt: str, limit: int = 5) -> list[RoutedMatch]:
        """Route a prompt through PortRuntime to find matching tools/commands."""
        runtime = self._ensure_port_runtime()
        matches = runtime.route_prompt(prompt, limit=limit)
        if matches:
            self.history_log.add("routing",
                                 f"matched {len(matches)} items: "
                                 + ", ".join(f"{m.kind}:{m.name}" for m in matches[:3]))
        return matches

    def submit_to_engine(self, prompt: str) -> TurnResult:
        """Submit a message through the QueryEngine for turn management."""
        engine = self._ensure_query_engine()
        matches = self.route_prompt(prompt)
        matched_commands = tuple(m.name for m in matches if m.kind == "command")
        matched_tools = tuple(m.name for m in matches if m.kind == "tool")

        # Check permission denials
        denied = []
        perm = self._ensure_permission_ctx()
        for m in matches:
            if m.kind == "tool" and perm.blocks(m.name):
                denied.append(PermissionDenial(tool_name=m.name,
                                                reason="Blocked by agent permission"))

        result = engine.submit_message(
            prompt,
            matched_commands=matched_commands,
            matched_tools=matched_tools,
            denied_tools=tuple(denied),
        )
        self.turn_count += 1
        # Track in transcript
        self.transcript.append(prompt)
        # Track usage
        self.total_input_tokens += result.usage.input_tokens
        self.total_output_tokens += result.usage.output_tokens
        apply_cost_hook(self.cost_tracker,
                        f"turn:{self.turn_count}",
                        result.usage.input_tokens + result.usage.output_tokens)
        self.history_log.add("turn_complete",
                             f"turn={self.turn_count} in={result.usage.input_tokens} "
                             f"out={result.usage.output_tokens} stop={result.stop_reason}")
        return result

    def execute_src_tool(self, tool_name: str, payload: str = "") -> ToolExecution:
        """Execute a tool through the src ExecutionRegistry."""
        registry = self._ensure_execution_registry()
        mirrored = registry.tool(tool_name)
        if mirrored:
            result_msg = mirrored.execute(payload)
            self.history_log.add("src_tool_exec", f"{tool_name}: {result_msg[:100]}")
            return ToolExecution(name=tool_name, source_hint=mirrored.source_hint,
                                payload=payload, handled=True, message=result_msg)
        # Fall back to direct src execute
        result = src_execute_tool(tool_name, payload)
        self.history_log.add("src_tool_exec",
                             f"{tool_name}: handled={result.handled} {result.message[:100]}")
        return result

    def execute_src_command(self, command_name: str, prompt: str = "") -> CommandExecution:
        """Execute a command through the src ExecutionRegistry."""
        registry = self._ensure_execution_registry()
        mirrored = registry.command(command_name)
        if mirrored:
            result_msg = mirrored.execute(prompt)
            self.history_log.add("src_cmd_exec", f"{command_name}: {result_msg[:100]}")
            return CommandExecution(name=command_name, source_hint=mirrored.source_hint,
                                   prompt=prompt, handled=True, message=result_msg)
        result = src_execute_command(command_name, prompt)
        self.history_log.add("src_cmd_exec",
                             f"{command_name}: handled={result.handled} {result.message[:100]}")
        return result

    def run_turn_loop(self, prompt: str, max_turns: int = 3) -> list[TurnResult]:
        """Execute multiple turns through PortRuntime (Claude Code style turn loop)."""
        runtime = self._ensure_port_runtime()
        results = runtime.run_turn_loop(
            prompt, limit=5, max_turns=max_turns,
            structured_output=False,
        )
        for r in results:
            self.turn_count += 1
            self.total_input_tokens += r.usage.input_tokens
            self.total_output_tokens += r.usage.output_tokens
            self.transcript.append(r.prompt)
            apply_cost_hook(self.cost_tracker,
                            f"loop_turn:{self.turn_count}",
                            r.usage.input_tokens + r.usage.output_tokens)
        self.history_log.add("turn_loop",
                             f"turns={len(results)} total_turn_count={self.turn_count}")
        return results

    def compact_memory(self):
        """Compact transcript and query engine messages (prevent unbounded growth)."""
        keep = max(10, self.profile.max_context_messages // 3)
        self.transcript.compact(keep_last=keep)
        engine = self._ensure_query_engine()
        engine.compact_messages_if_needed()
        self.history_log.add("memory_compact", f"kept_last={keep}")

    def replay_transcript(self) -> tuple[str, ...]:
        """Replay all user messages from transcript store."""
        return self.transcript.replay()

    def persist_engine_session(self, directory: Path | None = None) -> str:
        """Persist the full query engine session to disk."""
        engine = self._ensure_query_engine()
        # Sync engine state with agent state
        engine.total_usage = UsageSummary(
            input_tokens=self.total_input_tokens,
            output_tokens=self.total_output_tokens,
        )
        path = engine.persist_session()
        self.history_log.add("engine_session_persisted", path)
        return path

    def restore_engine_session(self) -> bool:
        """Restore query engine session from disk."""
        try:
            self._query_engine = QueryEnginePort.from_saved_session(self.session_id)
            # Sync back to agent
            self.total_input_tokens = self._query_engine.total_usage.input_tokens
            self.total_output_tokens = self._query_engine.total_usage.output_tokens
            # Restore transcript
            for msg in self._query_engine.mutable_messages:
                if msg not in self.transcript.entries:
                    self.transcript.append(msg)
            self.history_log.add("engine_session_restored",
                                 f"msgs={len(self._query_engine.mutable_messages)}")
            return True
        except Exception as e:
            self.history_log.add("engine_session_restore_failed", str(e))
            return False

    def get_engine_summary(self) -> str:
        """Get a summary of the query engine state."""
        engine = self._ensure_query_engine()
        return engine.render_summary()

    def get_routed_tools_for_prompt(self, prompt: str) -> dict:
        """Route a prompt and return structured match info."""
        matches = self.route_prompt(prompt)
        return {
            "matches": [
                {"kind": m.kind, "name": m.name,
                 "source_hint": m.source_hint, "score": m.score}
                for m in matches
            ],
            "commands": [m.name for m in matches if m.kind == "command"],
            "tools": [m.name for m in matches if m.kind == "tool"],
            "total": len(matches),
        }

    def get_tool_pool_info(self) -> dict:
        """Return info about the assembled tool pool."""
        pool = self._ensure_tool_pool()
        return {
            "tool_count": len(pool.tools),
            "simple_mode": pool.simple_mode,
            "include_mcp": pool.include_mcp,
            "tools": [
                {"name": t.name, "responsibility": t.responsibility[:80],
                 "source_hint": t.source_hint}
                for t in pool.tools[:30]
            ],
        }

    def _log(self, kind: str, data: dict):
        # Skip event logging during scheduled task execution — scheduled
        # prompts/replies must NOT appear in the agent's chat UI.
        if getattr(self, '_scheduled_context', False):
            return
        self.events.append(AgentEvent(time.time(), kind, data))
        if len(self.events) > 2000:
            self.events = self.events[-1500:]

    # ---- system prompt ----

    def _get_git_context(self) -> str:
        """Auto-inject git context: branch, status, recent commits."""
        import subprocess as _sp
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
    _STATIC_PROMPT_BUILD_VERSION = "v3-global_system_prompt"

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
            # Scope filtering: "all" applies to every agent,
            # "roles" only applies to agents whose role is in the list
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
        import hashlib
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
        return hashlib.md5("|".join(parts).encode()).hexdigest()

    def _build_static_system_prompt(self) -> str:
        """Build the STATIC portion of the system prompt.

        This includes: identity, personality, tools description, language,
        custom instructions, project context files (TUDOU_CLAW.md etc.), and
        model-specific guidance.  These change rarely (only on config edits).

        Cached via _cached_static_prompt / _static_prompt_hash.
        """
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

        # Project context files (change rarely — only when files are edited)
        for name in ("TUDOU_CLAW.md", "CLAW.md", "README.md"):
            ctx_file = wd / name
            if ctx_file.exists():
                try:
                    content = ctx_file.read_text(encoding="utf-8", errors="replace")[:4000]
                    parts.append(
                        f"\n<project_context file=\"{name}\">\n{content}\n</project_context>"
                    )
                except OSError:
                    pass

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
        context_limit = self._get_context_limit()
        static_len = len(self._cached_static_prompt) if self._cached_static_prompt else 0
        # Reserve at least 50% of context for conversation; static prompt also counted
        # Rough: 1 token ≈ 3 chars for CJK, 4 chars for EN → use 3 as conservative
        static_tokens = static_len // 3
        max_dynamic_tokens = max(200, (context_limit - static_tokens) * 3 // 10)  # 30% of remaining
        max_dynamic_chars = max_dynamic_tokens * 3  # convert back to chars

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
            # === L1→L2: 压缩溢出消息 ===
            if self._memory_turn_counter >= mem_config.l2_compress_threshold:
                overflow = mm.get_overflow_messages(
                    self.messages, max_turns=mem_config.l1_max_turns,
                )
                if overflow:
                    # 构建一个简单的 LLM 调用函数
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
                    })
                self._memory_turn_counter = 0  # 重置计数

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
            line = f"{icon} {step.order + 1}. {step.title}"
            if step.result_summary:
                line += f" → {step.result_summary[:80]}"
            lines.append(line)

        return "\n".join(lines)

    def _build_checkpoint_context(self) -> str:
        """构建任务恢复上下文，注入到系统提示中。

        当 agent_phase 为 EXECUTING 或 PLANNING 时调用，
        让 Agent 知道之前做到哪了，避免重头开始。
        """
        mm = self._get_memory_manager()
        parts = []

        # 1. 活跃计划的进度
        plan_summary = self._format_active_plan_summary()
        if plan_summary:
            parts.append(plan_summary)

        # 2. 从 L3 获取最近的 action_done (已完成的操作)
        if mm:
            recent_done = mm.get_recent_facts(self.id, limit=10, category="action_done")
            if recent_done:
                parts.append("\n**最近完成的操作:**")
                for f in recent_done[:10]:
                    parts.append(f"- {f.content}")

            # 3. 获取待办事项
            plans = mm.get_recent_facts(self.id, limit=5, category="action_plan")
            if plans:
                parts.append("\n**待办事项:**")
                for f in plans[:5]:
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
            if self.agent_phase != AgentPhase.BLOCKED:
                self.agent_phase = AgentPhase.IDLE
            return

        plan = active_plans[-1]
        progress = plan.get_progress()

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
            from .. import llm
        except ImportError:
            try:
                from app import llm
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
        """Estimate total token count of current messages (rough: 1 token ≈ 4 chars for CJK, 4 chars for EN)."""
        total = 0
        for m in self.messages:
            content = _ensure_str_content(m.get("content"))
            if content:
                total += max(len(content) // 3, len(content.split()))
            # tool_calls in message also count
            tc = m.get("tool_calls", [])
            if tc:
                total += len(json.dumps(tc, ensure_ascii=False)) // 4
        return total

    def _get_context_limit(self) -> int:
        """Get the context window token limit based on model.

        Priority:
        1. Provider's configured context_length (if > 0)
        2. Model name heuristic
        3. Default 4096 (safe for local models like LM Studio)
        """
        # Check provider: explicit config or auto-detected from server
        try:
            reg = llm.get_registry()
            if self.provider:
                entry = reg.get(self.provider)
                if entry:
                    if entry.context_length > 0:
                        return entry.context_length
                    # Try auto-detect from the server API
                    detected = llm.detect_context_length(entry, model=model)
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

    def _resolve_effective_provider_model(self, user_message: Any = None) -> tuple[str, str]:
        """Re-resolve provider/model from registry before each LLM call.

        If the configured provider is empty, disabled, or removed, falls back
        to the global default. This ensures agents pick up live config changes
        (new API key, URL, etc.) without needing a restart or re-create.

        P2 #8: if `user_message` is provided and contains vision/audio parts,
        route to the multimodal provider/model when configured.
        """
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
                if self.multimodal_provider or self.multimodal_model:
                    mm_prov = self.multimodal_provider or prov
                    mm_mdl = self.multimodal_model or mdl
                    logger.info(
                        "Agent %s: multimodal input → routing to %s/%s",
                        self.id[:8], mm_prov, mm_mdl,
                    )
                    prov, mdl = mm_prov, mm_mdl
                else:
                    logger.warning(
                        "Agent %s: multimodal input detected but no "
                        "multimodal_provider/model configured — sending "
                        "images to default %s/%s (ensure it supports vision)",
                        self.id[:8], prov, mdl,
                    )
        except Exception as _mm_err:
            logger.debug("multimodal routing skipped: %s", _mm_err)
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
        import datetime
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

    def _execute_tool_with_policy(self, tool_name: str, arguments: dict,
                                   on_event: Any = None) -> str:
        """Execute a tool, checking policy. May block for approval."""
        # Resolve alias (e.g. "exec" → "bash") BEFORE permission check
        tool_name = tools._TOOL_ALIASES.get(tool_name, tool_name)

        # Substitute credential placeholders ({{CRED_xxx}}) with real values
        # so sensitive data stays out of LLM context but reaches the tool.
        arguments = self._substitute_credentials(arguments)

        # Inject agent context for tools that need RAG routing
        if tool_name in ("knowledge_lookup",):
            arguments = dict(arguments)
            arguments["_agent_profile"] = self.profile
            arguments["agent_id"] = self.id

        # Check agent-level denied tools
        if tool_name in self.profile.denied_tools:
            return f"DENIED: Tool '{tool_name}' is not permitted for this agent."

        # Check agent-level allowed tools (empty list = all allowed)
        if self.profile.allowed_tools and tool_name not in self.profile.allowed_tools:
            return f"DENIED: Tool '{tool_name}' is not in this agent's allowed list."

        # Scheduled / background task: skip approval (already authorized at creation)
        if getattr(self, '_scheduled_context', False):
            with self._sandbox_scope():
                result = tools.execute_tool(tool_name, arguments)
            return result

        # Agent-level exec_policy: 'full' = auto-approve all tools
        if self.profile.exec_policy == "full":
            with self._sandbox_scope():
                result = tools.execute_tool(tool_name, arguments)
            return result

        from .auth import get_auth
        auth = get_auth()
        policy = auth.tool_policy

        # Check if this agent auto-approves this tool
        if tool_name in self.profile.auto_approve_tools:
            with self._sandbox_scope():
                result = tools.execute_tool(tool_name, arguments)
            auth.audit("tool_executed", actor=self.name, target=tool_name,
                       detail=result[:200])
            return result

        decision, reason = policy.check_tool(
            tool_name, arguments,
            agent_id=self.id, agent_name=self.name,
            agent_priority=getattr(self.profile, 'priority', 3),
        )

        # MODERATE risk: if agent_approvable and this agent (or a superior)
        # has authority, auto-approve it
        if decision == "agent_approvable":
            agent_pri = getattr(self.profile, 'priority', 3)
            if policy.can_agent_approve(self.id, agent_pri, "moderate"):
                decision = "allow"
                reason = f"Agent-approved (priority={agent_pri})"
                auth.audit("tool_agent_approved", actor=self.name,
                           target=tool_name, detail=reason)
            else:
                # Escalate to human approval
                decision = "needs_approval"

        if decision == "deny":
            auth.audit("tool_denied", actor=self.name, target=tool_name,
                       detail=f"Auto-denied: {reason}", success=False)
            evt = AgentEvent(time.time(), "approval", {
                "tool": tool_name, "status": "denied", "reason": reason,
                "agent_name": self.name,
            })
            self._log(evt.kind, evt.data)
            if on_event:
                on_event(evt)
            return f"DENIED: {reason}. This operation is not allowed for security reasons."

        if decision == "needs_approval":
            self.status = AgentStatus.WAITING_APPROVAL

            # Create the PendingApproval FIRST so approval_id is available
            # for the SSE event (clients need it to call the approve API).
            approval = policy.request_approval(
                tool_name, arguments,
                agent_id=self.id, agent_name=self.name,
                reason=reason,
            )

            evt = AgentEvent(time.time(), "approval", {
                "tool": tool_name, "status": "pending", "reason": reason,
                "arguments": _truncate_dict(arguments),
                "agent_name": self.name,
                "approval_id": approval.approval_id,
            })
            self._log(evt.kind, evt.data)
            if on_event:
                on_event(evt)

            auth.audit("tool_approval_requested", actor=self.name,
                       target=tool_name,
                       detail=json.dumps(_truncate_dict(arguments),
                                         ensure_ascii=False)[:300])

            result_status = policy.wait_for_approval(approval)
            self.status = AgentStatus.BUSY

            if result_status != "approved":
                auth.audit("tool_denied", actor=self.name, target=tool_name,
                           detail=f"Human denied/expired: {approval.decided_by}",
                           success=False)
                evt = AgentEvent(time.time(), "approval", {
                    "tool": tool_name, "status": "denied",
                    "reason": f"{result_status} by {approval.decided_by or 'timeout'}",
                "agent_name": self.name,
                })
                self._log(evt.kind, evt.data)
                if on_event:
                    on_event(evt)
                return (f"DENIED: Tool execution was {result_status}. "
                        f"Decided by: {approval.decided_by or 'timeout'}. "
                        f"Please try an alternative approach.")

            auth.audit("tool_approved", actor=self.name, target=tool_name,
                       detail=f"Approved by {approval.decided_by}")
            evt = AgentEvent(time.time(), "approval", {
                "tool": tool_name, "status": "approved",
                "decided_by": approval.decided_by,
                "agent_name": self.name,
            })
            self._log(evt.kind, evt.data)
            if on_event:
                on_event(evt)

        # ── Middleware: PRE_TOOL (lint check, etc.) ──
        try:
            from .middleware import ensure_pipeline, MiddlewareContext, Stage
            pipe = ensure_pipeline()
            pre_ctx = MiddlewareContext(
                agent_id=self.id, agent_name=self.name,
                tool_name=tool_name, tool_arguments=arguments,
            )
            pre_result = pipe.run(Stage.PRE_TOOL, pre_ctx)
            if pre_result.short_circuited:
                return pre_result.value  # Lint check failed — return error to LLM
        except Exception as _mw_err:
            logger.debug("pre_tool middleware skipped: %s", _mw_err)

        with self._sandbox_scope():
            result = tools.execute_tool(tool_name, arguments)

        # ── Middleware: POST_TOOL (truncation, etc.) ──
        try:
            from .middleware import ensure_pipeline, MiddlewareContext, Stage
            pipe = ensure_pipeline()
            post_ctx = MiddlewareContext(
                agent_id=self.id, agent_name=self.name,
                tool_name=tool_name, tool_arguments=arguments,
                tool_result=result,
            )
            post_result = pipe.run(Stage.POST_TOOL, post_ctx)
            if post_ctx.tool_result != result:
                result = post_ctx.tool_result  # middleware modified the result
        except Exception as _mw_err:
            logger.debug("post_tool middleware skipped: %s", _mw_err)

        auth.audit("tool_executed", actor=self.name, target=tool_name,
                   detail=result[:200])
        return result

    def _sandbox_scope(self):
        """Install a sandbox policy rooted at this agent's working_dir."""
        from . import sandbox as _sandbox
        import os as _os
        # Use the agent's working_dir as jail root. Fall back to the per-agent
        # workspace directory if no working_dir is configured.
        root = self.working_dir
        if not root:
            from . import DEFAULT_DATA_DIR as _DEFAULT_DD
            root = _os.path.join(
                _os.environ.get("TUDOU_CLAW_DATA_DIR") or _DEFAULT_DD,
                "workspaces", self.id, "sandbox")
        # Honor per-agent sandbox mode if set on the profile, otherwise
        # use the global default from TUDOU_SANDBOX env var.
        mode = getattr(self.profile, "sandbox_mode", "") or ""
        allow_list = list(getattr(self.profile, "sandbox_allow_commands", []) or [])

        # Build allowed_dirs from authorized workspaces + shared workspace
        allowed_dirs = []
        if self.shared_workspace:
            allowed_dirs.append(self.shared_workspace)
        # Add workspaces of authorized agents
        from . import DEFAULT_DATA_DIR as _DEFAULT_DD2
        data_dir = _os.environ.get("TUDOU_CLAW_DATA_DIR") or _DEFAULT_DD2
        for other_agent_id in self.authorized_workspaces:
            ws_path = _os.path.join(data_dir, "workspaces", other_agent_id)
            allowed_dirs.append(ws_path)
        # Allow access to agent's skills directory so granted skill scripts
        # can be executed without sandbox violations.
        agent_skills_dir = _os.path.join(str(self._get_agent_workspace()), "skills")
        if _os.path.isdir(agent_skills_dir):
            allowed_dirs.append(agent_skills_dir)

        policy = _sandbox.SandboxPolicy(
            root=root, mode=mode, allow_list=allow_list,
            agent_id=self.id, agent_name=self.name,
            allowed_dirs=allowed_dirs,
        )
        return _sandbox.sandbox_scope(policy)

    # ---- chat ----

    def chat(self, user_message, on_event: Any = None,
             abort_check: Any = None, source: str = "admin") -> str:
        """
        Run a chat turn. If abort_check is a callable returning True,
        the chat loop will stop early.

        user_message: str for text-only, or list[dict] for multimodal content
                      (OpenAI vision format: [{type:"text",text:...},{type:"image_url",...}])
        source: "admin" for messages from portal UI, "agent:{agent_name}" for inter-agent,
                "system" for system messages
        """
        # ── Token logging context: 让本次 chat 内所有 LLM 调用 ──
        # ── 都能归属到这个 agent，token 统计才能落到 agent.stats ──
        try:
            llm.set_token_context(agent_id=self.id, project_id="")
        except Exception:
            pass

        with self._lock:
            self.status = AgentStatus.BUSY

            # ── Multimodal content handling ──
            # user_message can be str (text-only) or list[dict] (multimodal)
            _is_multimodal = isinstance(user_message, list)
            if _is_multimodal:
                _user_text = " ".join(
                    p.get("text", "") for p in user_message
                    if isinstance(p, dict) and p.get("type") == "text"
                ).strip() or "(multimodal input)"
                _msg_content = user_message  # preserve list for LLM
            else:
                _user_text = str(user_message or "")
                _msg_content = _user_text

            # ── Memory augmentation: inject relevant facts as LLM CONTEXT ──
            # Memory is background reference, NOT a substitute for LLM reasoning.
            # The LLM always generates the actual answer.
            memory_context: str | None = None
            try:
                memory_context = self._build_memory_context(_user_text)
            except Exception as _mem_err:
                logger.debug("Failed to build memory context: %s", _mem_err)
                memory_context = None

            self._ensure_system_message(current_query=_user_text)
            self._trim_context()
            msg = {"role": "user", "content": _msg_content, "source": source}
            self.messages.append(msg)
            self._log("message", {"role": "user", "content": _user_text[:500], "source": source})

            # --- agent_state shadow (phase-1 grey rollout) ---
            # Mirror this user turn into the new typed state model.
            # Failures here MUST NOT affect the live agent path.
            try:
                from .agent_state.shadow import install_into_agent
                _shadow = getattr(self, "_shadow", None) or install_into_agent(self)
                if _shadow is not None:
                    _shadow.record_user(_user_text, source=source)
            except Exception:
                pass

            # --- Memory augmentation: inject retrieved facts as system context ---
            if memory_context:
                self.messages.append({
                    "role": "system",
                    "content": memory_context,
                })

            # --- Enhancement module: pre-thinking injection ---
            if self.enhancer and self.enhancer.enabled:
                pre_think = self.enhancer.pre_think(_user_text)
                if pre_think:
                    # Inject as a system-level context hint before LLM processes
                    self.messages.append({
                        "role": "system",
                        "content": pre_think,
                    })
                    self._log("enhancement", {"action": "pre_think",
                                               "pattern": pre_think[:100]})

            # --- Template Library: auto-match and inject templates ---
            try:
                tpl_lib = get_template_library()
                matched_templates = tpl_lib.match_templates(
                    _user_text, role=self.role, limit=2)
                if matched_templates:
                    tpl_context = tpl_lib.render_for_agent(
                        matched_templates, max_chars=4000)
                    if tpl_context:
                        self.messages.append({
                            "role": "system",
                            "content": tpl_context,
                        })
                        tpl_names = [t.name for t in matched_templates]
                        self._log("template_match", {
                            "templates": tpl_names,
                            "chars": len(tpl_context),
                        })
            except Exception:
                pass  # template library is optional

            # --- Skill System: auto-match and inject skills ---
            self._active_skill_ids = []
            self._chat_start_time = time.time()
            try:
                registry = get_prompt_pack_registry()
                if registry.store.get_active():
                    matched_skills = registry.match_skills(
                        _user_text, top_k=3,
                        agent_skills=self.bound_prompt_packs or None)
                    if matched_skills:
                        skill_ids = [s.skill_id for s in matched_skills]
                        context_text = registry.build_context_injection(
                            skill_ids, max_chars=15000)
                        if context_text:
                            self.messages.append({
                                "role": "system",
                                "content": context_text,
                            })
                            self._active_skill_ids = skill_ids
                            self._log("skill_match", {
                                "skills": [s.name for s in matched_skills],
                                "chars": len(context_text),
                            })
            except Exception:
                pass  # skill system is optional

            # --- src memory engine: transcript + routing ---
            self.transcript.append(_user_text)
            self.turn_count += 1
            # Route prompt through PortRuntime for context enrichment
            try:
                routed = self.route_prompt(_user_text, limit=3)
                if routed:
                    route_info = ", ".join(f"{m.kind}:{m.name}({m.score})"
                                           for m in routed[:3])
                    self._log("routing", {"matches": route_info})
            except Exception:
                routed = []

            def _emit(evt: AgentEvent):
                if on_event:
                    try:
                        on_event(evt)
                    except Exception:
                        pass

            def _is_aborted() -> bool:
                if abort_check and callable(abort_check):
                    return abort_check()
                return False

            tool_defs = self._get_effective_tools()
            final_content = ""

            # History: record chat start
            self.history_log.add("chat_start",
                                 f"user={_user_text[:80]}")

            try:
                old_cwd = os.getcwd()
                if self.working_dir and Path(self.working_dir).is_dir():
                    os.chdir(self.working_dir)

                # ── Real-time provider/model refresh (multimodal-aware) ──
                _eff_provider, _eff_model = self._resolve_effective_provider_model(
                    user_message=user_message,
                )

                # ── Middleware: PRE_LLM (compaction + model routing) ──
                try:
                    from .middleware import ensure_pipeline, MiddlewareContext, Stage
                    _mw_pipe = ensure_pipeline()
                    _mw_ctx = MiddlewareContext(
                        agent_id=self.id, agent_name=self.name,
                        messages=self.messages,
                        provider=_eff_provider, model=_eff_model,
                        data={"context_limit": self._get_context_limit()},
                    )
                    _mw_result = _mw_pipe.run(Stage.PRE_LLM, _mw_ctx)

                    # Handle compaction signal
                    compaction = _mw_ctx.data.get("compaction_needed")
                    if compaction in ("hard", "critical"):
                        self._compress_context()
                    elif compaction == "soft":
                        self._trim_context()

                    # Handle model routing suggestion
                    model_route = _mw_ctx.data.get("model_route")
                    if model_route and self.auto_route.get("enabled") and self.extra_llms:
                        route_label = str(self.auto_route.get(model_route, "")).strip()
                        if route_label:
                            for _slot in self.extra_llms:
                                if not isinstance(_slot, dict):
                                    continue
                                if str(_slot.get("label", "")).strip() == route_label:
                                    _sp = str(_slot.get("provider", "")).strip()
                                    _sm = str(_slot.get("model", "")).strip()
                                    if _sp or _sm:
                                        logger.info(
                                            "Agent %s: middleware model_route[%s] → %s/%s",
                                            self.id[:8], model_route,
                                            _sp or _eff_provider, _sm or _eff_model,
                                        )
                                        _eff_provider = _sp or _eff_provider
                                        _eff_model = _sm or _eff_model
                                    break
                except Exception as _mw_err:
                    logger.debug("pre_llm middleware skipped: %s", _mw_err)

                logger.info("Agent %s (%s) using provider=%s model=%s",
                            self.name, self.id[:8], _eff_provider, _eff_model)

                max_iters = 20

                # ── Task Checkpoint Injection: 任务恢复上下文 ──
                # 当 agent 处于 EXECUTING/PLANNING 阶段时，注入断点信息
                if self.agent_phase in (AgentPhase.EXECUTING, AgentPhase.PLANNING):
                    checkpoint_ctx = self._build_checkpoint_context()
                    if checkpoint_ctx:
                        self.messages.append({
                            "role": "system",
                            "content": checkpoint_ctx,
                        })
                        self._log("checkpoint_inject", {
                            "phase": self.agent_phase.value,
                            "chars": len(checkpoint_ctx),
                        })
                        self.history_log.add("checkpoint",
                                              f"[Checkpoint] 注入任务恢复上下文 phase={self.agent_phase.value}")

                # Build messages-to-send once per iteration: self.messages
                # (stable prefix) + dynamic context injected at the end.
                # This preserves LM Studio / Ollama KV cache across turns.
                _msgs_to_send = self._inject_dynamic_context(
                    self.messages, current_query=_user_text)

                # ── Strip base64 images from older messages ──
                # Only the LAST user message (current turn) keeps its images.
                # Older images are replaced with a text placeholder to save
                # tokens and avoid confusing the model with stale images.
                _msgs_to_send = _strip_old_images(_msgs_to_send)

                # ── Multimodal diagnostic: verify images survive pipeline ──
                if _is_multimodal:
                    _has_mm = any(
                        isinstance(m.get("content"), list)
                        for m in _msgs_to_send if m.get("role") == "user"
                    )
                    logger.info(
                        "MULTIMODAL CHECK: input=True, pipeline_preserved=%s, "
                        "provider=%s model=%s",
                        _has_mm, _eff_provider, _eff_model,
                    )
                    if on_event and _has_mm:
                        _img_count = sum(
                            1 for m in _msgs_to_send
                            if isinstance(m.get("content"), list)
                            for p in m["content"]
                            if isinstance(p, dict) and p.get("type") in (
                                "image_url", "image", "input_image")
                        )
                        _emit(AgentEvent(time.time(), "message", {
                            "role": "system",
                            "content": (
                                f"📎 {_img_count} image(s) received — "
                                f"sending to {_eff_provider}/{_eff_model}"
                            ),
                        }))

                # Vision models (e.g. llama3.2-vision) typically don't
                # support tool calling. When multimodal routing switched
                # to a dedicated vision model, disable tools unless the
                # model is known to support both (e.g. gpt-4o, claude-3.5).
                _effective_tools = tool_defs
                if (_is_multimodal
                        and (self.multimodal_provider or self.multimodal_model)
                        and not self.multimodal_supports_tools):
                    _effective_tools = None

                for iteration in range(max_iters):
                    if _is_aborted():
                        final_content = final_content or "[Aborted]"
                        break
                    # Rebuild messages-to-send each iteration (self.messages
                    # may have grown with tool results from previous iteration).
                    # Dynamic context is appended at the end — keeps prefix stable.
                    if iteration > 0:
                        _msgs_to_send = _strip_old_images(
                            self._inject_dynamic_context(
                                self.messages, current_query=_user_text))
                    # Strategy: always try streaming first (with tools).
                    # If the provider doesn't support streaming+tools,
                    # it falls back to non-streaming internally.
                    # For the first attempt when we have on_event, try
                    # streaming WITHOUT tools to get fast text output.
                    # If the model wants to call a tool, we retry with tools.

                    if on_event and not _effective_tools:
                        # Pure stream, no tools at all
                        try:
                            gen = llm.chat(
                                _msgs_to_send, tools=None, stream=True,
                                provider=_eff_provider, model=_eff_model,
                            )
                            content = ""
                            for chunk in gen:
                                if _is_aborted():
                                    break
                                content += chunk
                                evt = AgentEvent(time.time(), "text_delta",
                                                 {"content": chunk})
                                _emit(evt)
                            if _is_aborted():
                                final_content = content or "[Aborted]"
                                break
                            final_content = content
                            self.messages.append({"role": "assistant",
                                                  "content": content,
                                                  "_source": "llm"})
                            self._log("message",
                                      {"role": "assistant",
                                       "content": content,
                                       "source": "llm"})
                            break
                        except Exception:
                            pass  # Fall through

                    # Non-streaming path (with tools support)
                    if _is_aborted():
                        final_content = final_content or "[Aborted]"
                        break
                    try:
                        response = llm.chat_no_stream(
                            _msgs_to_send, tools=_effective_tools,
                            provider=_eff_provider, model=_eff_model,
                        )
                    except (ConnectionError, OSError) as conn_err:
                        # Provider unreachable — stop retrying immediately
                        raise RuntimeError(
                            f"LLM provider '{_eff_provider}' connection failed "
                            f"(model={_eff_model}): {conn_err}"
                        ) from conn_err
                    except Exception as llm_err:
                        # Other LLM errors (timeout, auth, etc.)
                        if "timeout" in str(llm_err).lower() or "timed out" in str(llm_err).lower():
                            raise RuntimeError(
                                f"LLM provider '{_eff_provider}' timed out "
                                f"(model={_eff_model}): {llm_err}"
                            ) from llm_err
                        raise
                    msg = response.get("message", {})
                    content = _ensure_str_content(msg.get("content"))
                    tool_calls = msg.get("tool_calls", [])

                    if content:
                        final_content = content
                        evt = AgentEvent(time.time(), "message",
                                         {"role": "assistant",
                                          "content": content})
                        self._log(evt.kind, evt.data)
                        _emit(evt)

                    # NOTE: text-to-tool-call extraction (for models that
                    # output JSON as text instead of tool_calls) is handled
                    # in llm.py's _normalize_response_tool_calls(), so
                    # tool_calls here is already normalised.

                    if not tool_calls:
                        # Final response — ensure we always emit something
                        if not content and final_content:
                            # LLM returned empty final response but we had
                            # intermediate content — re-emit the last known content
                            evt = AgentEvent(time.time(), "message",
                                             {"role": "assistant",
                                              "content": final_content})
                            self._log(evt.kind, evt.data)
                            _emit(evt)
                        self.messages.append({"role": "assistant",
                                              "content": content or final_content,
                                              "_source": "llm"})
                        break

                    assistant_msg: dict = {"role": "assistant",
                                           "content": content,
                                           "_source": "llm"}
                    assistant_msg["tool_calls"] = tool_calls
                    self.messages.append(assistant_msg)

                    # Check if all tool calls are parallel-safe
                    all_parallel_safe = all(
                        tc.get("function", {}).get("name", "unknown") in PARALLEL_SAFE_TOOLS
                        for tc in tool_calls
                    )

                    # Parse all tool calls first
                    parsed_calls = []  # list of (name, arguments, call_id)
                    for tc in tool_calls:
                        func_info = tc.get("function", {})
                        name = func_info.get("name", "unknown")
                        call_id = tc.get("id", f"call_{uuid.uuid4().hex[:8]}")
                        arguments = func_info.get("arguments", {})
                        if isinstance(arguments, str):
                            try:
                                arguments = json.loads(arguments)
                            except (json.JSONDecodeError, TypeError):
                                arguments = {}
                        # 双重保护：解析后仍然不是 dict
                        if not isinstance(arguments, dict):
                            try:
                                arguments = json.loads(str(arguments))
                            except (json.JSONDecodeError, TypeError, ValueError):
                                arguments = {"raw": str(arguments)}
                        parsed_calls.append((name, arguments, call_id))

                    # Execute in parallel if all tools are safe, otherwise sequential
                    if all_parallel_safe and len(parsed_calls) > 1:
                        def _execute_single_tool(name_args_id):
                            name, arguments, call_id = name_args_id
                            # Inject caller agent ID
                            if name in ("team_create", "send_message", "task_update",
                                        "mcp_call", "bash", "write_file", "edit_file"):
                                arguments["_caller_agent_id"] = self.id
                            # Execute
                            if name == "plan_update":
                                return name, self._handle_plan_update(arguments), call_id
                            elif name == "request_web_login":
                                return name, self._handle_web_login_request(
                                    arguments, on_event=on_event), call_id
                            else:
                                return name, self._execute_tool_with_policy(
                                    name, arguments, on_event=on_event), call_id

                        with concurrent.futures.ThreadPoolExecutor(
                            max_workers=MAX_PARALLEL_WORKERS
                        ) as executor:
                            futures = [
                                executor.submit(_execute_single_tool, (name, arguments, call_id))
                                for name, arguments, call_id in parsed_calls
                            ]
                            results = []
                            for future in concurrent.futures.as_completed(futures):
                                try:
                                    name, result, call_id = future.result()
                                    result = self._handle_large_result(name, result)
                                    results.append((name, result, call_id))
                                except Exception as e:
                                    logger.error(f"Parallel tool execution error: {e}")
                                    results.append(("unknown", f"Error: {e}", f"call_{uuid.uuid4().hex[:8]}"))
                    else:
                        # Sequential execution
                        results = []
                        for name, arguments, call_id in parsed_calls:
                            if _is_aborted():
                                break

                            evt = AgentEvent(time.time(), "tool_call",
                                             {"name": name,
                                              "arguments": _truncate_dict(arguments)})
                            self._log(evt.kind, evt.data)
                            _emit(evt)

                            # Inject caller agent ID for tools that need agent context
                            if name in ("team_create", "send_message", "task_update",
                                        "mcp_call", "bash", "write_file", "edit_file"):
                                arguments["_caller_agent_id"] = self.id

                            # Handle plan_update internally (needs agent context)
                            if name == "plan_update":
                                result = self._handle_plan_update(arguments)
                                _emit(AgentEvent(time.time(), "plan_update",
                                                 {"plan": self.get_current_plan()}))
                            elif name == "request_web_login":
                                result = self._handle_web_login_request(
                                    arguments, on_event=on_event)
                            else:
                                result = self._execute_tool_with_policy(
                                    name, arguments, on_event=on_event)

                            # Handle large results
                            result = self._handle_large_result(name, result)

                            results.append((name, result, call_id))

                    # Process and emit all results
                    for name, result, call_id in results:
                        # Ensure result is always a string for safe operations
                        result_str = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
                        evt = AgentEvent(time.time(), "tool_result",
                                         {"name": name, "result": result_str[:1000]})
                        self._log(evt.kind, evt.data)
                        _emit(evt)

                        # === 记录 Agent 自身操作到记忆 ===
                        self._record_tool_action(name, result_str)

                        # Check and inject budget pressure note
                        budget_note = llm.get_budget_pressure_note(iteration, max_iters)
                        if budget_note:
                            result_content = result_str + "\n\n" + budget_note
                        else:
                            result_content = result_str

                        self.messages.append({
                            "role": "tool",
                            "content": result_content,
                            "tool_call_id": call_id,
                        })

                        # --- agent_state shadow (phase-1 grey rollout) ---
                        try:
                            _shadow = getattr(self, "_shadow", None)
                            if _shadow is not None:
                                _shadow.record_tool_result(name, result_str)
                        except Exception:
                            pass

                os.chdir(old_cwd)
                self.status = AgentStatus.IDLE

                # --- src integration: track cost & history ---
                if final_content:
                    in_tokens = len(_user_text.split())
                    out_tokens = len(final_content.split())
                    self.total_input_tokens += in_tokens
                    self.total_output_tokens += out_tokens
                    apply_cost_hook(self.cost_tracker,
                                    f"chat:{self.id[:6]}", in_tokens + out_tokens)
                    self.history_log.add("chat_done",
                                         f"[LLM] in={in_tokens} out={out_tokens} total_cost={self.cost_tracker.total_units}")
                    # Track in QueryEngine if initialized
                    if self._query_engine is not None:
                        self._query_engine.total_usage = UsageSummary(
                            input_tokens=self.total_input_tokens,
                            output_tokens=self.total_output_tokens,
                        )
                    # Auto-compact transcript if it's getting large
                    if len(self.transcript.entries) > self.profile.max_context_messages:
                        self.compact_memory()

                    # --- Enhancement module: auto-learn from interaction ---
                    if self.enhancer and self.enhancer.enabled:
                        try:
                            learn_result = self.enhancer.learn_from_interaction(
                                user_message=_user_text,
                                agent_response=final_content[:500],
                                outcome="success",
                            )
                            # 将自我学习的成果也写入 L3 记忆 (向量化)
                            if learn_result:
                                self._sync_enhancement_to_memory(learn_result)
                        except Exception:
                            pass

                    # --- Execution Analyzer: auto-analysis after chat ---
                    try:
                        analysis = analyze_and_grow(
                            self,
                            task_id=f"chat_{int(self._chat_start_time)}",
                            start_time=self._chat_start_time,
                        )
                        if analysis and self._active_skill_ids:
                            registry = get_prompt_pack_registry()
                            tools_used = [e.data.get("tool", "") for e in self.events[-50:]
                                          if e.kind == "tool_call"]
                            for sid in self._active_skill_ids:
                                applied = len(tools_used) > 0
                                registry.mark_skill_applied(
                                    sid, applied=applied,
                                    task_completed=analysis.task_completed)
                    except Exception:
                        pass  # auto-analysis is optional

                    # --- Three-layer memory: post-response write-back ---
                    self._memory_write_back(_user_text, final_content)

                    # --- Update state machine phase ---
                    self._update_agent_phase()

            except Exception as e:
                # Per-agent error isolation: log the error but recover to IDLE
                # so this agent remains usable and doesn't block the system.
                evt = AgentEvent(time.time(), "error", {"error": str(e)})
                self._log(evt.kind, evt.data)
                _emit(evt)
                logger.error("Agent %s (%s) chat error: %s", self.name, self.id, e)
                try:
                    os.chdir(old_cwd)
                except Exception:
                    pass
                final_content = f"Error: {e}"
                # Recover to IDLE — the error is recorded in events/history,
                # but the agent should not stay in ERROR permanently.
                self.status = AgentStatus.IDLE
                # --- agent_state shadow (phase-1 grey rollout) ---
                try:
                    _shadow = getattr(self, "_shadow", None)
                    if _shadow is not None:
                        _shadow.record_error(e)
                except Exception:
                    pass

            # --- agent_state shadow (phase-1 grey rollout) ---
            # Final assistant turn — closes the current shadow task.
            try:
                _shadow = getattr(self, "_shadow", None)
                if _shadow is not None:
                    _shadow.record_assistant(final_content or "")
            except Exception:
                pass

            self._auto_save_check()  # Auto-save after each chat turn
            return final_content

    def chat_async(self, user_message, source: str = "admin") -> ChatTask:
        """Submit a chat as a background task. Returns immediately.

        source: "admin" for messages from portal UI, "agent:{agent_name}" for inter-agent,
                "system" for system messages

        If another chat task is already running for this agent, the new message
        is appended to a per-agent pending queue and will be executed
        sequentially after the current task (and any already-queued tasks)
        finish. We NEVER abort the running task just because a new message
        arrived — that would destroy in-flight work.
        """
        mgr = get_chat_task_manager()
        # Detect any in-flight task for this agent
        active_states = (ChatTaskStatus.THINKING,
                         ChatTaskStatus.STREAMING,
                         ChatTaskStatus.TOOL_EXEC,
                         ChatTaskStatus.QUEUED,
                         ChatTaskStatus.WAITING_APPROVAL)
        has_active = False
        for existing_task in mgr.get_agent_tasks(self.id):
            if existing_task.status in active_states:
                has_active = True
                break
        task = mgr.create_task(self.id, user_message)
        task.set_status(ChatTaskStatus.QUEUED, "Queued", 0)

        # Ensure the per-agent pending-message queue exists
        if not hasattr(self, "_pending_chat_queue") or self._pending_chat_queue is None:
            self._pending_chat_queue = []
        if not hasattr(self, "_pending_chat_lock") or self._pending_chat_lock is None:
            self._pending_chat_lock = threading.Lock()

        if has_active:
            with self._pending_chat_lock:
                self._pending_chat_queue.append((task, user_message, source))
                queue_depth = len(self._pending_chat_queue)
            logger.info(
                "Agent %s busy — queued chat task %s (queue depth=%d)",
                self.id[:8], task.id, queue_depth)
            try:
                task.push_event({
                    "type": "queued",
                    "content": f"⏳ 排队中 ({queue_depth}) — 等上一轮对话结束",
                    "queue_position": queue_depth,
                })
            except Exception:
                pass
            return task

        def _run(task=task, user_message=user_message, source=source):
            try:
                # Show which provider/model is being used — use dynamic
                # routing so multimodal/auto_route overrides are reflected.
                _eff_prov, _eff_mdl = self._resolve_effective_provider_model(
                    user_message=user_message,
                )
                _mdl_name = _eff_mdl or self.model or "default"
                _prov_name = _eff_prov or self.provider or "default"
                try:
                    reg = llm.get_registry()
                    entry = reg.get(_eff_prov or self.provider)
                    if entry:
                        _prov_name = f"{entry.name} ({entry.kind})"
                except Exception:
                    pass
                task.set_status(ChatTaskStatus.THINKING,
                                f"🧠 Thinking... ({_mdl_name})", 10)
                task.push_event({"type": "thinking",
                                 "content": f"🧠 Thinking... ({_mdl_name})"})

                _tool_count = [0]  # track tool iterations for progress

                def _on_event(evt: AgentEvent):
                    """Bridge agent events into ChatTask events."""
                    if evt.kind == "text_delta":
                        task.set_status(ChatTaskStatus.STREAMING,
                                        "Generating response...", 80)
                        task.push_event({"type": "text_delta",
                                         "content": evt.data.get("content", "")})
                    elif evt.kind == "message" and evt.data.get("role") == "assistant":
                        task.set_status(ChatTaskStatus.STREAMING,
                                        "Generating response...", 85)
                        task.push_event({"type": "text",
                                         "content": evt.data.get("content", "")})
                    elif evt.kind == "tool_call":
                        _tool_count[0] += 1
                        name = evt.data.get("name", "")
                        # Progress: 20% base + increments per tool (up to 70%)
                        prog = min(70, 20 + _tool_count[0] * 15)
                        task.set_status(ChatTaskStatus.TOOL_EXEC,
                                        f"{name}", prog)
                        task.push_event({
                            "type": "tool_call",
                            "name": name,
                            "args": json.dumps(
                                evt.data.get("arguments", {}),
                                ensure_ascii=False)[:200],
                        })
                    elif evt.kind == "tool_result":
                        prog = min(75, 25 + _tool_count[0] * 15)
                        task.set_status(ChatTaskStatus.THINKING,
                                        "Analyzing...", prog)
                        task.push_event({
                            "type": "tool_result",
                            "content": evt.data.get("result", "")[:500],
                        })
                        task.push_event({"type": "thinking",
                                         "content": "Thinking..."})
                    elif evt.kind == "approval":
                        status = evt.data.get("status", "")
                        if status == "pending":
                            task.set_status(ChatTaskStatus.WAITING_APPROVAL,
                                            "Waiting for approval...", -1)
                            task.push_event({
                                "type": "approval_request",
                                "tool": evt.data.get("tool", ""),
                                "reason": evt.data.get("reason", ""),
                                "arguments": evt.data.get("arguments", {}),
                                "agent_id": self.id,
                                "agent_name": self.name,
                                "approval_id": evt.data.get("approval_id", ""),
                            })
                        elif status in ("approved", "denied"):
                            task.push_event({
                                "type": "approval_" + status,
                                "tool": evt.data.get("tool", ""),
                            })
                    elif evt.kind == "login_request":
                        task.set_status(ChatTaskStatus.WAITING_APPROVAL,
                                        "Waiting for login credentials...", -1)
                        task.push_event({
                            "type": "login_request",
                            "request_id": evt.data.get("request_id", ""),
                            "url": evt.data.get("url", ""),
                            "site_name": evt.data.get("site_name", ""),
                            "login_url": evt.data.get("login_url", ""),
                            "reason": evt.data.get("reason", ""),
                            "agent_id": self.id,
                            "agent_name": self.name,
                        })
                    elif evt.kind == "plan_update":
                        task.push_event({
                            "type": "plan_update",
                            "plan": evt.data.get("plan"),
                        })
                    elif evt.kind == "error":
                        task.push_event({"type": "error",
                                         "content": evt.data.get("error", "")})

                result = self.chat(user_message, on_event=_on_event,
                                   abort_check=lambda: task.aborted, source=source)
                task.result = result
                if task.aborted:
                    # Already set to ABORTED by abort()
                    pass
                else:
                    # All answers now come from the LLM. Memory is only
                    # injected as background context — no more short-circuit.
                    # --- agent_state shadow (phase-2 envelope injection) ---
                    # Push any artifacts produced during this turn to the
                    # frontend BEFORE the "done" event so the FileCard
                    # widgets attach to the just-finished assistant bubble.
                    # Wrapped in try/except — never break the live path.
                    try:
                        _shadow = getattr(self, "_shadow", None)
                        if _shadow is not None:
                            refs = _shadow.build_envelope_refs()
                            if refs:
                                task.push_event({
                                    "type": "artifact_refs",
                                    "refs": refs,
                                })
                    except Exception:
                        pass
                    task.set_status(ChatTaskStatus.COMPLETED, "Done", 100)
                    task.push_event({"type": "done", "source": "llm"})
            except Exception as e:
                if task.aborted:
                    pass  # Abort may cause exceptions, ignore
                else:
                    task.error = str(e)
                    task.set_status(ChatTaskStatus.FAILED, f"Error: {e}", -1)
                    task.push_event({"type": "error", "content": str(e)})
                    task.push_event({"type": "done"})
            finally:
                # Persist chat history so messages survive a restart.
                # We append to self.messages during chat() but nothing upstream
                # of that call flushes to disk, so we do it here at the end of
                # every chat task (success, failure, or abort).
                try:
                    from .hub import get_hub as _get_hub
                    _hub = _get_hub()
                    if _hub is not None:
                        try:
                            _hub._save_agent_workspace(self)
                        except Exception:
                            pass
                        # Also bump the aggregate JSON/SQLite dump so the
                        # sidebar-state load path sees fresh messages.
                        try:
                            _hub._save_agents()
                        except Exception:
                            pass
                except Exception as _persist_err:
                    logger.debug("post-chat persist failed: %s", _persist_err)

                # Drain pending chat queue: if the user typed more messages
                # while we were busy, run the next one now (sequentially).
                try:
                    next_item = None
                    lock = getattr(self, "_pending_chat_lock", None)
                    if lock is not None:
                        with lock:
                            q = getattr(self, "_pending_chat_queue", None) or []
                            if q:
                                next_item = q.pop(0)
                                # Refresh queue-position events for remaining
                                for _i, (_t, _, _) in enumerate(q):
                                    try:
                                        _t.push_event({
                                            "type": "queued",
                                            "content": f"⏳ 排队中 ({_i+1})",
                                            "queue_position": _i + 1,
                                        })
                                    except Exception:
                                        pass
                    if next_item is not None:
                        nxt_task, nxt_msg, nxt_src = next_item
                        logger.info(
                            "Agent %s draining pending chat task %s",
                            self.id[:8], nxt_task.id)
                        # Re-enter the same _run closure (with rebind via
                        # default parameters) on a fresh worker thread so we
                        # don't grow the call stack.
                        _runner = _run  # local name capture
                        threading.Thread(
                            target=lambda: _runner(nxt_task, nxt_msg, nxt_src),
                            daemon=True,
                        ).start()
                except Exception as _drain_err:
                    logger.debug("pending chat drain failed: %s", _drain_err)

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return task

    # ---- Enhancement module management ----

    def enable_enhancement(self, domain) -> dict:
        """Enable an enhancement domain for this agent.

        `domain` may be a single string (legacy) or a list of up to 8
        preset ids to merge into a composite enhancer.
        """
        if isinstance(domain, (list, tuple)):
            doms = [str(d).strip() for d in domain if str(d).strip()][:8]
            if not doms:
                self.enhancer = build_enhancer("custom")
            elif len(doms) == 1:
                self.enhancer = build_enhancer(doms[0])
            else:
                self.enhancer = build_multi_enhancer(doms)
            label = "+".join(doms) if doms else "custom"
        else:
            self.enhancer = build_enhancer(str(domain))
            label = str(domain)
        # Rebuild system prompt to include new knowledge
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = self._build_system_prompt()
        logger.info("Enhancement enabled for agent %s: domain=%s", self.id, label)
        return self.enhancer.get_stats()

    def disable_enhancement(self):
        """Disable the enhancement module."""
        self.enhancer = None
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = self._build_system_prompt()
        logger.info("Enhancement disabled for agent %s", self.id)

    def get_enhancement_info(self) -> dict | None:
        """Get detailed enhancement module info."""
        if not self.enhancer:
            return None
        return {
            **self.enhancer.get_stats(),
            "knowledge_entries": [e.to_dict() for e in self.enhancer.knowledge.entries.values()],
            "reasoning_patterns": [p.to_dict() for p in self.enhancer.reasoning.patterns.values()],
            "memory_nodes": [n.to_dict() for n in self.enhancer.memory.nodes.values()],
            "tool_chains": [tc.to_dict() for tc in self.enhancer.tool_chains.values()],
        }

    # ---- Active Thinking ----

    def enable_active_thinking(self, **kwargs) -> dict:
        """Enable the active thinking engine for this agent."""
        from .active_thinking import ActiveThinkingEngine
        if not self.active_thinking:
            self.active_thinking = ActiveThinkingEngine(
                agent=self, role=self.role)
        self.active_thinking.enable(**kwargs)
        # Rebuild system prompt to include active thinking context
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = self._build_system_prompt()
        logger.info("Active thinking enabled for agent %s", self.id)
        return self.active_thinking.get_stats()

    def disable_active_thinking(self):
        """Disable active thinking."""
        if self.active_thinking:
            self.active_thinking.disable()
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = self._build_system_prompt()
        logger.info("Active thinking disabled for agent %s", self.id)

    def trigger_thinking(self, trigger: str = "manual",
                         context: str = "") -> dict:
        """Manually trigger one active thinking cycle."""
        from .active_thinking import ActiveThinkingEngine
        if not self.active_thinking:
            self.active_thinking = ActiveThinkingEngine(
                agent=self, role=self.role)
            self.active_thinking.enable()
        result = self.active_thinking.think_now(trigger=trigger,
                                                 context=context)
        return result.to_dict()

    # ---- Self-Improvement (Experience Library) ----

    def enable_self_improvement(self, auto_retro: bool = True,
                                 auto_learn_interval: int = 0,
                                 import_experience: bool = True,
                                 import_limit: int = 50) -> dict:
        """Enable self-improvement for this agent."""
        from .experience_library import SelfImprovementEngine
        if not self.self_improvement:
            self.self_improvement = SelfImprovementEngine(
                agent=self, role=self.role)
        self.self_improvement.enable(
            auto_retro=auto_retro,
            auto_learn_interval=auto_learn_interval,
            import_experience=import_experience,
            import_limit=import_limit,
        )
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = self._build_system_prompt()
        logger.info("Self-improvement enabled for agent %s", self.id)
        return self.self_improvement.get_stats()

    def disable_self_improvement(self):
        """Disable self-improvement."""
        if self.self_improvement:
            self.self_improvement.disable()
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = self._build_system_prompt()
        logger.info("Self-improvement disabled for agent %s", self.id)

    def trigger_retrospective(self, task_summary: str = "",
                               context: str = "") -> dict:
        """Trigger a retrospective analysis."""
        from .experience_library import SelfImprovementEngine
        if not self.self_improvement:
            self.self_improvement = SelfImprovementEngine(
                agent=self, role=self.role)
            self.self_improvement.enable()

        # Build prompt and run through LLM
        prompt = self.self_improvement.build_retrospective_prompt(
            task_summary=task_summary, context=context)

        # Use agent's own LLM to perform retrospective
        try:
            from . import llm
            _prov, _mdl = self._resolve_effective_provider_model()
            resp = llm.chat_no_stream(
                messages=[
                    {"role": "system", "content": "你是一个经验复盘助手。请严格按JSON格式输出。"},
                    {"role": "user", "content": prompt},
                ],
                model=_mdl, provider=_prov,
            )
            raw = resp.get("content", "") if isinstance(resp, dict) else str(resp)
            result = self.self_improvement.process_retrospective_output(
                raw, task_summary=task_summary)
            return result.to_dict()
        except Exception as e:
            logger.error(f"Retrospective failed for agent {self.id}: {e}")
            return {"error": str(e)}

    def trigger_active_learning(self, learning_goal: str = "",
                                 knowledge_gap: str = "") -> dict:
        """Trigger active learning. Lower priority than tasks/projects.

        Rejects empty / placeholder goals — these produce noise in the
        learning plan board and never converge to real experiences. Callers
        (growth tick, portal button, etc.) MUST pass a concrete goal.
        """
        goal = (learning_goal or "").strip()
        if not goal or goal in ("(未设定)", "未设定", "未设定目标"):
            return {
                "status": "rejected",
                "error": "learning_goal required: provide a specific study objective",
                "learning_goal": "",
            }

        from .experience_library import SelfImprovementEngine
        if not self.self_improvement:
            self.self_improvement = SelfImprovementEngine(
                agent=self, role=self.role)
            self.self_improvement.enable()

        # Priority check: if agent has pending tasks/projects, queue instead of executing
        if self.self_improvement.should_pause_for_tasks():
            try:
                queued = self.self_improvement.queue_learning(goal, knowledge_gap)
            except ValueError as ve:
                return {"status": "rejected", "error": str(ve), "learning_goal": goal}
            return {
                "status": "queued",
                "message": f"Agent 有未完成的任务/项目，学习计划已排队。任务完成后将自动执行。",
                "queued_task": queued,
                "learning_goal": goal,
            }

        prompt = self.self_improvement.build_learning_prompt(
            learning_goal=learning_goal, knowledge_gap=knowledge_gap)

        try:
            from . import llm
            _prov, _mdl = self._resolve_effective_provider_model()
            resp = llm.chat_no_stream(
                messages=[
                    {"role": "system", "content": "你是一个主动学习助手。请严格按JSON格式输出。"},
                    {"role": "user", "content": prompt},
                ],
                model=_mdl, provider=_prov,
            )
            raw = resp.get("content", "") if isinstance(resp, dict) else str(resp)
            result = self.self_improvement.process_learning_output(raw)
            return result.to_dict()
        except Exception as e:
            logger.error(f"Active learning failed for agent {self.id}: {e}")
            return {"error": str(e)}

    # ---- Execution Plan: tool handler ----

    def _handle_web_login_request(self, arguments: dict, on_event=None) -> str:
        """Handle request_web_login: pause agent, show login form, wait for credentials."""
        from .auth import create_login_request, wait_for_login
        url = arguments.get("url", "")
        site_name = arguments.get("site_name", "")
        reason = arguments.get("reason", "")
        login_url = arguments.get("login_url", "")
        if not url:
            return json.dumps({"error": "url is required"})

        req = create_login_request(
            agent_id=self.id, agent_name=self.name,
            url=url, site_name=site_name, reason=reason, login_url=login_url,
        )

        # Emit SSE event so chat UI shows login form
        prev_status = self.status
        self.status = AgentStatus.WAITING_APPROVAL
        evt = AgentEvent(time.time(), "login_request", {
            "request_id": req.request_id,
            "url": url,
            "site_name": site_name,
            "login_url": login_url or url,
            "reason": reason,
            "agent_id": self.id,
            "agent_name": self.name,
        })
        self._log(evt.kind, evt.data)
        if on_event:
            on_event(evt)

        # Block until user submits credentials or timeout
        credentials = wait_for_login(req, timeout=300)
        self.status = prev_status if prev_status != AgentStatus.WAITING_APPROVAL else AgentStatus.BUSY

        if not credentials:
            return json.dumps({"error": "Login request expired — no credentials provided within 5 minutes"})

        # ── __BROWSER_SESSION__ signal: user logged in via iframe/new tab,
        #    agent should capture cookies from its own browser instance. ──
        if credentials.get("cookies") == "__BROWSER_SESSION__":
            return json.dumps({
                "ok": True,
                "site_name": site_name,
                "url": url,
                "login_method": "browser_session",
                "note": (
                    "User has completed login in their browser. "
                    "The browser session is now authenticated. "
                    "Use browser_get_cookies or continue browsing the target URL directly — "
                    "the session cookies are already active in the browser context."
                ),
            }, ensure_ascii=False)

        # ── Sensitive-data masking: store real values in runtime vault,
        #    return placeholder variables to LLM context so passwords/tokens
        #    never appear in conversation history or logs. ──
        vault_prefix = f"CRED_{req.request_id[:8]}"
        result = {"ok": True, "site_name": site_name, "url": url, "login_method": "credentials"}
        _cred_fields = [("username", False), ("password", True),
                        ("cookies", True), ("token", True)]
        for field_name, is_secret in _cred_fields:
            val = credentials.get(field_name, "")
            if not val:
                continue
            if is_secret:
                # Store in vault, give LLM a placeholder
                key = f"{vault_prefix}_{field_name.upper()}"
                self._credential_vault[key] = val
                result[field_name] = "{{" + key + "}}"
            else:
                # Non-secret (username) can go to LLM directly
                result[field_name] = val
        result["note"] = (
            "Credentials received. Secret fields are masked as {{CRED_xxx}} placeholders. "
            "Pass them as-is when calling tools — they will be auto-substituted at execution time. "
            "Do NOT attempt to decode or log the placeholders."
        )
        return json.dumps(result, ensure_ascii=False)

    def _substitute_credentials(self, arguments: dict) -> dict:
        """Replace {{CRED_xxx}} placeholders in tool arguments with real values
        from the runtime credential vault. This ensures sensitive data never
        appears in LLM conversation history but is available at execution time."""
        if not self._credential_vault:
            return arguments
        import re
        _CRED_RE = re.compile(r"\{\{(CRED_[A-Za-z0-9_]+)\}\}")
        substituted = {}
        for k, v in arguments.items():
            if isinstance(v, str) and "{{CRED_" in v:
                def _repl(m):
                    return self._credential_vault.get(m.group(1), m.group(0))
                substituted[k] = _CRED_RE.sub(_repl, v)
            else:
                substituted[k] = v
        return substituted

    def _handle_plan_update(self, arguments: dict) -> str:
        """Handle the plan_update tool call internally."""
        # 防御：arguments 可能是 str（LLM 返回未解析的 JSON）
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (json.JSONDecodeError, TypeError):
                return json.dumps({"error": "Invalid arguments format"})
        action = arguments.get("action", "")
        if action == "create_plan":
            # steps 可能是 JSON 字符串而非 list
            # Some models (Qwen) use "plan" instead of "steps"
            steps_raw = arguments.get("steps") or arguments.get("plan") or []
            if isinstance(steps_raw, str):
                try:
                    steps_raw = json.loads(steps_raw)
                except (json.JSONDecodeError, TypeError):
                    steps_raw = []
            if not isinstance(steps_raw, list):
                steps_raw = []
            # 确保每个 step 是 dict
            steps_clean = []
            for s in steps_raw:
                if isinstance(s, str):
                    try:
                        s = json.loads(s)
                    except (json.JSONDecodeError, TypeError):
                        s = {"title": s}
                if isinstance(s, dict):
                    steps_clean.append(s)
            plan = self.create_execution_plan(
                task_summary=arguments.get("task_summary", ""),
                steps=steps_clean,
            )
            step_ids = [{"id": s.id, "title": s.title} for s in plan.steps]
            return json.dumps({"ok": True, "plan_id": plan.id,
                               "steps": step_ids}, ensure_ascii=False)
        elif action == "start_step":
            step = self.update_plan_step(
                arguments.get("step_id", ""), "in_progress")
            return json.dumps({"ok": step is not None,
                               "step": step.to_dict() if step else None},
                              ensure_ascii=False)
        elif action == "complete_step":
            step = self.update_plan_step(
                arguments.get("step_id", ""), "completed",
                arguments.get("result_summary", ""))
            return json.dumps({"ok": step is not None,
                               "step": step.to_dict() if step else None},
                              ensure_ascii=False)
        elif action == "fail_step":
            step = self.update_plan_step(
                arguments.get("step_id", ""), "failed",
                arguments.get("result_summary", ""))
            return json.dumps({"ok": step is not None,
                               "step": step.to_dict() if step else None},
                              ensure_ascii=False)
        elif action == "add_step":
            step = self.add_plan_step(
                title=arguments.get("title", ""),
                detail=arguments.get("detail", ""))
            return json.dumps({"ok": step is not None,
                               "step": step.to_dict() if step else None},
                              ensure_ascii=False)
        elif action == "replan":
            plan = self._current_plan
            if not plan:
                return json.dumps({"ok": False, "error": "No active plan to replan"},
                                  ensure_ascii=False)
            # Keep completed/in_progress steps, remove pending
            plan.steps = [s for s in plan.steps
                          if s.status in (StepStatus.COMPLETED, StepStatus.IN_PROGRESS)]
            new_steps = arguments.get("steps", [])
            if isinstance(new_steps, str):
                try:
                    new_steps = json.loads(new_steps)
                except (json.JSONDecodeError, TypeError):
                    new_steps = []
            if not isinstance(new_steps, list):
                new_steps = []
            for s_data in new_steps:
                if isinstance(s_data, str):
                    try:
                        s_data = json.loads(s_data)
                    except (json.JSONDecodeError, TypeError):
                        s_data = {"title": s_data}
                if isinstance(s_data, dict):
                    step = plan.add_step(
                        title=s_data.get("title", ""),
                        detail=s_data.get("detail", ""),
                    )
                    if "depends_on" in s_data:
                        step.depends_on = s_data["depends_on"]
            plan.status = "active"
            task_summary = arguments.get("task_summary", "")
            if task_summary:
                plan.task_summary = task_summary
            kept = len(plan.steps) - len(new_steps)
            result_text = f"Replanned: kept {kept} completed steps, added {len(new_steps)} new steps"
            step_ids = [{"id": s.id, "title": s.title} for s in plan.steps]
            return json.dumps({"ok": True, "message": result_text,
                               "steps": step_ids}, ensure_ascii=False)
        else:
            return json.dumps({"error": f"Unknown action: {action}"},
                              ensure_ascii=False)

    # ---- Execution Plan management ----

    def create_execution_plan(self, task_summary: str,
                               steps: list[dict] | None = None) -> ExecutionPlan:
        """Create a new execution plan for the current task."""
        plan = ExecutionPlan(task_summary=task_summary)
        if steps:
            for s in steps:
                step = plan.add_step(
                    title=s.get("title", ""),
                    detail=s.get("detail", ""),
                )
                if "depends_on" in s:
                    step.depends_on = s["depends_on"]
        self._current_plan = plan
        self.execution_plans.append(plan)
        # Emit event so UI can update
        self._log("plan_created", {
            "plan_id": plan.id,
            "task": task_summary[:100],
            "steps": len(plan.steps),
        })
        # 写入 L3 记忆：里程碑/步骤持久化
        self._write_plan_to_memory(plan)
        # 更新状态机
        self._update_agent_phase()
        return plan

    def update_plan_step(self, step_id: str, status: str,
                          result_summary: str = "") -> ExecutionStep | None:
        """Update a step's status in the current plan."""
        plan = self._current_plan
        if not plan:
            return None
        if status == "in_progress":
            step = plan.start_step(step_id)
        elif status == "completed":
            step = plan.complete_step(step_id, result_summary)
        elif status == "failed":
            step = plan.fail_step(step_id, result_summary)
        else:
            return None
        if step:
            self._log("plan_step_updated", {
                "plan_id": plan.id,
                "step_id": step.id,
                "title": step.title,
                "status": status,
            })
            # 步骤完成时写入 L3 记忆
            if status == "completed" and plan:
                self._write_step_completion_to_memory(plan, step)
            # 更新状态机
            self._update_agent_phase()
        return step

    def add_plan_step(self, title: str, detail: str = "") -> ExecutionStep | None:
        """Add a new step to the current plan (during execution)."""
        if not self._current_plan:
            return None
        step = self._current_plan.add_step(title=title, detail=detail)
        self._log("plan_step_added", {
            "plan_id": self._current_plan.id,
            "step_id": step.id,
            "title": title,
        })
        return step

    def get_current_plan(self) -> dict | None:
        """Get the current execution plan for UI display."""
        if self._current_plan:
            return self._current_plan.to_dict()
        return None

    def get_execution_plans(self, limit: int = 10) -> list[dict]:
        """Get recent execution plans."""
        return [p.to_dict() for p in self.execution_plans[-limit:]]

    def delegate(self, task: str, from_agent: str = "hub", child_agent: "Agent | None" = None) -> str:
        """
        Enhanced delegate() with depth tracking, isolation, and parent-child relationship.

        Args:
            task: Task description/prompt
            from_agent: Name of delegating agent
            child_agent: Optional pre-created Agent instance. If None, creates a new sub-agent.

        Returns:
            Result string from the delegated task

        Raises:
            RuntimeError: If delegation depth exceeds max_delegate_depth
        """
        # Check delegation depth limit
        if self._delegate_depth >= self._max_delegate_depth:
            error_msg = (
                f"Delegation depth limit reached (current: {self._delegate_depth}, "
                f"max: {self._max_delegate_depth}). Cannot spawn new sub-agent."
            )
            self._log("delegation_error", {
                "error": "depth_limit_exceeded",
                "current_depth": self._delegate_depth,
                "max_depth": self._max_delegate_depth,
            })
            logger.error(error_msg)
            return f"ERROR: {error_msg}"

        # ── Admin-configurable fork policy (auth.tool_policy.fork_policy) ──
        # Resolve prospective child_role for role-edge check.
        # If caller passed a pre-built child_agent, use its role; otherwise the
        # new sub-agent will inherit self.role (see child_agent creation below).
        _prospective_child_role = child_agent.role if child_agent is not None else self.role
        _policy = None
        try:
            from .auth import get_auth as _get_auth
            _policy = _get_auth().tool_policy
            cost_last_hour = 0.0
            try:
                cost_last_hour = float(getattr(self.cost_tracker, "cost_last_hour", lambda: 0.0)())
            except Exception:
                pass
            ok, reason = _policy.check_fork_allowed(
                parent_id=self.id, parent_role=self.role,
                parent_depth=self._delegate_depth,
                cost_last_hour_usd=cost_last_hour,
                child_role=_prospective_child_role,
            )
            if not ok:
                self._log("delegation_error", {"error": "fork_policy_blocked", "reason": reason})
                logger.warning("Fork policy blocked: %s", reason)
                return f"ERROR: fork policy denied: {reason}"
            _policy.register_fork_start(self.id)
        except Exception as _e:
            logger.debug("fork policy check skipped: %s", _e)
            _policy = None

        # Create or use provided child agent
        if child_agent is None:
            child_agent = Agent(
                name=f"{self.name}_child_{uuid.uuid4().hex[:6]}",
                role=self.role,
                model=self.model,
                provider=self.provider,
                # Inherit working directory and shared workspace
                working_dir=self.working_dir,
                shared_workspace=self.shared_workspace,
                system_prompt=self.system_prompt,
                profile=AgentProfile.from_dict(self.profile.to_dict()),
                node_id=self.node_id,
                parent_id=self.id,  # Track parent relationship
                authorized_workspaces=list(self.authorized_workspaces),
            )

        # Set child's depth to parent's depth + 1
        child_agent._delegate_depth = self._delegate_depth + 1
        child_agent._max_delegate_depth = self._max_delegate_depth

        # Inherit cancellation event from parent for interrupt signaling
        child_agent._cancellation_event = self._cancellation_event

        # Track active child
        with self._active_children_lock:
            self._active_children.append((child_agent.id, child_agent))

        try:
            # Build prompt with delegation metadata
            prompt = f"[Delegated task from {from_agent} | depth={child_agent._delegate_depth}/{self._max_delegate_depth}]\n{task}"

            # Log delegation event
            self._log("inter_agent_message", {
                "from_agent": from_agent,
                "to_agent": child_agent.id,
                "content": task[:500],
                "msg_type": "delegation",
                "depth": child_agent._delegate_depth,
            })

            logger.info(
                "DELEGATE: parent=%s child=%s task_len=%d depth=%d/%d",
                self.id, child_agent.id, len(task),
                child_agent._delegate_depth, self._max_delegate_depth
            )

            # Execute delegated task with isolation (separate message history)
            result = child_agent.chat(prompt)

            return result

        except Exception as e:
            error_msg = f"Delegation to {child_agent.id} failed: {str(e)}"
            self._log("delegation_error", {
                "error": str(e),
                "child_agent": child_agent.id,
                "depth": child_agent._delegate_depth,
            })
            logger.error(error_msg)
            return f"ERROR: {error_msg}"

        finally:
            # Remove child from active list when done
            with self._active_children_lock:
                self._active_children = [
                    (aid, ag) for aid, ag in self._active_children
                    if aid != child_agent.id
                ]
            # Decrement fork policy counter
            try:
                if _policy is not None:
                    _policy.register_fork_end(self.id)
            except Exception:
                pass

    def delegate_parallel(self, tasks: list[dict], max_workers: int = 4) -> list[dict]:
        """
        Spawn multiple sub-agents in parallel to handle a list of tasks.

        This is a Hermes-style parallel delegation pattern that:
        - Creates isolated sub-agent instances (each with separate message history)
        - Executes tasks concurrently via ThreadPoolExecutor
        - Shares: working_dir, tool access, LLM config, parent context
        - Respects: delegation depth limits, cancellation signals

        Args:
            tasks: List of task dicts, each containing:
                - "task" (str, required): Task description
                - "agent_id" (str, optional): Custom sub-agent ID (for tracking)
                - "context" (str, optional): Extra context to inject into task
            max_workers: Max parallel sub-agents (capped at 4 for safety)

        Returns:
            List of result dicts:
                [{
                    "agent_id": str,
                    "task": str,
                    "status": "success" | "failed" | "cancelled",
                    "result": str,
                    "error": str (if failed),
                    "duration": float,
                }]

        Example:
            results = agent.delegate_parallel([
                {"task": "Review code in file A for security", "agent_id": "reviewer_a"},
                {"task": "Review code in file B for performance", "agent_id": "reviewer_b"},
                {"task": "Write unit tests", "context": "Use pytest framework"},
            ])
        """
        if self._delegate_depth >= self._max_delegate_depth:
            error_msg = (
                f"Cannot delegate_parallel: depth limit reached "
                f"(current: {self._delegate_depth}, max: {self._max_delegate_depth})"
            )
            self._log("parallel_delegation_error", {"error": "depth_limit_exceeded"})
            return [{
                "status": "failed",
                "error": error_msg,
                "task": t.get("task", ""),
                "agent_id": t.get("agent_id", "unknown"),
                "result": "",
                "duration": 0.0,
            } for t in tasks]

        # Cap max_workers at 4 for safety
        max_workers = min(max_workers, 4)

        self._log("parallel_delegation_start", {
            "task_count": len(tasks),
            "max_workers": max_workers,
            "depth": self._delegate_depth,
        })

        logger.info(
            "PARALLEL_DELEGATE: parent=%s tasks=%d workers=%d depth=%d/%d",
            self.id, len(tasks), max_workers,
            self._delegate_depth, self._max_delegate_depth
        )

        results = []
        start_time = time.time()

        # Pre-flight fork policy check (one allowance per parallel task)
        try:
            from .auth import get_auth as _get_auth_pp
            _pp_policy = _get_auth_pp().tool_policy
        except Exception:
            _pp_policy = None

        def _execute_task(task_spec: dict) -> dict:
            """Execute a single task in a sub-agent (runs in thread)."""
            task_text = task_spec.get("task", "")
            agent_id = task_spec.get("agent_id", f"sub_{uuid.uuid4().hex[:6]}")
            context = task_spec.get("context", "")
            task_start = time.time()

            # Per-task fork policy check
            # parallel sub-agents inherit self.role, so child_role = self.role
            if _pp_policy is not None:
                try:
                    ok, reason = _pp_policy.check_fork_allowed(
                        parent_id=self.id, parent_role=self.role,
                        parent_depth=self._delegate_depth,
                        child_role=task_spec.get("role") or self.role,
                    )
                    if not ok:
                        return {"agent_id": agent_id, "task": task_text,
                                "status": "blocked", "result": "",
                                "error": f"fork policy: {reason}",
                                "duration": time.time() - task_start}
                    _pp_policy.register_fork_start(self.id)
                except Exception:
                    pass

            try:
                # Check cancellation signal
                if self._cancellation_event.is_set():
                    return {
                        "agent_id": agent_id,
                        "task": task_text,
                        "status": "cancelled",
                        "result": "",
                        "error": "Cancelled by parent agent",
                        "duration": time.time() - task_start,
                    }

                # Create isolated sub-agent
                sub_agent = Agent(
                    name=f"{self.name}_parallel_{agent_id}",
                    role=self.role,
                    model=self.model,
                    provider=self.provider,
                    working_dir=self.working_dir,
                    shared_workspace=self.shared_workspace,
                    system_prompt=self.system_prompt,
                    profile=AgentProfile.from_dict(self.profile.to_dict()),
                    node_id=self.node_id,
                    parent_id=self.id,
                    authorized_workspaces=list(self.authorized_workspaces),
                )

                # Set depth and inherit cancellation event
                sub_agent._delegate_depth = self._delegate_depth + 1
                sub_agent._max_delegate_depth = self._max_delegate_depth
                sub_agent._cancellation_event = self._cancellation_event

                # Track as active child
                with self._active_children_lock:
                    self._active_children.append((sub_agent.id, sub_agent))

                try:
                    # Build task prompt with context
                    full_task = task_text
                    if context:
                        full_task = f"{task_text}\n\n[Additional Context]\n{context}"

                    prompt = f"[Parallel delegated task | agent={agent_id} | depth={sub_agent._delegate_depth}/{self._max_delegate_depth}]\n{full_task}"

                    # Execute in isolation (separate message history)
                    result = sub_agent.chat(prompt)

                    return {
                        "agent_id": agent_id,
                        "task": task_text,
                        "status": "success",
                        "result": result,
                        "error": "",
                        "duration": time.time() - task_start,
                    }

                finally:
                    # Clean up from active children list
                    with self._active_children_lock:
                        self._active_children = [
                            (aid, ag) for aid, ag in self._active_children
                            if aid != sub_agent.id
                        ]
                    if _pp_policy is not None:
                        try:
                            _pp_policy.register_fork_end(self.id)
                        except Exception:
                            pass

            except Exception as e:
                if _pp_policy is not None:
                    try:
                        _pp_policy.register_fork_end(self.id)
                    except Exception:
                        pass
                return {
                    "agent_id": agent_id,
                    "task": task_text,
                    "status": "failed",
                    "result": "",
                    "error": str(e),
                    "duration": time.time() - task_start,
                }

        # Execute tasks in parallel using ThreadPoolExecutor
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(_execute_task, task): task for task in tasks}

            for future in concurrent.futures.as_completed(futures):
                try:
                    result = future.result(timeout=300)  # 5 min timeout per task
                    results.append(result)
                except Exception as e:
                    task = futures[future]
                    results.append({
                        "agent_id": task.get("agent_id", "unknown"),
                        "task": task.get("task", ""),
                        "status": "failed",
                        "result": "",
                        "error": str(e),
                        "duration": time.time() - task_start,
                    })

        total_duration = time.time() - start_time

        self._log("parallel_delegation_complete", {
            "task_count": len(tasks),
            "success": sum(1 for r in results if r["status"] == "success"),
            "failed": sum(1 for r in results if r["status"] == "failed"),
            "cancelled": sum(1 for r in results if r["status"] == "cancelled"),
            "duration": total_duration,
        })

        logger.info(
            "PARALLEL_DELEGATE COMPLETE: parent=%s success=%d failed=%d duration=%.2fs",
            self.id,
            sum(1 for r in results if r["status"] == "success"),
            sum(1 for r in results if r["status"] == "failed"),
            total_duration
        )

        return results

    def cancel_children(self) -> dict:
        """
        Signal all active child agents to stop execution.

        Sets a threading.Event that child agents check in their chat loops
        (if abort_check callback is used). Returns summary of cancellation.

        Returns:
            {"cancelled_count": int, "agent_ids": list[str]}
        """
        self._cancellation_event.set()

        with self._active_children_lock:
            agent_ids = [aid for aid, _ in self._active_children]
            child_count = len(self._active_children)

        self._log("children_cancelled", {
            "count": child_count,
            "agent_ids": agent_ids,
        })

        logger.info(
            "CANCEL_CHILDREN: parent=%s cancelled=%d agents=%s",
            self.id, child_count, agent_ids
        )

        return {
            "cancelled_count": child_count,
            "agent_ids": agent_ids,
        }

    def clear(self):
        with self._lock:
            self.messages.clear()
            self.events.clear()
            self._log("status", {"action": "cleared"})

    def update_profile(self, **kwargs):
        """Update profile fields dynamically."""
        for k, v in kwargs.items():
            if hasattr(self.profile, k):
                setattr(self.profile, k, v)

    # ---- task management ----

    def add_task(self, title: str, description: str = "",
                 priority: int = 0, parent_id: str = "",
                 assigned_by: str = "user",
                 source: str = "admin",
                 source_agent_id: str = "",
                 deadline: float = 0.0,
                 tags: list[str] | None = None,
                 recurrence: str = "once",
                 recurrence_spec: str = "") -> AgentTask:
        task = AgentTask(
            title=title, description=description,
            priority=priority, parent_id=parent_id,
            assigned_by=assigned_by,
            source=source,
            source_agent_id=source_agent_id,
            deadline=deadline,
            tags=tags or [],
            recurrence=recurrence or "once",
            recurrence_spec=recurrence_spec or "",
        )
        # Compute next_run_at for recurring tasks
        if task.recurrence != "once":
            try:
                from .scheduler import compute_next_run
                task.next_run_at = compute_next_run(
                    task.recurrence, task.recurrence_spec, time.time()) or 0.0
            except Exception:
                task.next_run_at = 0.0
        self.tasks.append(task)
        self._log("task", {"action": "created", "task_id": task.id,
                           "title": title, "source": source,
                           "deadline": task.deadline_str or "none"})
        logger.info("TASK added: agent=%s task=%s title='%s' source=%s deadline=%s",
                    self.name, task.id, title, source, task.deadline_str or "none")
        # Inject task notification into agent context so it knows about the task
        self._notify_new_task(task)
        return task

    def _notify_new_task(self, task: AgentTask):
        """Inject a system-level notification so the agent is aware of the new task."""
        if task.notified:
            return
        deadline_info = f" | Deadline: {task.deadline_str}" if task.deadline else ""
        priority_label = {0: "Normal", 1: "High", 2: "Urgent"}.get(task.priority, "Normal")
        source_label = f"{task.source}"
        if task.source_agent_id:
            source_label += f" (agent: {task.source_agent_id})"
        notification = (
            f"[TASK ASSIGNED] ID: {task.id}\n"
            f"  Title: {task.title}\n"
            f"  Description: {task.description or 'N/A'}\n"
            f"  Priority: {priority_label}{deadline_info}\n"
            f"  Source: {source_label}\n"
            f"  Status: {task.status.value}\n"
            f"  Please acknowledge and work on this task."
        )
        # Add as a system message so the agent sees it in its next turn
        self.messages.append({"role": "user", "content": notification})
        task.notified = True
        self._log("task", {"action": "notified", "task_id": task.id})
        logger.info("TASK notified: agent=%s task=%s", self.name, task.id)

    def get_pending_tasks_summary(self) -> str:
        """Generate a summary of pending tasks for injection into agent context."""
        pending = [t for t in self.tasks if t.status in (TaskStatus.TODO, TaskStatus.IN_PROGRESS)]
        if not pending:
            return ""
        lines = ["[PENDING TASKS]"]
        for t in pending:
            dl = f" (Due: {t.deadline_str})" if t.deadline else ""
            overdue = " ⚠️OVERDUE" if t.is_overdue else ""
            pri = {0: "", 1: " [HIGH]", 2: " [URGENT]"}.get(t.priority, "")
            lines.append(f"  - [{t.status.value}]{pri} {t.title}{dl}{overdue}")
        return "\n".join(lines)

    # ── Self-growth closed loop ───────────────────────────────────────────
    def enqueue_growth_task(self, learning_goal: str = "",
                            knowledge_gap: str = "",
                            title: str = "") -> AgentTask:
        """Create a low-priority self-growth task.

        Growth tasks have priority -1 (background). They only run when the
        agent has no pending normal/high/urgent tasks AND is not busy
        chatting. They use the agent's learning_provider/learning_model
        (cheap/local) instead of the main provider/model.
        """
        task_title = title or (
            f"自我成长: {learning_goal}" if learning_goal else "自我成长 — 经验积累"
        )
        task = AgentTask(
            title=task_title,
            description=knowledge_gap or learning_goal or "Background self-improvement run.",
            priority=-1,
            assigned_by="system",
            source="system",
            tags=["growth"],
            provider=self.learning_provider or "",
            model=self.learning_model or "",
            learning_goal=learning_goal or "",
            knowledge_gap=knowledge_gap or "",
        )
        self.tasks.append(task)
        self._log("task", {
            "action": "growth_enqueued",
            "task_id": task.id,
            "title": task_title,
            "learning_provider": self.learning_provider or "(default)",
            "learning_model": self.learning_model or "(default)",
        })
        logger.info("GROWTH task enqueued: agent=%s task=%s",
                    self.name, task.id)
        return task

    def _has_higher_priority_pending(self) -> bool:
        """True if there are any non-growth tasks still pending or running."""
        for t in self.tasks:
            if t.status in (TaskStatus.TODO, TaskStatus.IN_PROGRESS) and t.priority >= 0:
                return True
        return False

    def _next_growth_task(self) -> AgentTask | None:
        for t in self.tasks:
            if t.status == TaskStatus.TODO and t.priority < 0:
                return t
        return None

    def _derive_growth_gap(self) -> dict | None:
        """Introspect recent activity to synthesize a new growth topic.

        Strategy (cheap, no-LLM):
        1. If there are recent failed tool calls in history_log, target the
           most frequent failing tool.
        2. Otherwise pick a topic from the agent's role/expertise that
           hasn't been studied in the last 24h (tracked via _learning_topics_seen).
        3. Throttle: only synthesize a new topic at most once every 30 minutes
           per agent to avoid spamming the learning model.
        """
        now = time.time()
        # Throttle synthesis
        last_synth = getattr(self, "_last_growth_synth", 0.0) or 0.0
        if (now - last_synth) < 1800:
            return None
        self._last_growth_synth = now

        # Scan recent history for failures
        try:
            recent = list(self.history_log.events[-40:])
        except Exception:
            recent = []
        failures = []
        for e in recent:
            title = (getattr(e, "title", "") or "").lower()
            detail = (getattr(e, "detail", "") or "")
            if "fail" in title or "error" in title or "denied" in title:
                failures.append(detail[:200])
        if failures:
            return {
                "goal": "排查最近的执行失败并提炼改进经验",
                "gap": "最近的失败样本：\n" + "\n".join("- " + f for f in failures[:5]),
            }

        # Fallback: role/expertise-based self-study
        topics_seen = getattr(self, "_growth_topics_seen", None)
        if topics_seen is None:
            topics_seen = {}
            self._growth_topics_seen = topics_seen
        candidates = []
        role = (self.role or "general").lower()
        expertise = list(getattr(self.profile, "expertise", []) or [])
        base_topics = {
            "coder": ["代码重构技巧", "常见 bug 模式", "测试策略", "性能优化"],
            "reviewer": ["代码审查清单", "安全漏洞模式", "可读性准则"],
            "cto": ["系统设计权衡", "技术选型评估", "团队协作流程"],
            "pm": ["需求拆解方法", "风险管理", "里程碑规划"],
            "general": ["沟通技巧", "问题分解方法", "学习新工具的流程"],
        }
        candidates = list(expertise) + base_topics.get(role, base_topics["general"])
        for topic in candidates:
            last_ts = topics_seen.get(topic, 0.0)
            if (now - last_ts) > 86400:  # not in last 24h
                topics_seen[topic] = now
                return {
                    "goal": f"自我学习 — {topic}",
                    "gap": f"围绕角色 '{self.role}' 深化对「{topic}」的理解。请总结关键要点并提炼可复用的经验条目。",
                }
        return None

    def tick_growth(self, min_interval: float = 60.0) -> dict | None:
        """Heartbeat hook: pick a growth task and run it if truly idle.

        Idle = no higher-priority pending tasks AND no active chat. Throttled
        by ``min_interval`` seconds between runs per agent. Returns the task's
        result dict on success, or None if nothing happened.
        """
        now = time.time()
        if (now - self._last_growth_tick) < min_interval:
            return None
        # Don't interrupt user chat or running work
        if self.status == AgentStatus.BUSY:
            return None
        if self._has_higher_priority_pending():
            return None
        task = self._next_growth_task()
        # ── Closed-loop #1: drain SelfImprovementEngine._learning_queue into
        # growth tasks. trigger_active_learning() queues here when the agent
        # was busy at request time; we now unblock them. Skip entries whose
        # goal is empty — they'd just pollute the board. ──
        if task is None and self.self_improvement is not None:
            queue = getattr(self.self_improvement, "_learning_queue", None) or []
            while queue and task is None:
                nxt = queue.pop(0)
                _g = (nxt.get("learning_goal") or "").strip()
                if not _g:
                    continue  # drop noise
                task = self.enqueue_growth_task(
                    learning_goal=_g,
                    knowledge_gap=(nxt.get("knowledge_gap") or "").strip(),
                )
        # ── Closed-loop #2: if still nothing, synthesize a background
        # introspection task from recent failures / unreviewed experience.
        # We ONLY enqueue when derive returns a concrete goal; no more empty
        # "自我反思与经验沉淀" fallback. ──
        if task is None:
            gap = self._derive_growth_gap()
            if gap and (gap.get("goal") or "").strip():
                task = self.enqueue_growth_task(
                    learning_goal=gap["goal"].strip(),
                    knowledge_gap=(gap.get("gap") or "").strip(),
                )
        if task is None:
            return None
        self._last_growth_tick = now
        # Mark in-progress and run
        task.status = TaskStatus.IN_PROGRESS
        task.updated_at = now
        prev_task = self._current_task
        self._current_task = task  # routes provider/model via resolver
        try:
            result = self.trigger_active_learning(
                learning_goal=task.learning_goal,
                knowledge_gap=task.knowledge_gap,
            )
            task.status = TaskStatus.DONE
            task.result = (
                result.get("message", "")
                or result.get("status", "")
                or "growth tick complete"
            )[:500] if isinstance(result, dict) else str(result)[:500]
            task.updated_at = time.time()
            self._log("task", {
                "action": "growth_done",
                "task_id": task.id,
            })
            return result if isinstance(result, dict) else {"result": str(result)}
        except Exception as e:
            task.status = TaskStatus.BLOCKED
            task.result = f"growth tick failed: {e}"
            logger.error("GROWTH tick failed for agent %s: %s", self.name, e)
            return {"error": str(e)}
        finally:
            self._current_task = prev_task

    def update_task(self, task_id: str, **kwargs) -> AgentTask | None:
        for t in self.tasks:
            if t.id == task_id:
                for k, v in kwargs.items():
                    if k == "status":
                        t.status = TaskStatus(v) if isinstance(v, str) else v
                    elif hasattr(t, k):
                        setattr(t, k, v)
                t.updated_at = time.time()
                self._log("task", {"action": "updated", "task_id": task_id,
                                   "changes": list(kwargs.keys())})
                return t
        return None

    def get_task(self, task_id: str) -> AgentTask | None:
        for t in self.tasks:
            if t.id == task_id:
                return t
        return None

    def list_tasks(self, status: str = "") -> list[AgentTask]:
        if status:
            return [t for t in self.tasks if t.status.value == status]
        return list(self.tasks)

    def remove_task(self, task_id: str) -> bool:
        for i, t in enumerate(self.tasks):
            if t.id == task_id:
                self.tasks.pop(i)
                self._log("task", {"action": "removed", "task_id": task_id})
                return True
        return False


# ---------------------------------------------------------------------------
# ChatTask — background chat execution with progress tracking
# ---------------------------------------------------------------------------

class ChatTaskStatus(str, Enum):
    QUEUED = "queued"
    THINKING = "thinking"
    STREAMING = "streaming"
    TOOL_EXEC = "tool_exec"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


@dataclass
class ChatTask:
    """A background chat task that runs independently of the HTTP connection."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    agent_id: str = ""
    user_message: str = ""
    status: ChatTaskStatus = ChatTaskStatus.QUEUED
    progress: int = 0           # 0-100
    phase: str = ""             # human-readable phase description
    result: str = ""            # final assistant text
    error: str = ""
    events: list = field(default_factory=list)   # SSE event dicts
    _event_cursor: int = 0      # for clients to track what they've read
    aborted: bool = False       # abort flag checked by chat loop
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def abort(self):
        """Signal the task to stop."""
        self.aborted = True
        self.set_status(ChatTaskStatus.ABORTED, "Aborted by user", -1)
        self.push_event({"type": "error", "content": "Task aborted by user"})
        self.push_event({"type": "done"})

    def push_event(self, evt: dict):
        """Thread-safe event push."""
        with self._lock:
            self.events.append(evt)
            self.updated_at = time.time()

    def get_events_since(self, cursor: int) -> tuple[list[dict], int]:
        """Return events since cursor and new cursor position."""
        with self._lock:
            new_events = self.events[cursor:]
            return new_events, len(self.events)

    def set_status(self, status: ChatTaskStatus, phase: str = "",
                   progress: int = -1):
        with self._lock:
            self.status = status
            if phase:
                self.phase = phase
            if progress >= 0:
                self.progress = min(progress, 100)
            self.updated_at = time.time()

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "status": self.status.value,
            "progress": self.progress,
            "phase": self.phase,
            "result": self.result[:500] if self.result else "",
            "error": self.error,
            "event_count": len(self.events),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }



# ChatTaskManager, get_chat_task_manager — imported from app.chat_task (line 39-42).
# DO NOT duplicate here; the router must share the same singleton.


def _truncate_dict(d: dict, max_len: int = 200) -> dict:
    out = {}
    for k, v in d.items():
        if isinstance(v, str) and len(v) > max_len:
            out[k] = v[:max_len] + "..."
        else:
            out[k] = v
    return out


# ---------------------------------------------------------------------------
# Role presets — now with rich profile defaults
# ---------------------------------------------------------------------------

ROLE_PRESETS: dict[str, dict] = {
    "general": {
        "name": "Claw",
        "system_prompt": "You are a general-purpose AI programming assistant.",
        "profile": AgentProfile(
            personality="helpful",
            communication_style="technical",
            expertise=["programming", "debugging", "system administration"],
            skills=["code_writing", "code_review", "debugging", "documentation"],
        ),
    },
    "coder": {
        "name": "Coder",
        "system_prompt": (
            "You are an expert software engineer. Write clean, well-tested code. "
            "Always read existing code before making changes. Prefer small, focused edits."
        ),
        "profile": AgentProfile(
            personality="precise",
            communication_style="technical",
            expertise=["python", "javascript", "rust", "go", "algorithms", "data structures"],
            skills=["code_writing", "refactoring", "testing", "optimization"],
            auto_approve_tools=["write_file", "edit_file"],
        ),
    },
    "reviewer": {
        "name": "Reviewer",
        "system_prompt": (
            "You are a code reviewer. Read the code carefully, find bugs, suggest improvements. "
            "Be constructive and specific. Check for security issues, performance, and readability."
        ),
        "profile": AgentProfile(
            personality="strict",
            communication_style="detailed",
            expertise=["code_quality", "security", "performance", "best_practices"],
            skills=["code_review", "security_audit", "performance_analysis"],
            allowed_tools=["read_file", "search_files", "glob_files", "bash", "web_search"],
            denied_tools=["write_file", "edit_file"],
        ),
    },
    "researcher": {
        "name": "Researcher",
        "system_prompt": (
            "You are a research assistant. Search the web, read documentation, and gather "
            "information. Summarise findings clearly with sources."
        ),
        "profile": AgentProfile(
            personality="curious",
            communication_style="educational",
            expertise=["research", "documentation", "technical_writing"],
            skills=["web_research", "summarization", "comparison", "analysis"],
            allowed_tools=["read_file", "search_files", "glob_files",
                           "web_search", "web_fetch"],
            denied_tools=["write_file", "edit_file", "bash"],
        ),
    },
    "architect": {
        "name": "Architect",
        "system_prompt": (
            "You are a software architect. Design systems, plan implementations, and make "
            "high-level technical decisions. Consider trade-offs, scalability, and maintainability."
        ),
        "profile": AgentProfile(
            personality="thoughtful",
            communication_style="detailed",
            expertise=["system_design", "architecture", "scalability",
                        "microservices", "database_design"],
            skills=["system_design", "technical_planning", "trade-off_analysis",
                    "documentation"],
        ),
    },
    "devops": {
        "name": "DevOps",
        "system_prompt": (
            "You are a DevOps engineer. Help with CI/CD, Docker, deployment, monitoring, "
            "and infrastructure. Use bash commands to inspect and configure systems."
        ),
        "profile": AgentProfile(
            personality="pragmatic",
            communication_style="brief",
            expertise=["docker", "kubernetes", "ci_cd", "monitoring",
                        "linux", "networking", "cloud"],
            skills=["deployment", "monitoring", "troubleshooting",
                    "infrastructure_as_code"],
            auto_approve_tools=["bash"],
        ),
    },
}


def create_agent(
    name: str = "",
    role: str = "general",
    model: str = "",
    provider: str = "",
    working_dir: str = "",
    system_prompt: str = "",
    node_id: str = "local",
    profile_overrides: dict | None = None,
    parent_id: str = "",
    priority_level: int = 3,
    role_title: str = "",
) -> Agent:
    """Create a new agent from a role preset, with optional profile overrides.

    priority_level: 1=CXO (highest), 2=PM, 3=Team Member (default)
    role_title: e.g. "CXO", "PM", "Developer"
    """
    logger.info("create_agent: name=%s role=%s model=%s provider=%s node_id=%s priority=%s",
                name, role, model, provider, node_id, priority_level)
    preset = ROLE_PRESETS.get(role, ROLE_PRESETS["general"])
    profile = AgentProfile(
        **preset.get("profile", AgentProfile()).to_dict()
    ) if isinstance(preset.get("profile"), AgentProfile) else AgentProfile()

    # Apply overrides
    if profile_overrides:
        for k, v in profile_overrides.items():
            if hasattr(profile, k) and v is not None:
                setattr(profile, k, v)

    agent = Agent(
        name=name or preset["name"],
        role=role,
        model=model,
        provider=provider,
        working_dir=working_dir,
        system_prompt=system_prompt or preset["system_prompt"],
        profile=profile,
        node_id=node_id,
        parent_id=parent_id,
        priority_level=priority_level,
        role_title=role_title,
    )
    # If no working_dir provided, default to the agent's own workspace folder.
    # This ensures generated reports/files land under agents/{id}/workspace/
    # instead of leaking into the node's cwd or root.
    if not working_dir:
        try:
            ws = agent._ensure_workspace_layout()
            agent.working_dir = str(ws)
        except Exception:
            pass
    else:
        # User gave an explicit working_dir, still seed the layout (no-op if exists)
        try:
            agent._ensure_workspace_layout()
        except Exception:
            pass
    logger.info("create_agent OK: id=%s name=%s node=%s workspace=%s",
                agent.id, agent.name, agent.node_id, agent.working_dir)
    return agent
