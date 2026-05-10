# Agent Specialty Cultivation Module

**Status:** Phase 0 (skeleton) — see [docs/superpowers/plans/](../../docs/superpowers/plans/)

## What this is

A self-contained module that lets any Tudou agent be cultivated into a domain
expert (legal / medical / finance / ...). Agents stay one entity; the module
adds: corpus + RAG + (later) LoRA + routing.

See the [design spec](../../docs/superpowers/specs/2026-05-10-agent-specialty-cultivation-design.md)
for the full architecture.

## Hard isolation guarantees

- `TUDOU_EXPERT_DISABLED=1` → module skips init; existing functionality untouched
- New deps **optional** in `requirements-expert.txt`; main `requirements.txt` unchanged
- All persistent data under `~/.tudou_claw/expert/<agent_id>/`
- Agent dataclass gets **5 OPTIONAL** fields (all default empty); old `agents.json` loads cleanly

## Sub-packages (filled by parallel tracks)

| Sub-package | Track | Status |
|---|---|---|
| `corpus/` | Track A — ingestion + chunking + vector store | Phase 0: empty |
| `retrieval/` | Track A — embedding + reranker + hybrid pipeline | Phase 0: empty |
| `training/` | Track C — trace cleaner + RAFT + LoRA + eval | Phase 0: empty |
| `inference/` | (Verticals V4+) — routing + safety + pipeline | Phase 0: empty |
| `api/` | All — REST endpoints `/api/portal/agent/{id}/expert/*` | Phase 0: 501 stubs |

## Where to look

- API entry: `app/domain_expert/api/routers.py`
- Specialty templates: `app/data/specialty_templates/*.yaml`
- Persistent data: `~/.tudou_claw/expert/<agent_id>/`
- Spec: `docs/superpowers/specs/2026-05-10-agent-specialty-cultivation-design.md`
- Plans: `docs/superpowers/plans/2026-05-10-INDEX.md`

## How to disable

```bash
TUDOU_EXPERT_DISABLED=1 ~/run_tudou.sh --restart
```

When disabled, the API namespace returns `503 Service Unavailable` and
the agent reply pipeline silently bypasses the cultivation hook.
