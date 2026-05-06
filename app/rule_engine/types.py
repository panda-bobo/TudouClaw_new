"""Core dataclasses for the rule engine."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


# ── Scope kinds — the four worlds an agent operates in ──
# global  — applies everywhere, no further filter
# project — applies inside a project chat (scope_targets = project_ids or ["*"])
# meeting — applies inside a meeting (scope_targets = meeting_ids or ["*"])
# solo    — applies in 1:1 user↔agent chat (scope_targets = agent_ids or ["*"])
SCOPE_KINDS = ("global", "project", "meeting", "solo")


# ── Triggers — the decision points where the engine evaluates ──
# Naming convention: before_X = can deny/rewrite; after_X = post-hoc (log/side_effect).
# This list is the source of truth for what PEPs the codebase wires.
TRIGGERS = (
    "before_tool_call",          # agent.py — agent about to invoke a tool
    "after_tool_call",           # agent.py — tool returned (success or error)
    "before_file_write",         # tools_split/fs.py — write_file / edit_file
    "before_task_done",          # project.py — task transition to DONE
    "before_task_assign",        # project.py — dispatching task to agent
    "before_milestone_done",     # project.py — milestone status flip
    "before_message_send",       # project.py / meeting.py — chat post
    "before_skill_invoke",       # agent.py — skill dispatch
    "before_step_complete",      # workflow execution — step transition
    "before_dispatch_task",      # tools_split/coordination.py — dispatch_task tool
    "before_approval_decide",    # auth.py — admin approve/deny
    "on_event_observed",         # hub.py — generic observation (background)
)


# ── Action types — the verbs the engine can apply when a rule matches ──
ActionType = str  # one of:
#   "deny"            — short-circuit, return error message to caller
#   "warn"            — inject system warning (caller continues)
#   "require_approval" — route through ToolPolicy.request_approval
#   "rewrite_arg"     — transform an argument via named function
#   "log"             — append to audit log only (no side effect)
#   "side_effect"     — invoke a registered side-effect handler
ACTION_TYPES = (
    "deny", "warn", "require_approval",
    "rewrite_arg", "log", "side_effect",
)


@dataclass
class RuleScope:
    """Where a rule applies. Engine filters rule set by current context."""
    kind: str                              # one of SCOPE_KINDS
    targets: list[str] = field(default_factory=lambda: ["*"])

    def matches(self, ctx_scope: dict) -> bool:
        """Does this rule's scope cover the current request context?

        ``ctx_scope`` shape: {"kind": "...", "project_id"?, "meeting_id"?, "agent_id"?}
        """
        if self.kind == "global":
            return True
        if self.kind != ctx_scope.get("kind"):
            return False
        if "*" in self.targets:
            return True
        if self.kind == "project":
            return ctx_scope.get("project_id") in self.targets
        if self.kind == "meeting":
            return ctx_scope.get("meeting_id") in self.targets
        if self.kind == "solo":
            return ctx_scope.get("agent_id") in self.targets
        return False

    def to_dict(self) -> dict:
        return {"kind": self.kind, "targets": list(self.targets)}

    @staticmethod
    def from_dict(d: dict) -> "RuleScope":
        return RuleScope(
            kind=str(d.get("kind") or "global"),
            targets=[str(x) for x in (d.get("targets") or ["*"])],
        )


@dataclass
class Rule:
    """A single declarative rule.

    condition: JSON DSL expression evaluated against the trigger context.
               See condition.py for grammar. Empty dict = always match.
    actions:   ordered list of {"type": ..., ...config} dicts. Engine
               applies in order; "deny" short-circuits the whole rule chain.
    """
    id: str = field(default_factory=lambda: "r_" + uuid.uuid4().hex[:10])
    name: str = ""
    description: str = ""
    scope: RuleScope = field(default_factory=lambda: RuleScope("global"))
    trigger: str = ""
    condition: dict = field(default_factory=dict)
    actions: list[dict] = field(default_factory=list)
    enabled: bool = True
    priority: int = 0
    created_by: str = ""
    created_at: float = field(default_factory=time.time)
    revision: int = 1
    revision_note: str = ""
    # Source is used by migrators to mark rules they generated (so a
    # re-run can replace them without clobbering admin-authored ones).
    source: str = "admin"   # "admin" | "migrator:<name>" | "system:default"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "scope": self.scope.to_dict(),
            "trigger": self.trigger,
            "condition": dict(self.condition or {}),
            "actions": [dict(a) for a in (self.actions or [])],
            "enabled": bool(self.enabled),
            "priority": int(self.priority),
            "created_by": self.created_by,
            "created_at": self.created_at,
            "revision": self.revision,
            "revision_note": self.revision_note,
            "source": self.source,
        }

    @staticmethod
    def from_dict(d: dict) -> "Rule":
        return Rule(
            id=str(d.get("id") or ("r_" + uuid.uuid4().hex[:10])),
            name=str(d.get("name") or ""),
            description=str(d.get("description") or ""),
            scope=RuleScope.from_dict(d.get("scope") or {}),
            trigger=str(d.get("trigger") or ""),
            condition=dict(d.get("condition") or {}),
            actions=[dict(a) for a in (d.get("actions") or [])],
            enabled=bool(d.get("enabled", True)),
            priority=int(d.get("priority", 0) or 0),
            created_by=str(d.get("created_by") or ""),
            created_at=float(d.get("created_at") or time.time()),
            revision=int(d.get("revision", 1) or 1),
            revision_note=str(d.get("revision_note") or ""),
            source=str(d.get("source") or "admin"),
        )


@dataclass
class Decision:
    """One rule's verdict for the current evaluation pass.

    The engine returns a list of these (one per matching rule). Caller
    inspects them in order and applies short-circuit logic for "deny".
    """
    rule_id: str
    rule_name: str
    action: ActionType
    matched: bool                    # did the condition evaluate True?
    message: str = ""
    config: dict = field(default_factory=dict)   # action-specific payload
    evidence: dict = field(default_factory=dict) # which clauses matched (for audit)
    error: str = ""                  # populated if rule eval itself failed

    @property
    def is_terminal(self) -> bool:
        """True if the caller should stop processing further rules /
        actions after this one (e.g. deny short-circuits)."""
        return self.action == "deny" and self.matched
