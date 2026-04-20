"""
AgentV2 — thin shell that declares capabilities (PRD §6.5).

Differences from V1 agent:
    - No ``chat()`` method. Conversation is a task too (template_id="conversation").
    - No ``messages`` field. Messages live on the Task's ``context``.
    - Capability binding happens at Task start (snapshot) — PRD §7.3.
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from ..core.task import Task, TaskPhase, TaskStatus


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


@dataclass
class Capabilities:
    """Declarative capability set. Bound to concrete tools at Task start."""
    skills: list[str] = field(default_factory=list)        # skill_id list
    mcps: list[str] = field(default_factory=list)          # mcp_binding_id list
    tools: list[str] = field(default_factory=list)         # built-in tool names
    llm_tier: str = "default"                              # resolver decides model
    denied_tools: list[str] = field(default_factory=list)  # hard blocklist


@dataclass
class AgentV2:
    id: str
    name: str
    role: str
    v1_agent_id: str = ""
    capabilities: Capabilities = field(default_factory=Capabilities)
    task_template_ids: list[str] = field(default_factory=list)
    working_directory: str = ""
    created_at: float = 0.0
    archived: bool = False

    # ── identity ───────────────────────────────────────────────────────

    @classmethod
    def create(
        cls,
        name: str,
        role: str,
        *,
        id: str = "",
        capabilities: Optional[Capabilities] = None,
        task_template_ids: Optional[list[str]] = None,
        working_directory: str = "",
        v1_agent_id: str = "",
    ) -> "AgentV2":
        """Create a V2 agent shell.

        When ``id`` is supplied, the shell adopts it verbatim — used when
        pairing a V2 shell with an existing V1 agent so both systems
        address the same agent_id. When omitted, a new ``av2_*`` id is
        generated for V2-only agents.
        """
        agent = cls(
            id=(id.strip() if id else _new_id("av2")),
            name=name,
            role=role,
            v1_agent_id=v1_agent_id,
            capabilities=capabilities or Capabilities(),
            task_template_ids=list(task_template_ids or []),
            working_directory=working_directory,
            created_at=time.time(),
        )
        # Ensure workspace directory exists so artifact paths resolve.
        if not agent.working_directory:
            from app import DEFAULT_DATA_DIR
            agent.working_directory = os.path.join(
                DEFAULT_DATA_DIR, "v2", "agents", agent.id, "workspace"
            )
        os.makedirs(agent.working_directory, exist_ok=True)
        return agent

    # ── capabilities snapshot (PRD §7.3) ───────────────────────────────

    def capabilities_snapshot(self) -> dict:
        """Freeze current capabilities + versions for a new Task.

        Returns an immutable dict. Skill / MCP version lookups go through
        bridges; if a bridge isn't available yet we still record the ids
        so the snapshot structure is stable.
        """
        snap: dict = {
            "skills": [{"id": sid, "version": ""} for sid in self.capabilities.skills],
            "mcps":   [{"id": mid, "binding_id": ""} for mid in self.capabilities.mcps],
            "tools":  list(self.capabilities.tools),
            "llm_tier": self.capabilities.llm_tier,
            "denied_tools": list(self.capabilities.denied_tools),
            "frozen_at": time.time(),
        }
        # Best-effort enrichment via bridges (quietly no-op if missing).
        try:
            from ..bridges import skill_bridge
            versions = skill_bridge.versions_for(self.id, self.capabilities.skills)
            for s, v in zip(snap["skills"], versions):
                s["version"] = v
        except Exception:
            pass
        try:
            from ..bridges import mcp_bridge
            bindings = mcp_bridge.bindings_for(self.id, self.capabilities.mcps)
            for m, b in zip(snap["mcps"], bindings):
                m["binding_id"] = b
        except Exception:
            pass
        return snap

    # ── task submission (PRD §10.4.1) ──────────────────────────────────

    def submit_task(
        self,
        intent: str,
        *,
        template_id: str = "",
        parent_task_id: str = "",
        priority: int = 5,
        timeout_s: int = 1800,
        attachments: list[dict] | None = None,
        store=None,
        bus=None,
        run_in_background: bool = True,
    ) -> Task:
        """Create a Task and kick off a TaskLoop. Returns immediately.

        ``store`` and ``bus`` default to the process singletons
        (``v2.core.task_store.get_store``, a shared bus) — callers in
        tests can inject fakes.
        """
        from ..core.task_store import get_store
        from ..core.task_events import TaskEventBus
        from ..core.task_loop import TaskLoop

        store = store or get_store()
        bus = bus or _get_shared_bus(store)

        tmpl_id = template_id or self._default_template_for(intent)
        now = time.time()

        # Concurrency policy: one RUNNING/PAUSED task per agent at a
        # time. Additional submissions go into a FIFO queue; they'll be
        # promoted to RUNNING by ``task_controller.dispatch_next_queued``
        # when the current task finalises. Subtasks (``parent_task_id``
        # set) bypass the queue — a parent can spawn children that run
        # in parallel with itself.
        is_subtask = bool(parent_task_id)
        busy = (not is_subtask) and (store.count_active_tasks(self.id) > 0)
        initial_status = TaskStatus.QUEUED if busy else TaskStatus.RUNNING

        task = Task(
            id=_new_id("t"),
            agent_id=self.id,
            parent_task_id=parent_task_id,
            template_id=tmpl_id,
            intent=intent,
            phase=TaskPhase.INTAKE,
            status=initial_status,
            priority=int(priority),
            timeout_s=int(timeout_s),
            created_at=now,
            updated_at=now,
        )
        task.context.capabilities_snapshot = self.capabilities_snapshot()
        if attachments:
            task.context.attachments = list(attachments)
        store.save(task)

        # Critical event: submission is persisted synchronously.
        bus.publish(task.id, TaskPhase.INTAKE, "task_submitted", {
            "intent": intent,
            "template_id": tmpl_id,
            "priority": priority,
            "timeout_s": timeout_s,
            "queued": busy,
            "parent_task_id": parent_task_id,
            "attachment_count": len(task.context.attachments),
        })

        # Only start the loop if the task is actually RUNNING. QUEUED
        # tasks sit in the DB until the current one finalises; the
        # dispatcher wakes them up then.
        if initial_status == TaskStatus.RUNNING:
            loop = TaskLoop(task=task, agent=self, bus=bus, store=store, template=None)
            if run_in_background:
                th = threading.Thread(
                    target=loop.run,
                    name=f"TaskLoop-{task.id}",
                    daemon=True,
                )
                th.start()
            else:
                loop.run()
        return task

    # ── helpers ────────────────────────────────────────────────────────

    def _default_template_for(self, intent: str) -> str:
        """Pick a default template — keyword match, else 'conversation'."""
        if not self.task_template_ids:
            return "conversation"
        # Prefer a bound template if intent hints at it; else first bound id.
        lowered = (intent or "").lower()
        for tid in self.task_template_ids:
            if tid and tid.lower() in lowered:
                return tid
        return self.task_template_ids[0]

    # ── clone from V1 (PRD §13.4) ──────────────────────────────────────

    @classmethod
    def clone_from_v1(cls, v1_agent_id: str, hub=None, store=None) -> "AgentV2":
        """Shallow clone of V1 identity + capabilities.

        Migrates (explicit allowlist — PRD §7.6 铁律 D2: V2 is a
        fresh-context shell):

            * ``name``
            * ``role``
            * ``granted_skills``               → capabilities.skills
            * effective MCP ids (via V1 MCP manager) → capabilities.mcps

        Does NOT migrate — reasoning:

            * ``messages`` / ``events`` / ``transcript`` / ``cost_tracker``
              — V2 owns runtime state per-Task, not per-Agent (D2).
            * ``system_prompt`` / ``soul_md`` / ``profile``
              — V2 agents behave via capabilities, not baked prompts.
            * ``working_dir`` — V2 assigns its own workspace.
            * ``extra_llms`` / ``auto_route`` / ``multimodal_*`` / ``coding_*``
              — V2 uses the tier system (capabilities.llm_tier) instead.
            * ``priority_level`` / ``role_title`` / ``channel_ids`` /
              ``authorized_workspaces`` / ``parent_id`` / ``project_id``
              — V1-specific concepts with no V2 counterpart.

        Raises:
            KeyError: V1 agent not found.
        """
        # V1 hub lookup — Layer-1 shared service access.
        if hub is None:
            from app.hub._core import get_hub
            hub = get_hub()
        v1_agent = hub.get_agent(v1_agent_id)
        if v1_agent is None:
            raise KeyError(f"V1 agent {v1_agent_id!r} not found")

        # Effective MCP ids — best effort.
        mcp_ids: list[str] = []
        try:
            from app.mcp import manager as _mgr
            mcps = _mgr.get_mcp_manager().get_agent_effective_mcps(
                getattr(hub, "node_id", "local"), v1_agent_id,
            )
            mcp_ids = [m.id for m in mcps if getattr(m, "id", "")]
        except Exception:
            pass

        caps = Capabilities(
            skills=list(getattr(v1_agent, "granted_skills", []) or []),
            mcps=mcp_ids,
            tools=[],
            llm_tier="default",
            denied_tools=[],
        )

        agent = cls.create(
            name=getattr(v1_agent, "name", "cloned"),
            role=getattr(v1_agent, "role", "general"),
            capabilities=caps,
            v1_agent_id=v1_agent_id,
        )
        if store is not None:
            store.save_agent(agent)
        return agent


# ── shared singleton bus (lazy) ─────────────────────────────────────────

_SHARED_BUS = None
_BUS_LOCK = threading.Lock()


def _get_shared_bus(store):
    from ..core.task_events import TaskEventBus
    global _SHARED_BUS
    with _BUS_LOCK:
        if _SHARED_BUS is None:
            _SHARED_BUS = TaskEventBus(store)
        return _SHARED_BUS


__all__ = ["AgentV2", "Capabilities"]
