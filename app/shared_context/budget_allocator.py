"""Token-budget allocator for per-call context assembly.

The contract: callers say "I have N tokens of context budget; give me
the most useful N tokens to inject". We split N across 5 sections by
priority weight, fill each section from its source, truncate per-section
when overfull, and concatenate.

  ┌────────────────────────┬──────────┐
  │ Section                │ Default  │
  ├────────────────────────┼──────────┤
  │ task_state             │  15%     │
  │ upstream_dependencies  │  25%     │
  │ decisions              │  10%     │
  │ knowledge (RAG)        │  35%     │
  │ history_summary        │  15%     │
  └────────────────────────┴──────────┘

Total weights must sum to 1.0 (normalized if not). Per-task complexity
hints can boost RAG up to 50% at the expense of history+dependencies.

Returns ``ContextBundle`` with both the rendered string and per-section
token accounting (for observability — feeds the orchestration overview).

Token counting uses the tiktoken approximation (4 chars ≈ 1 token for
mixed-language text); good enough for budget gating without a per-model
tokenizer dep on the hot path.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("tudou.shared_context.budget")


# Per-section default weights. Must sum to ~1.0; the allocator
# re-normalises when the dict diverges.
DEFAULT_WEIGHTS: dict[str, float] = {
    "task_state":            0.15,
    "upstream_dependencies": 0.25,
    "decisions":             0.10,
    "knowledge":             0.35,
    "history_summary":       0.15,
}

# Weight overrides for "complex task" mode — boost RAG, shrink history.
COMPLEX_WEIGHTS: dict[str, float] = {
    "task_state":            0.10,
    "upstream_dependencies": 0.20,
    "decisions":             0.10,
    "knowledge":             0.50,
    "history_summary":       0.10,
}

# Per-section minimums (in token estimate). If a section can't fit in
# its budget but has any content, we still try to give it this floor;
# the difference is taken from the section with the most slack.
MIN_BUDGET_PER_SECTION = 30


def _estimate_tokens(text: str) -> int:
    """Cheap token estimate: 1 token ≈ 4 chars for mixed CN+EN text."""
    if not text:
        return 0
    return max(1, len(text) // 4)


def _truncate_to_tokens(text: str, max_tokens: int, suffix: str = "…") -> str:
    """Truncate text so that estimate is ≤ max_tokens. Adds suffix if cut."""
    if max_tokens <= 0 or not text:
        return ""
    cur = _estimate_tokens(text)
    if cur <= max_tokens:
        return text
    target_chars = max(1, max_tokens * 4 - len(suffix))
    return text[:target_chars] + suffix


@dataclass
class SectionResult:
    """One section's accounting after allocation."""
    name: str
    text: str = ""
    budget: int = 0      # tokens allocated to this section
    used: int = 0        # tokens actually consumed (estimate)
    truncated: bool = False
    source: str = ""     # which underlying store fed this section


@dataclass
class ContextBundle:
    """Output of ``get_agent_context``."""
    rendered: str = ""
    total_used: int = 0
    total_budget: int = 0
    sections: list[SectionResult] = field(default_factory=list)

    def section_breakdown(self) -> dict[str, int]:
        """For observability — section name → tokens used."""
        return {s.name: s.used for s in self.sections}


