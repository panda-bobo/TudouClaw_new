"""Action handler registry — maps {"type": "..."} dicts to side effects.

Built-in action types:
  deny             — terminal; caller short-circuits with the message
  warn             — caller still proceeds; engine logs to audit
  log              — pure audit, no behavior change
  rewrite_arg      — caller mutates an arg via named transform
  require_approval — caller routes through ToolPolicy.request_approval
  side_effect      — caller invokes a registered named handler

The engine itself only KNOWS about built-in types; everything else
(approval routing, side-effect execution) happens at the caller because
those depend on per-PEP context (which arg, which agent, which auth
session).

Transforms (for rewrite_arg) and side-effects are NAMED entries in
registries declared here. Adding a new transform means registering it
here, not opening up arbitrary code execution to rule authors.
"""
from __future__ import annotations

import os
from typing import Any, Callable

# ── Transform registry — named functions the rule engine can apply ──
# Signature: transform(value, context, config) -> new_value
# Caller (PEP) is responsible for actually mutating its argument list.
_TRANSFORMS: dict[str, Callable[[Any, dict, dict], Any]] = {}

# ── Side-effect registry — named handlers triggered by side_effect action ──
# Signature: handler(context, config) -> None (or returns dict for audit)
_SIDE_EFFECTS: dict[str, Callable[[dict, dict], Any]] = {}


def register_transform(name: str, fn: Callable[[Any, dict, dict], Any]) -> None:
    """Register a named transform. Idempotent (re-register replaces)."""
    _TRANSFORMS[name] = fn


def register_side_effect(name: str, fn: Callable[[dict, dict], Any]) -> None:
    """Register a named side-effect handler."""
    _SIDE_EFFECTS[name] = fn


def get_transform(name: str) -> Callable | None:
    return _TRANSFORMS.get(name)


def get_side_effect(name: str) -> Callable | None:
    return _SIDE_EFFECTS.get(name)


def list_transforms() -> list[str]:
    return sorted(_TRANSFORMS.keys())


def list_side_effects() -> list[str]:
    return sorted(_SIDE_EFFECTS.keys())


# ── Built-in transforms ─────────────────────────────────────────────

def _t_into_agent_subdir(value: Any, context: dict, config: dict) -> Any:
    """Rewrite a path so the basename lands inside the calling agent's
    own subdir under the project workspace. ``value`` is the original
    path; if already inside the agent dir, returns unchanged.

    Config: {"workspace_field": "scope.workspace"} or hardcoded fallback.
    """
    if not isinstance(value, str):
        return value
    workspace = ""
    ws_field = config.get("workspace_field") or "scope.workspace"
    # Walk dotted path
    cursor: Any = context
    for part in ws_field.split("."):
        if isinstance(cursor, dict):
            cursor = cursor.get(part)
        else:
            cursor = None
            break
    if isinstance(cursor, str):
        workspace = cursor.rstrip("/")
    agent = (context.get("agent") or {})
    role = agent.get("role") or "general"
    name = agent.get("name") or "agent"
    subdir = f"{role}-{name}"
    if not workspace:
        return value
    # Already in agent dir? leave alone.
    if value.startswith(f"{workspace}/{subdir}/"):
        return value
    basename = os.path.basename(value)
    if not basename:
        return value
    return f"{workspace}/{subdir}/{basename}"


def _t_truncate(value: Any, context: dict, config: dict) -> Any:
    """Truncate a string to N chars. Config: {"max_len": 1000}."""
    if not isinstance(value, str):
        return value
    n = int(config.get("max_len") or 1000)
    return value[:n]


def _t_lowercase(value: Any, context: dict, config: dict) -> Any:
    return value.lower() if isinstance(value, str) else value


# Register built-ins
register_transform("into_agent_subdir", _t_into_agent_subdir)
register_transform("truncate", _t_truncate)
register_transform("lowercase", _t_lowercase)


# ── Built-in side-effects ───────────────────────────────────────────
# These are stubs — full implementations live alongside the relevant
# subsystems (e.g. auto_register_deliverable in tools_split/fs.py).
# Registering early handlers here keeps the engine self-contained for
# tests; production code re-registers richer versions at hub boot.

def _se_noop(context: dict, config: dict) -> dict:
    """No-op side effect, used for tests + as a safe fallback."""
    return {"noop": True}


register_side_effect("noop", _se_noop)
