# Runtime Mode Guide — `legacy` ↔ `sdk`

How to switch a TudouClaw agent between the two chat-loop runtimes,
and how to switch back if something breaks.

## Quick reference

| Mode | Path | Behavior |
|---|---|---|
| `legacy` | `app/agent.py:Agent.chat()` | Self-rolled chat loop (default, stable) |
| `sdk` | `app/agent_runtime/SDKAgentRunner` | OpenAI Agents SDK 0.17+ (experimental) |

Both modes share the same B layer (`app/runtime/`): intent
detection, nudge evaluation, stream filters. Per-agent prompts /
persona / skills / memory / tool permissions all flow through
identically — only the chat-loop orchestration differs.

## Three ways to flip the mode

### 1. Portal UI (easiest)

1. Open agent edit modal (gear icon next to agent name)
2. Find the **Runtime Mode** section (above Tool Permissions)
3. Pick `legacy` or `sdk` from the dropdown
4. Click Save
5. Status pill shows `SDK ready` or `SDK not installed`

The selection is persisted to `agents.json`, survives backend
restart.

### 2. POST endpoint

```bash
curl -X POST http://localhost:9090/api/portal/agent/<AGENT_ID>/runtime-mode \
  -H "Content-Type: application/json" \
  -d '{"mode": "sdk"}'
```

Response:

```json
{
  "ok": true,
  "agent_id": "...",
  "previous": "legacy",
  "current": "sdk",
  "sdk_available": true,
  "warning": ""
}
```

### 3. Direct Python (for tests / scripts)

```python
from app.hub import get_hub
agent = get_hub().get_agent("...")
agent.runtime_mode = "sdk"   # or "legacy"
get_hub()._save_agents()      # persist
```

## Rollback (switch back to legacy)

**Always works**. Just flip the dropdown back to `legacy` and save,
or POST `{"mode": "legacy"}` to the same endpoint, or set
`agent.runtime_mode = "legacy"` in Python.

The legacy chat loop is unchanged from before the SDK adapter was
added — flipping back is byte-identical to "as if SDK never
existed for this agent". State (messages, plan, memory, tools)
persists across the toggle; only the orchestration path changes.

## Auto-fallback when SDK isn't installed

If `runtime_mode='sdk'` is set but `pip install openai-agents` was
never run, the next `agent.chat()` logs a one-time warning and
falls back to the legacy loop:

```
WARNING tudou.agent | Agent <id> has runtime_mode='sdk' but
openai-agents is not installed. Falling back to legacy. Run
`pip install openai-agents` to enable.
```

So the UI toggle is safe to flip pre-install — agents keep working
under legacy until you actually install the SDK.

## What the SDK runtime currently delivers (Phase 2 status)

✅ End-to-end LLM round-trip via SDK Runner  
✅ TudouClaw tools wrapped as SDK FunctionTool, dispatch through
   `tools.tool_registry.dispatch` (same handlers as legacy — no
   tool reimplementation)  
✅ Static system prompt + dynamic context (env / kb_wiki /
   scheduled / plan / recent artifacts / project / meeting) —
   identical to legacy  
✅ Opt-in filter for `knowledge_lookup` / `memory_recall` /
   `wiki_ingest` — same as legacy  
✅ XML tool_call leak detection on streamed text deltas (same B
   helper, same retract event)  
✅ Events forwarded to portal in legacy `AgentEvent` shape
   (tool_call_start / tool_call_end / message). NOTE: as of
   2026-05-16 the SDK runtime is **non-streaming** — see "Why
   non-streaming" below. Tool-call cards still appear; the final
   message arrives as one bubble instead of typing animation.  
✅ L3 action buffer + flush on agent_end (long-term memory still
   accumulates)  
✅ Switch-back to legacy at any time, persisted across restart  

## Why non-streaming (2026-05-16)