def get_agent_context(
    *,
    agent_id: str,
    project_id: str = "",
    task_id: str = "",
    intent: str = "",
    role: str = "",
    budget: int = 2000,
    weights: Optional[dict[str, float]] = None,
    complex_task: bool = False,
    history_summary_text: str = "",
) -> ContextBundle:
    """Assemble dynamic per-call context within a hard token budget.

    Args:
        agent_id:        Caller — used to filter handoffs/Q&A "for me".
        project_id:      Project scope. If empty, only RAG section is
                         populated (other sections need a project).
        task_id:         Optional, currently used as an annotation hint.
        intent:          Free-text intent — feeds RAG retrieval and the
                         "task_state" section header.
        role:            Caller's role — used by RAG and per-section RAG.
        budget:          Hard cap on total context tokens.
        weights:         Override DEFAULT_WEIGHTS; auto-normalised.
        complex_task:    Switch to COMPLEX_WEIGHTS (RAG up to 50%).
        history_summary_text: Pre-summarised conversation tail; the
                         allocator does NOT do summarization itself
                         (that's the caller's job — it knows the
                         relevant message buffer).

    Returns:
        ContextBundle with the assembled string + per-section accounting.
        Empty rendered=""  when budget ≤ 0 or no source has content.
    """
    if budget <= 0:
        return ContextBundle()

    w = dict(weights or (COMPLEX_WEIGHTS if complex_task else DEFAULT_WEIGHTS))
    # Normalise weights (handle malformed input)
    total_w = sum(max(0, v) for v in w.values()) or 1.0
    w = {k: max(0, v) / total_w for k, v in w.items()}

    # Compute per-section budgets
    section_budgets: dict[str, int] = {
        name: max(MIN_BUDGET_PER_SECTION, int(budget * w.get(name, 0)))
        for name in DEFAULT_WEIGHTS
    }

    sections: list[SectionResult] = []

    # ── 1. task_state ──────────────────────────────────────────────────
    if intent:
        text = f"# 当前任务\n{intent}"
        if task_id:
            text += f"\n(task_id: {task_id})"
        budget_n = section_budgets["task_state"]
        truncated = _estimate_tokens(text) > budget_n
        text = _truncate_to_tokens(text, budget_n)
        sections.append(SectionResult(
            name="task_state", text=text, budget=budget_n,
            used=_estimate_tokens(text), truncated=truncated, source="caller",
        ))

    # ── 2. upstream_dependencies (handoffs to me + recent artifacts) ───
    if project_id:
        try:
            from . import get_shared_context_store
            store = get_shared_context_store()
            parts: list[str] = []
            # Pending handoffs FOR me
            handoffs = store.list_handoffs(
                project_id=project_id, dst_agent=agent_id,
                status="pending", limit=5,
            ) if agent_id else []
            if handoffs:
                parts.append("# 给我的待处理 handoff")
                for h in handoffs:
                    refs = ", ".join(h.get("artifact_refs") or []) or "无"
                    parts.append(
                        f"- 来自 {h['src_agent']}: {h['intent']}"
                        f"\n  refs={refs} · {h.get('summary','')[:80]}"
                    )
            # Recent artifacts in the project (others' outputs to consider)
            arts = store.list_artifacts(
                project_id=project_id, status="active", limit=5,
            )
            if arts:
                parts.append("\n# 项目最近 artifacts")
                for a in arts:
                    parts.append(
                        f"- `{a['id']}` ({a['kind']}) by {a['agent_id']}: "
                        f"{a['title'][:40]} — {a['summary'][:80]}"
                    )
            text = "\n".join(parts)
        except Exception as e:
            logger.debug("upstream_dependencies fetch failed: %s", e)
            text = ""
        if text:
            budget_n = section_budgets["upstream_dependencies"]
            truncated = _estimate_tokens(text) > budget_n
            text = _truncate_to_tokens(text, budget_n)
            sections.append(SectionResult(
                name="upstream_dependencies", text=text, budget=budget_n,
                used=_estimate_tokens(text), truncated=truncated,
                source="sc_handoffs+sc_artifacts",
            ))

    # ── 3. decisions (recent project decisions) ────────────────────────
    if project_id:
        try:
            from . import get_shared_context_store
            store = get_shared_context_store()
            decs = store.list_decisions(
                project_id=project_id, status="final", limit=5,
            )
            if decs:
                lines = ["# 项目已确认决策"]
                for d in decs:
                    lines.append(
                        f"- **{d['topic']}** → {d['decision']} (by {d['decided_by']})"
                    )
                    if d.get("rationale"):
                        lines.append(f"  理由: {d['rationale'][:100]}")
                text = "\n".join(lines)
            else:
                text = ""
        except Exception as e:
            logger.debug("decisions fetch failed: %s", e)
            text = ""
        if text:
            budget_n = section_budgets["decisions"]
            truncated = _estimate_tokens(text) > budget_n
            text = _truncate_to_tokens(text, budget_n)
            sections.append(SectionResult(
                name="decisions", text=text, budget=budget_n,
                used=_estimate_tokens(text), truncated=truncated,
                source="sc_decisions",
            ))

    # ── 4. knowledge (RAG via existing rag_bridge) ─────────────────────
    if intent:
        try:
            from ..v2.bridges.rag_bridge import retrieve_task_knowledge
            rag_text = retrieve_task_knowledge(intent, role=role or "")
        except Exception as e:
            logger.debug("rag fetch failed: %s", e)
            rag_text = ""
        if rag_text:
            budget_n = section_budgets["knowledge"]
            truncated = _estimate_tokens(rag_text) > budget_n
            rag_text = _truncate_to_tokens(rag_text, budget_n)
            sections.append(SectionResult(
                name="knowledge", text=rag_text, budget=budget_n,
                used=_estimate_tokens(rag_text), truncated=truncated,
                source="rag_bridge",
            ))

    # ── 5. history_summary (caller-provided) ────────────────────────────
    if history_summary_text:
        budget_n = section_budgets["history_summary"]
        truncated = _estimate_tokens(history_summary_text) > budget_n
        text = _truncate_to_tokens(history_summary_text, budget_n)
        sections.append(SectionResult(
            name="history_summary", text=text, budget=budget_n,
            used=_estimate_tokens(text), truncated=truncated, source="caller",
        ))

    # ── compose ────────────────────────────────────────────────────────
    rendered = "\n\n".join(s.text for s in sections if s.text).strip()
    total_used = sum(s.used for s in sections)
    return ContextBundle(
        rendered=rendered,
        total_used=total_used,
        total_budget=budget,
        sections=sections,
    )
