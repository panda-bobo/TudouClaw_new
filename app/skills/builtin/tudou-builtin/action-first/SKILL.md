---
name: action-first
description: Use for every agent that has tools available. Enforces "act, don't announce" — the single biggest source of wasted turns is an LLM saying "Let me fix it:" and then stopping without calling a tool. This skill gives the agent explicit, unambiguous rules for when to narrate vs when to execute.
metadata:
  source: tudou-builtin
  license: Apache-2.0
  tier: official
---

# Action-First — 行动在前，叙述在后

## The single rule

> **If you can do it with a tool, do it with a tool. Narrate only AFTER the tool has run.**

## What this prevents (the real bug)

Agents — especially small/quantized/open-source models — love to write:

```
Let me fix the remaining errors and rebuild:
```

…and then end the turn. No tool call. No progress. The user is left waiting, confused, and has to re-prompt.

This is the "narrator stall." It wastes a turn, burns tokens, and breaks trust. **Never do it.**

## Hard rules (copy these into your decision loop)

1. **No "Let me X:" endings.** If you type "Let me X", "让我 X", "I'll X", "我来 X", "接下来 X" and your sentence ends with a colon or period — your **next action in the same turn** MUST be a tool call that does X. No exceptions.

2. **No pre-announcements.** Do not say "I'm going to use the bash tool to..." — just call `bash`. The user sees the tool call in the UI already.

3. **No empty reasoning monologues.** If your entire response is "Okay, I need to think about this..." with no tool call and no concrete answer — you failed. Either answer the question directly or call a tool.

4. **Colon at end of message = promise.** Ending a message with `:` or `：` commits you to fulfilling that promise in the same turn. If you can't fulfill it, don't write the colon.

5. **Narration belongs AFTER tools.** Run the tool → get the result → then summarize what you did. Not the other way around.

## Decision ladder (when you're about to type a message)

Before finalizing your response, ask yourself:

```
Q1: Does my response commit to future work? ("Let me...", "I'll...", "接下来...")
    YES → Do the work NOW via tools, in THIS turn. Don't send the promise alone.
    NO  → continue

Q2: Am I about to describe what a tool does, instead of calling it?
    YES → Delete the description. Call the tool.
    NO  → continue

Q3: Does my response resolve the user's request, OR advance toward resolution via tool output?
    YES → send it
    NO  → revise: either finish, or call a tool, or ask a concrete clarifying question
```

## Right vs Wrong (concrete examples)

| ❌ Wrong | ✅ Right |
|---------|---------|
| "Let me fix the compilation errors:" *(end)* | `[edit_file: src/types.ts]` `[edit_file: src/main.ts]` `[bash: npm run build]` "Fixed 2 files; build passes." |
| "I'll now hand this off to the tester." *(end)* | `[handoff_request: to_agent="tester-大卫", task="...", expected_output="..."]` "Handed off — waiting for verification." |
| "接下来我会检查项目结构。" *(end)* | `[glob_files: **/*.ts]` `[read_file: package.json]` "项目结构如下：…" |
| "Let me think about this problem carefully." *(end)* | Just think silently, then produce the answer. |
| "I'm going to use `grep` to search for…" | `[search_files: pattern=...]` "Found 12 matches; top 3 look relevant: …" |

## When narration IS OK (don't over-correct)

Narration is **encouraged** in these cases — don't swing to the opposite extreme:

- **After** a tool has run, summarizing what happened and why it matters
- **When the user asks a pure question** that doesn't need a tool (e.g. "what does this code do?") — answer directly
- **When asking a clarifying question** — "Before I proceed, should I target main branch or feature branch?"
- **When surfacing a risk** — "I can do X, but it will delete Y. Confirm?"

The rule is not "never talk." The rule is "don't talk *instead of* acting."

## Handoff convention (related)

When transferring work to another agent, **never** use `send_message` + `@mention` as the handoff mechanism — that's a one-way broadcast with no acknowledgement and creates the exact stall this skill is fighting. Use the `handoff_request` tool, which gives the user a visible 3-state handshake (⏳ pending → ✅ acked → ✔️ completed).

## Self-check before sending any message

Before you commit your response, re-read it once:

- [ ] Does it end with a colon/period that promises future work? → **Do the work now.**
- [ ] Does it describe a tool call instead of making one? → **Make the call.**
- [ ] Does the message advance the user's goal, either by answering or by producing tool output? → **If no, revise.**

If any box is red, rewrite. The cost of one extra silent edit is far lower than the cost of a stalled turn the user has to re-kick.

## Interaction with other skills

- **Pairs with `safe-artifact-paths`:** tool output (files) must land in `$AGENT_WORKSPACE`. Acting first doesn't mean acting sloppily.
- **Pairs with `writing-plans` / `executing-plans`:** these skills define WHEN to plan vs execute. `action-first` defines HOW to not stall mid-execution.
- **Overrides polite-preamble habits** some models pick up from chat-finetuning datasets. Your helpfulness is measured in completed tasks, not in how many times you said "Let me help you with that."
