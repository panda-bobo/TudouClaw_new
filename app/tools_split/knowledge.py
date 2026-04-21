"""Knowledge / learning tools — save_experience, knowledge_lookup,
share_knowledge, learn_from_peers.

All four read/write either the role-based Experience Library or the
shared Knowledge Base, and need to resolve caller-agent attribution
through the hub.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from .. import knowledge as _knowledge
from ._common import _get_hub

logger = logging.getLogger(__name__)


# knowledge_lookup: max results per RAG tier before merging.
_RAG_TOP_K = 5

# knowledge_lookup: cap on partial-match list returned to the LLM when
# there's no exact title match.
_PARTIAL_MATCHES_CAP = 20

# learn_from_peers: clamp user-supplied limit to this range.
_LEARN_LIMIT_MIN = 1
_LEARN_LIMIT_MAX = 20


# ── save_experience ──────────────────────────────────────────────────

def _tool_save_experience(
    scene: str,
    core_knowledge: str,
    action_rules: list[str] | None = None,
    taboo_rules: list[str] | None = None,
    priority: str = "medium",
    tags: list[str] | None = None,
    exp_type: str = "retrospective",
    source: str = "",
    role: str = "",
    **ctx: Any,
) -> str:
    """Persist an experience entry into the role-based experience library.

    Skills (installable capability packages) are managed by the Skill
    Registry and are NOT written through this tool. Experiences are
    short, scene-anchored lessons that get auto-injected into the
    relevant role's system prompt.
    """
    try:
        if not scene or not core_knowledge:
            return "Error: 'scene' and 'core_knowledge' are required"

        # Resolve role: explicit arg > calling agent's role > 'default'.
        resolved_role = (role or "").strip()
        if not resolved_role:
            try:
                caller_id = ctx.get("_caller_agent_id", "") if isinstance(ctx, dict) else ""
                if caller_id:
                    hub = _get_hub()
                    agent = hub.get_agent(caller_id) if hub else None
                    if agent is not None:
                        resolved_role = (getattr(agent, "role", "") or "").strip()
            except Exception:
                pass
        if not resolved_role:
            resolved_role = "default"

        # Validate priority.
        pri = (priority or "medium").strip().lower()
        if pri not in ("high", "medium", "low"):
            pri = "medium"

        # Validate exp_type.
        etype = (exp_type or "retrospective").strip().lower()
        if etype not in ("retrospective", "active_learning"):
            etype = "retrospective"

        from ..experience_library import get_experience_library, Experience

        lib = get_experience_library()
        exp = Experience(
            exp_type=etype,
            source=source or "agent.save_experience",
            scene=scene.strip(),
            core_knowledge=core_knowledge.strip(),
            action_rules=[str(r).strip() for r in (action_rules or []) if str(r).strip()],
            taboo_rules=[str(r).strip() for r in (taboo_rules or []) if str(r).strip()],
            priority=pri,
            tags=[str(t).strip() for t in (tags or []) if str(t).strip()],
        )
        saved = lib.add_experience(resolved_role, exp)
        return (
            f"✓ Experience saved: id={saved.id} role={resolved_role} "
            f"priority={saved.priority} scene={saved.scene[:60]}"
        )
    except Exception as e:
        return f"Error saving experience: {e}"


# ── knowledge_lookup ─────────────────────────────────────────────────

def _tool_knowledge_lookup(query: str = "", entry_id: str = "",
                           agent_id: str = "", **kw: Any) -> str:
    """Look up entries in the knowledge base.

    Routing is determined by the agent's rag_mode:
      - "shared"  → query global shared knowledge (default)
      - "private" → query agent's private collection
      - "both"    → query private first, then shared
      - "none"    → return empty

    If entry_id is provided, returns that entry's full content from
    shared KB. Otherwise searches by query using the agent's configured
    RAG routing.
    """
    try:
        # If entry_id is provided, fetch that entry directly from shared KB.
        if entry_id:
            entry = _knowledge.get_entry(entry_id)
            if entry:
                return json.dumps({"status": "success", "entry": entry},
                                  ensure_ascii=False, indent=2)
            return json.dumps({
                "status": "error",
                "message": f"Entry '{entry_id}' not found"
            })

        if not query:
            return json.dumps({
                "status": "error",
                "message": "Either 'query' or 'entry_id' must be provided"
            })

        # --- RAG-routed search ---
        agent_profile = kw.get("_agent_profile")
        rag_mode = "shared"  # default
        if agent_profile:
            rag_mode = getattr(agent_profile, "rag_mode", "shared") or "shared"

        if rag_mode == "none":
            return json.dumps({
                "status": "not_found",
                "message": "RAG is disabled for this agent (rag_mode=none)"
            })

        results_combined = []

        # Private / Both: search via RAG provider registry.
        if rag_mode in ("private", "both") and agent_id:
            try:
                from ..rag_provider import search_for_agent
                rag_results = search_for_agent(agent_profile, query,
                                               agent_id=agent_id,
                                               top_k=_RAG_TOP_K)
                for r in rag_results:
                    results_combined.append({
                        "id": r.get("id", ""),
                        "title": r.get("title", ""),
                        "content": r.get("content", ""),
                        "tags": (r.get("metadata", {}).get("tags", "").split(",")
                                 if r.get("metadata", {}).get("tags") else []),
                        "source": "private_rag",
                    })
            except Exception as e:
                logger.warning("RAG provider search failed: %s", e)

        # Shared / Both: also search the classic shared knowledge base.
        if rag_mode in ("shared", "both"):
            shared_results = _knowledge.search(query)
            for e in shared_results:
                # Avoid duplicates (same ID already from RAG).
                if not any(r["id"] == e["id"] for r in results_combined):
                    results_combined.append(e)

        if not results_combined:
            return json.dumps({
                "status": "not_found",
                "message": f"No knowledge entries found matching '{query}'"
            }, ensure_ascii=False)

        # Check for exact title match.
        query_lower = query.lower().strip()
        for entry in results_combined:
            if entry.get("title", "").lower().strip() == query_lower:
                return json.dumps({"status": "success", "entry": entry},
                                  ensure_ascii=False, indent=2)

        # No exact match — return list for refinement.
        matches = [
            {"id": e.get("id", ""), "title": e.get("title", ""),
             "tags": e.get("tags", []),
             "source": e.get("source", "shared")}
            for e in results_combined[:_PARTIAL_MATCHES_CAP]
        ]

        return json.dumps({
            "status": "partial",
            "message": (f"Found {len(matches)} matching entries. "
                        "Use entry_id to read full content."),
            "matches": matches
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Error querying knowledge base: {str(e)}"
        })


# ── share_knowledge ──────────────────────────────────────────────────

def _tool_share_knowledge(title: str, content: str,
                          tags: list[str] | None = None,
                          **ctx: Any) -> str:
    """Share knowledge with all agents via the shared Knowledge Base."""
    try:
        if not title or not title.strip():
            return "Error: 'title' is required"
        if not content or not content.strip():
            return "Error: 'content' is required"

        # Resolve caller agent info for attribution.
        caller_name = ""
        caller_role = ""
        try:
            caller_id = ctx.get("_caller_agent_id", "") if isinstance(ctx, dict) else ""
            if caller_id:
                hub = _get_hub()
                agent = hub.get_agent(caller_id) if hub else None
                if agent is not None:
                    caller_name = getattr(agent, "name", "") or ""
                    caller_role = getattr(agent, "role", "") or ""
        except Exception:
            pass

        # Add source attribution.
        source_info = ""
        if caller_name or caller_role:
            source_info = f"\n\n---\nShared by: {caller_name} (role: {caller_role})"

        resolved_tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
        if caller_role:
            resolved_tags += ["shared-by-agent", caller_role]

        entry = _knowledge.add_entry(
            title=title.strip(),
            content=content.strip() + source_info,
            tags=resolved_tags,
        )
        return (
            f"Knowledge shared successfully: '{title.strip()}' "
            f"(id: {entry['id']}). "
            f"All agents can now access this via knowledge_lookup."
        )
    except Exception as e:
        return f"Failed to share knowledge: {e}"


# ── learn_from_peers ─────────────────────────────────────────────────

def _tool_learn_from_peers(source_role: str, topic: str = "",
                           limit: int = 5, **ctx: Any) -> str:
    """Learn from other agents' experiences by importing from another role."""
    try:
        if not source_role or not source_role.strip():
            return "Error: 'source_role' is required"

        source_role = source_role.strip()
        limit = max(_LEARN_LIMIT_MIN, min(int(limit), _LEARN_LIMIT_MAX))

        # Resolve caller's role.
        caller_role = ""
        try:
            caller_id = ctx.get("_caller_agent_id", "") if isinstance(ctx, dict) else ""
            if caller_id:
                hub = _get_hub()
                agent = hub.get_agent(caller_id) if hub else None
                if agent is not None:
                    caller_role = getattr(agent, "role", "") or ""
        except Exception:
            pass

        from ..experience_library import _get_global_library
        library = _get_global_library()
        experiences = library.import_cross_role(
            source_role=source_role,
            target_role=caller_role or "default",
            topic=topic.strip() if topic else "",
            limit=limit,
        )
        if not experiences:
            msg = f"No experiences found for role '{source_role}'"
            if topic:
                msg += f" on topic '{topic}'"
            return msg

        lines = [f"Imported {len(experiences)} experiences from role "
                 f"'{source_role}':"]
        for i, exp in enumerate(experiences, 1):
            lines.append(f"\n{i}. [{exp.priority}] {exp.scene}")
            lines.append(f"   Knowledge: {exp.core_knowledge[:200]}")
            if exp.action_rules:
                lines.append(f"   Rules: {'; '.join(exp.action_rules[:3])}")
            lines.append(f"   Success rate: {exp.success_rate:.0%}")
        return "\n".join(lines)
    except Exception as e:
        return f"Failed to learn from peers: {e}"
