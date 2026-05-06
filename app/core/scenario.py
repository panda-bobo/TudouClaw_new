"""Agent scenario context — Day 3 PM (2026-05-05).

A ``Scenario`` captures "what context is this agent operating in right
now":

  * project task   — kind='project',  project_id, task_id, workspace_dir
  * standalone chat — kind='chat',    no project, agent's own workspace
  * canvas run     — kind='canvas',   project + canvas instance
  * general        — kind='general',  no project (e.g. cron job, REPL)

The dispatcher (project handler / canvas runner / chat endpoint /
cron) sets ``agent.current_scenario = Scenario(...)`` BEFORE calling
``agent.chat()``. The agent uses it for:

  1. Memory recall scope filter (auto-derived from the scenario)
  2. Sandbox ``allowed_dirs`` (workspace_dir is always allowed)
  3. System prompt injection (a ``<current_scenario>`` block tells the
     model where it is)
  4. L1 short-term memory clearing on scenario switch (so a chat from
     project A doesn't bleed into project B)

This is the single source of truth for "context" — replacing the
scattered ``getattr(self, 'project_id', '')`` reads strewn throughout
the codebase. Old call sites still work (project_id remains on Agent),
but new code should read from current_scenario.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Scenario:
    """Immutable snapshot of the agent's current operating context."""

    kind: str = "general"               # project / chat / canvas / general
    project_id: str = ""
    project_name: str = ""
    task_id: str = ""
    task_title: str = ""
    workspace_dir: str = ""             # absolute path, may be empty
    extras: dict = field(default_factory=dict)  # free-form metadata

    @property
    def signature(self) -> str:
        """Stable hash that changes when the agent moves to a different
        scenario. Used to detect context switches and clear L1."""
        h = hashlib.sha1()
        h.update(self.kind.encode())
        h.update(b"|")
        h.update(self.project_id.encode())
        h.update(b"|")
        h.update(self.task_id.encode())
        return h.hexdigest()[:16]

    def scope_filter(self, agent_id: str) -> list[str]:
        """Return the set of memory scopes that should be visible from
        this scenario. Always includes 'global' and the per-agent scope.

        Used by the recall layer to filter SemanticFact.scope. Cross-
        project / cross-task entries are excluded.
        """
        scopes = ["global", f"agent:{agent_id}"]
        if self.project_id:
            scopes.append(f"project:{self.project_id}")
        if self.task_id:
            scopes.append(f"task:{self.task_id}")
        return scopes

    def to_prompt_block(self) -> str:
        """Render as a system_prompt block the model can read."""
        lines = ["<current_scenario>"]
        lines.append(f"  kind: {self.kind}")
        if self.project_id:
            disp = self.project_name or self.project_id[:8]
            lines.append(f"  project: {self.project_id}  \"{disp}\"")
        if self.task_id:
            disp = self.task_title or self.task_id[:8]
            lines.append(f"  task: {self.task_id}  \"{disp}\"")
        if self.workspace_dir:
            lines.append(f"  workspace: {self.workspace_dir}")
        lines.append("</current_scenario>")
        # Recall policy hint — explains scope_filter to the agent so
        # it knows it WON'T see cross-project memories.
        if self.project_id or self.task_id:
            lines.append("<recall_policy>")
            scope_short = "global ∪ agent:self"
            if self.project_id:
                scope_short += f" ∪ project:{self.project_id[:8]}"
            if self.task_id:
                scope_short += f" ∪ task:{self.task_id[:8]}"
            lines.append(f"  Memory recall scope: {scope_short}")
            lines.append("  Cross-project memories are NOT visible.")
            lines.append("</recall_policy>")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "project_id": self.project_id,
            "project_name": self.project_name,
            "task_id": self.task_id,
            "task_title": self.task_title,
            "workspace_dir": self.workspace_dir,
            "extras": dict(self.extras or {}),
        }

    # ── Constructors ──

    @classmethod
    def general(cls) -> "Scenario":
        return cls(kind="general")

    @classmethod
    def for_chat(cls, agent_id: str, workspace_dir: str = "") -> "Scenario":
        return cls(kind="chat", workspace_dir=workspace_dir,
                   extras={"chat_agent_id": agent_id})

    @classmethod
    def for_project(cls, project_id: str, project_name: str = "",
                    workspace_dir: str = "",
                    task_id: str = "", task_title: str = "") -> "Scenario":
        return cls(kind="project",
                   project_id=project_id, project_name=project_name,
                   task_id=task_id, task_title=task_title,
                   workspace_dir=workspace_dir)

    @classmethod
    def for_canvas(cls, project_id: str, instance_id: str,
                   project_name: str = "", workspace_dir: str = "") -> "Scenario":
        return cls(kind="canvas",
                   project_id=project_id, project_name=project_name,
                   workspace_dir=workspace_dir,
                   extras={"canvas_instance": instance_id})


def set_agent_scenario(agent: Any, scenario: Scenario) -> bool:
    """Assign a scenario to an agent. If the signature differs from
    the previous scenario, clear L1 short-term memory (the running
    conversation buffer) so chat from a previous context doesn't bleed
    in. Returns True when L1 was cleared.
    """
    prev = getattr(agent, "current_scenario", None)
    prev_sig = prev.signature if prev is not None else ""
    try:
        agent.current_scenario = scenario
    except Exception:
        return False
    if prev_sig and prev_sig != scenario.signature:
        # Clear L1 short-term — keep system / first-user message if
        # the agent is mid-turn (rare). For simplicity: clear the
        # running messages list except for any system anchor.
        try:
            msgs = list(getattr(agent, "messages", []) or [])
            kept = [m for m in msgs if m.get("role") == "system"
                    and m.get("_source") in ("anchor", "system_anchor")]
            agent.messages = kept
            return True
        except Exception:
            return False
    return False
