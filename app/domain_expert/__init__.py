"""Agent Specialty Cultivation System.

See docs/superpowers/specs/2026-05-10-agent-specialty-cultivation-design.md
for the design doc this module implements.

Sub-packages:
    corpus      — Track A: ingestion + chunking + vector store
    retrieval   — Track A: embedding + reranking + hybrid search
    training    — Track C: trace cleaner + RAFT synth + LoRA + eval
    inference   — Future: routing + safety + pipeline
    api         — REST endpoints under /api/portal/agent/{id}/expert/*

Hard isolation guarantees:
    - TUDOU_EXPERT_DISABLED=1 → module skips init; existing functionality untouched
    - New deps optional in `requirements-expert.txt`; main `requirements.txt` unchanged
    - All persistent data under ~/.tudou_claw/expert/<agent_id>/
    - Agent dataclass gets 5 OPTIONAL fields (default empty); old agents.json loads cleanly
"""
__version__ = "0.0.1-phase-0"
