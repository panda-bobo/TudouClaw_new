# TudouClaw Architectural Principles

Living document. Each principle below is enforced in code and called out
in commit messages so future contributors don't accidentally regress.

---

## 1. Decision authority: user / framework, not agent

> **Anything that can be decided by the user or by the framework should
> not be decided by the agent.**
>
> The agent decides **HOW to do** what's already been decided to do. It
> does not decide **WHETHER** to do it, **WHEN** to do it, or **WHETHER
> to consume a particular resource**.

### Why

1. **Decision space collapses → weak models stay reliable.** Mimo /
   qwen / early-deepseek can't reliably classify "should I call
   knowledge_lookup right now?". Removing the choice removes the
   failure mode.
2. **Token savings.** Tools the agent can't choose to call don't ship
   in `tools[]` (~200-500 tokens per surface).
3. **Explainability.** "Why did the agent call X?" has one answer:
   "user/framework triggered it." Never "the agent guessed wrong."

### Concrete applications

#### ✅ Shipped

| Surface | Mechanism | Detector | Commit |
|---|---|---|---|
| `knowledge_lookup` / `memory_recall` | Filtered out of `tool_defs` unless user message contains explicit retrieval phrasing | `app/agent.py::_user_explicitly_requests_retrieval` | `2579e41` (2026-05-15) |
| `wiki_ingest` | Filtered out unless user message contains explicit save phrasing | `app/agent.py::_user_explicitly_requests_wiki_write` | (this commit) |

#### 🚧 Roadmap (in priority order)

| Surface | Current pain | Proposed trigger |
|---|---|---|
| `team_create` | Agent-spawned sub-agents waste compute when triggered without user intent | User: "拆成几个并行做" / framework: `parallel:true` plan flag |
| `web_search` / `web_fetch` | Data-leak surface; agent can fetch arbitrary URLs | User mentions URL or "查/online" / framework allow-list domain |
| `propose_skill` | Agent decides "I need a new tool" → admin queue noise | User-only ("把这个能力做成 skill") |

### Detector design rules

When adding a new explicit-opt-in surface:

1. **Detector is conservative — false-negatives over false-positives.**
   Better the user retypes with explicit wording than the agent gets
   to spam.
2. **Cheap fast-path before regex.** Single substring check against a
   keyword set; only run the regex if the keyword check passes.
3. **Bilingual ZH + EN.** Match both natural language families.
4. **Log when the filter fires.** Include the user message snippet so
   admins can audit "the framework saw NO retrieval intent here".
5. **Test against ≥20 realistic positive + ≥10 negative cases.** Aim
   for 100% on the test set before shipping.

### Non-negotiable for new tools

When adding a NEW tool to TudouClaw, ask:

> Is the agent going to be able to decide WHEN to call this correctly,
> on a model as weak as mimo?

If the answer is no → default OFF, add to the explicit-opt-in detector.

---

## 2. Code holds mechanism, scene_prompts hold policy

> **Built-in code (`app/system_prompt.py`, `app/core/prompt_schemas.py`)
> describes framework MECHANISM only. Behavioral POLICY lives in admin-
> editable scene_prompts.**

### Why

1. **Single source of truth per rule.** Anything an admin can edit in
   the Settings UI shouldn't ALSO be hardcoded — that's double-injection
   AND prevents the admin's edits from actually overriding anything.
2. **Each install can tune behavior.** Built-in stays the same; admin
   gets to express their company's culture.

### Concrete applications

#### ✅ Shipped (commit `01b6ee6`, 2026-05-14)

- Retired `ExecutionDisciplineSchema` injection (was double-injection
  with admin scene_prompt "执行纪律").
- Trimmed `_TOOL_RULES_ZH/EN` to framework MECHANICS only
  (parallel-return, plan_update API, team_create, approval flow,
  📂/📦 markers). Behavioral rules ("don't repeat the plan in chat",
  batching style) live in scene_prompts.
- Trimmed `_KNOWLEDGE_RULES_ZH/EN` to bare tool signatures
  (`wiki_ingest`, `knowledge_lookup`).

### Test for new built-in prompt content

Before adding a new sentence to `system_prompt.py` or
`prompt_schemas.py`, ask:

> Could an admin reasonably want this rule to be different in their
> install? (Different language, different tone, stricter, looser?)

If yes → it's policy → put it in a default scene_prompt template, not
in code.

---

## 3. Compression preserves user intent verbatim

> **History compression must NEVER lose user-stated facts. Code
> determinism is preferred over LLM summarization for things a code
> scan can extract reliably.**

### Why

The agent's continuation logic depends on user intent. Lossy
summarization that drops user phrasing causes the agent to drift.

### Concrete applications

#### ✅ Shipped

| Mechanism | Module | Commit |
|---|---|---|
| `USER_VERBATIM` block — all user messages preserved verbatim (cap 30K total / 8K per msg) | `app/agent.py::_summarize_old_history` | (pre-existing) |
| `STRUCTURED_FACTS` — deterministic extraction of files modified, bash commands, errors, tool counts | `app/agent.py::_extract_structured_facts` | `57fa7c9` (2026-05-14, fix tool name match) |
| Claude Code 9-section summary template — sections 3/4/6 filled by deterministic blocks; LLM only writes 1, 2, 5, 7, 8, 9 | `app/agent.py::_summarize_old_history` | `10a6702` (2026-05-14) |
| Manual `/compact` triggers `_summarize_old_history` on `self.messages` (was: only transcript, ineffective) | `app/agent.py::Agent.compact_memory` | `bf2d645` (2026-05-14) |
| Emergency-compact retry on context-overflow LLM errors | `app/agent.py::chat()` | `6ad2965` (2026-05-14) |

---

## 4. UI events filter leaked internal markers before emit

> **Any internal-marker text the LLM might mimic (sanitizer
> placeholders, framework instructions, system-block envelopes) must be
> filtered BEFORE the MESSAGE event reaches the UI / log, not after.**

### Why

Streaming text_delta events draw the chat bubble character-by-
character. Post-stream filters that only clean `self.messages[]`
leave the bubble drawn. Users see the leak.

### Concrete applications

#### ✅ Shipped (commit `149359d`, 2026-05-14)

- `_emit("message", ...)` at agent.py:11436 now strips through
  `_strip_leaked_system_blocks` BEFORE emit + log.
- If strip removes ≥50% of content (whole reply was a leak), suppress
  emit entirely AND emit `retract_last_assistant` event so the UI
  removes the partial-stream bubble.
- Sanitizer placeholder text (`app/llm.py:1086`) uses ⟦…⟧ unusual
  brackets + explicit "do NOT echo this line" inline directive to
  reduce LLM mimic rate at the source.
