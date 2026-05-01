"""
Agent type definitions — enums, dataclasses, and data models.

Extracted from agent.py to reduce file size and improve modularity.
"""
from __future__ import annotations
import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum


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
    started_at: float = 0.0
    completed_at: float = 0.0
    result_summary: str = ""      # brief result after completion
    # LLM routing hint — which category best fits this step's work.
    # Filled by the primary LLM when it calls plan_update(create_plan)
    # (with the scores table injected into its system prompt, so the
    # decision is informed). Read by the per-iteration LLM resolver to
    # override keyword-based detection. Empty = fall back to detection.
    # Valid values: tool-heavy | multimodal | reasoning | analysis | coding | default
    llm_purpose: str = ""
    llm_rationale: str = ""       # short sentence explaining the choice (optional)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title,
            "detail": self.detail,
            "status": self.status.value,
            "order": self.order,
            "parent_step_id": self.parent_step_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result_summary": self.result_summary,
            "llm_purpose": self.llm_purpose,
            "llm_rationale": self.llm_rationale,
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
            started_at=d.get("started_at", 0),
            completed_at=d.get("completed_at", 0),
            result_summary=d.get("result_summary", ""),
            llm_purpose=str(d.get("llm_purpose") or ""),
            llm_rationale=str(d.get("llm_rationale") or ""),
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
        # Encrypt sensitive env values (PASSWORD / TOKEN / API_KEY / ...)
        # before they touch disk. Idempotent — already-encrypted values
        # pass through untouched. See app.mcp.secrets for the threat
        # model (2026-04-29: an agent picked up plaintext SMTP creds
        # from the MCP config and self-sent email via smtplib).
        try:
            from .mcp.secrets import encrypt_env_dict
            _env_persisted = encrypt_env_dict(self.env or {})
        except Exception:
            _env_persisted = self.env or {}
        return {
            "id": self.id, "name": self.name,
            "transport": self.transport,
            "command": self.command,
            "url": self.url,
            "env": _env_persisted,
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

    def __post_init__(self):
        # Defensive: any list-typed field passed as ``None`` (legacy
        # saved JSON, preset round-trip via to_dict→AgentProfile(**d),
        # explicit ``None`` from form input) → coerce to ``[]``. Without
        # this guard, ``list(profile.allowed_tools)`` crashes downstream
        # when re-saving the profile from another agent's update.
        for _f in (
            "rag_collection_ids", "expertise", "skills",
            "allowed_tools", "denied_tools", "auto_approve_tools",
            "exec_blacklist", "exec_whitelist", "mcp_servers",
            "sandbox_allow_commands", "skill_capabilities",
        ):
            if getattr(self, _f, None) is None:
                setattr(self, _f, [])

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
            # NB: ``or []`` (not just ``, []``) — saved JSON may contain
            # ``"allowed_tools": null`` for older agents, and dict.get's
            # default only fires when the KEY is absent, not when the
            # VALUE is None. Without ``or []`` we'd hand back None and
            # downstream ``list(profile.allowed_tools)`` crashes.
            rag_collection_ids=d.get("rag_collection_ids") or [],
            personality=d.get("personality", "helpful"),
            communication_style=d.get("communication_style", "technical"),
            expertise=d.get("expertise") or [],
            skills=d.get("skills") or [],
            language=d.get("language", "auto"),
            max_context_messages=d.get("max_context_messages", 50),
            allowed_tools=d.get("allowed_tools") or [],
            denied_tools=d.get("denied_tools") or [],
            auto_approve_tools=d.get("auto_approve_tools") or [],
            temperature=d.get("temperature", 0.7),
            custom_instructions=d.get("custom_instructions", ""),
            exec_policy=d.get("exec_policy", "ask"),
            exec_blacklist=d.get("exec_blacklist") or [],
            exec_whitelist=d.get("exec_whitelist") or [],
            mcp_servers=mcp_servers,
            sandbox_mode=d.get("sandbox_mode", ""),
            sandbox_allow_commands=d.get("sandbox_allow_commands") or [],
            skill_capabilities=d.get("skill_capabilities") or [],
        )


@dataclass
class AgentEvent:
    timestamp: float
    kind: str   # message | tool_call | tool_result | error | delegate | status | approval
    data: dict

    def to_dict(self) -> dict:
        return {"timestamp": self.timestamp, "kind": self.kind, "data": self.data}
