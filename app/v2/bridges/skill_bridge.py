"""
skill_bridge — V2 adapter for ``app.skills.engine`` (PRD §10.5.2).

V2 never caches ``granted_skills`` locally (that was V1 root cause #3).
Every lookup round-trips through the shared registry so a revoke
mid-session is honored on the next read.

Skill manifests aren't OpenAI tool schemas, so this module also houses
the one-and-only manifest → tool-schema converter used by V2.
"""
from __future__ import annotations

from typing import Any

# V1 Layer-1 imports — explicitly allowed by isolation check (PRD §13.1).
from app.skills import engine as _sk


# ── public API ─────────────────────────────────────────────────────────

def get_skill_tools_for_agent(agent_v2_id: str) -> list[dict]:
    """Return OpenAI tool schemas for every skill granted to this agent."""
    try:
        installs = _sk.get_registry().list_for_agent(agent_v2_id)
    except Exception:
        return []
    out: list[dict] = []
    for inst in installs:
        try:
            out.append(_manifest_to_tool_schema(inst.manifest))
        except Exception:
            # Malformed manifest shouldn't break the whole tool list.
            continue
    return out


def invoke_skill(agent_v2_id: str, skill_id: str, args: dict) -> str:
    """Execute a skill; return result coerced to string.

    Raises the same PermissionError / KeyError the registry raises so
    the caller (TaskExecutor) can distinguish "not granted" from
    "bad args" from "runtime crash".
    """
    result = _sk.get_registry().invoke(skill_id, agent_v2_id, dict(args or {}))
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    # Structured results: stringify via JSON so downstream token renderers
    # can still parse them. Fall through to str() on non-serializable types.
    try:
        import json
        return json.dumps(result, ensure_ascii=False)
    except Exception:
        return str(result)


def versions_for(agent_v2_id: str, skill_ids: list[str]) -> list[str]:
    """Return current registry version for each skill id, ordered. Missing → ''."""
    reg = _sk.get_registry()
    out: list[str] = []
    for sid in skill_ids:
        try:
            inst = reg.get(sid)
            out.append(inst.manifest.version if inst else "")
        except Exception:
            out.append("")
    return out


# ── manifest → OpenAI tool schema ──────────────────────────────────────

_TYPE_MAP = {
    "string": "string", "str": "string",
    "int": "integer", "integer": "integer",
    "float": "number", "number": "number",
    "bool": "boolean", "boolean": "boolean",
    "list": "array", "array": "array",
    "dict": "object", "object": "object",
}


def _manifest_to_tool_schema(manifest) -> dict:
    """Convert a SkillManifest into an OpenAI function-tool schema."""
    properties: dict[str, dict] = {}
    required: list[str] = []
    for inp in getattr(manifest, "inputs", []) or []:
        schema: dict[str, Any] = {
            "type": _TYPE_MAP.get((inp.type or "string").lower(), "string"),
            "description": inp.description or "",
        }
        if inp.enum:
            schema["enum"] = list(inp.enum)
        properties[inp.name] = schema
        if inp.required:
            required.append(inp.name)

    return {
        "type": "function",
        "function": {
            # Registry uses ``name@version`` as the invoke key (``manifest.id``);
            # expose that as the tool name so dispatch is unambiguous.
            "name": manifest.id or manifest.name,
            "description": manifest.get_description("zh-CN") or manifest.description or "",
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


__all__ = [
    "get_skill_tools_for_agent",
    "invoke_skill",
    "versions_for",
]