`Runner.run_streamed` has a reproducible bug with DeepSeek-style
OpenAI-compat backends + parallel `tool_calls`: the streaming delta
accumulator drops one of the parallel tool_call entries, so the
SECOND LLM call sends N tool_calls in the asst message but only
N-1 tool results. DeepSeek correctly rejects that with HTTP 400
"insufficient tool messages following tool_calls". Same setup with
`Runner.run` (non-streaming) works perfectly — verified via
`scripts/sdk_nonstreaming_verify.py`.

So `_run_streamed` now uses `Runner.run` and forwards
`result.new_items` via a `_FakeRunItemEvent` shim into the same
event bridge dispatcher. Everything the portal needs (tool_call
cards, final assistant text, retract events) still works; only
the per-token typing animation is lost. UX downgrade is acceptable;
correctness wins. When upstream SDK fixes the streaming
accumulator, flip `_run_streamed` back to `Runner.run_streamed`.

## What's NOT yet wired (Phase 3+)

⏳ Token-by-token typing animation. Lost when we moved to
   `Runner.run`; restoring it requires either (a) upstream SDK
   fix for the streaming + parallel-tool_calls bug, or (b) our own
   manual streaming wrapper that doesn't trip the accumulator.

⏳ Nudge **injection** (re-running the LLM with an injected system
   message). Currently `evaluate_nudge` runs in `on_llm_end`, would-
   fire events emit to portal, but the actual mid-run input mutation
   isn't bound — SDK 0.17 exposes input mutation only via the
   Runner's input_items parameter for the NEXT call, which doesn't
   directly map to the legacy "inject + continue" pattern. Phase 3
   work: wrap a manual loop calling `Runner.run` per turn so we can
   mutate input between turns.

⏳ History compaction trigger from inside the SDK loop. Currently
   compaction is driven by the legacy chat loop's outer iteration
   counter; the SDK Runner has its own `max_turns` limit.

⏳ Plan-pending continuation nudge. Same reason as above — needs
   nudge injection.

⏳ Multimodal user_message (images, audio). Currently we cast
   non-string `user_message` to `str()`. Phase 3 work: pass through
   as-is via SDK's content-list input form.

⏳ Multi-agent dispatch (`team_create`). TudouClaw's
   cross-process orchestration doesn't go through SDK Handoffs.
   Probably stays separate (TudouClaw is the orchestrator, SDK is
   per-agent).

## Production rollout recommendation

1. **Phase A (now)**: leave all production agents on `legacy`. Use
   `sdk` only on test agents created specifically to validate.
2. **Phase B (after Phase 3)**: pick ONE production agent that
   doesn't depend on nudges (e.g. simple Q&A bot, no
   tool-error-loop, no validate-claim pattern), flip to `sdk`,
   run for a week, observe.
3. **Phase C**: roll out per-agent based on observed behavior.

If anything goes wrong in any phase, flip the dropdown back to
`legacy`. Zero state corruption — just a one-line toggle.

## Diagnostic / debug

Backend logs `TOOL_SET agent=<id> count=<N>` per turn so you can
see how many tools the agent ended up with under either runtime.

SDK runtime additionally logs:
  - `tool_registry: built N SDK function_tool(s) for agent=<id>`
  - `SDK runtime nudge would fire: kind=<kind>` (Phase 3 will turn
    this into actual injection)
  - `SDKAgentRunner.run failed (agent=<id>): <err>` if the SDK
    layer threw — check for upstream OpenAI-compat issues, model
    name mismatch, or rate limit

If under `sdk` mode an agent emits `[SDK runtime error — falling
back to legacy turn: ...]` as its reply text, it means the SDK
crashed mid-turn. The legacy fallback wasn't taken (we'd need to
re-route, which is invasive). Flip the toggle back to `legacy` for
that agent until the SDK error is fixed.

## Related

- `docs/MIGRATION_OPENAI_AGENTS_SDK.md` — full migration design
- `docs/SDK_CHEATSHEET.md` — SDK API quick reference
- `scripts/sdk_mimo_connectivity_poc.py` — verifies SDK can talk
  to TudouClaw's existing OpenAI-compat providers
