# Superpowers — Vendored from obra/superpowers

This directory contains 14 skills vendored from [obra/superpowers](https://github.com/obra/superpowers) (MIT-licensed). Each skill is a SKILL.md file with YAML frontmatter conforming to the Anthropic Agent Skills spec — natively compatible with TudouClaw's `skill_store` catalog.

## Attribution

- Upstream: <https://github.com/obra/superpowers>
- License: MIT (see `LICENSE`)
- Vendored at: `app/skills/builtin/superpowers/`
- Source recorded in each SKILL.md frontmatter as `metadata.source: obra/superpowers`

## Role Bindings (see `app/core/role_defaults.py`)

### Auto-bound (installed by default when agent is created with that role)

**coder** (8 core engineering skills):
- `test-driven-development`
- `systematic-debugging`
- `verification-before-completion`
- `writing-plans`
- `executing-plans`
- `requesting-code-review`
- `receiving-code-review`
- `finishing-a-development-branch`

**reviewer** (+3 collateral):
- `receiving-code-review`
- `requesting-code-review`
- `verification-before-completion`

**architect** (+3 collateral):
- `writing-plans`
- `executing-plans`
- `systematic-debugging`

**tester** (+2 collateral):
- `test-driven-development`
- `verification-before-completion`

**pm** (+1 collateral):
- `brainstorming`

### Catalog-only (installed but NOT auto-bound; users opt-in per agent)

| skill | Why not auto-bound |
|-------|---------------------|
| `using-git-worktrees` | TudouClaw does not use worktrees by default |
| `using-superpowers` | Meta-skill — describes how the others fit together |
| `writing-skills` | Meta — for authors of new skills |
| `dispatching-parallel-agents` | Requires Claude Code's `Task` tool; adapted to TudouClaw `delegate` but kept off until workflows are stable |
| `subagent-driven-development` | Same reason |

## TudouClaw Adaptations

Two skills reference Claude Code's `Task` tool, which does not map 1:1 to TudouClaw. A minimal adaptation note has been inserted at the top of each body:

- `dispatching-parallel-agents/SKILL.md`
- `subagent-driven-development/SKILL.md`

Both map the `Task` tool → TudouClaw's `delegate` capability (`Agent.delegate()` in `app/agent.py:5838`). They remain disabled-by-default in `role_defaults`; enable manually in the portal when the workflow truly benefits.

## Updating from Upstream

To sync with upstream obra/superpowers (preserving local adaptation notes):

```bash
# 1. Re-clone upstream
git clone --depth 1 https://github.com/obra/superpowers /tmp/superpowers_src

# 2. Diff each SKILL.md — manually re-apply any new upstream body content,
#    keeping the TudouClaw Adaptation Note blocks in the 2 translated skills
#    and preserving the metadata.source frontmatter block in all 14.

# 3. Re-run the frontmatter patch script (see commit history for the original patch script).
```

Alternatively, use the Portal's **Skill Store → Import from URL** feature with URL `https://github.com/obra/superpowers/tree/main/skills` — this populates the user-space catalog at `~/.tudou_claw/skill_catalog/` without touching the vendored copy. The vendored copy wins priority at resolution time unless renamed.
