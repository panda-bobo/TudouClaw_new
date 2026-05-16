"""Instructions builder — wraps TudouClaw's static + dynamic prompt
machinery into an SDK-compatible callable.

Why a callable (not a static string):
  - Persona / project context / dynamic state change per turn
  - SDK supports ``Agent(instructions=callable)`` for exactly this
  - Lets us reuse TudouClaw's existing _build_static_system_prompt
    AND _build_dynamic_context **as-is** (no behavior fork)

The returned callable receives the SDK's RunContextWrapper + agent
instance per the SDK contract; we ignore both — TudouClaw's prompt
is built from the TudouClaw Agent state we captured at construction
time.

Layout (matches what legacy A sees, to keep prompt cache stable):

    [static system prompt]   ← persona / role / soul / scene_prompts
                                / FILE_DISPLAY / IMAGE / ATTACHMENT /
                                granted skill index / handoff role /
                                ...
    \\n\\n
    [dynamic context]        ← env / plan state / kb_wiki / scheduled
                                / recent artifacts / project chat /
                                meeting digest / ...

In the legacy A loop, dynamic context is prepended into the LAST
user message (KV-cache optimization for local Ollama / vLLM). In
the SDK runtime we put it in the instructions string instead — the
SDK's Runner doesn't expose the same per-message mutation hook,
and the cost difference is negligible for hosted providers. If the
KV-cache cost ever becomes a real issue under SDK we can switch to
a RunHooks.on_llm_start mutation.
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def build_instructions_callable(
    tudou_agent,
    *,
    user_message: Any,
    context_id: str = "solo",
) -> Callable:
    """Return a callable suitable for SDK ``Agent(instructions=...)``.

    The callable is invoked by the SDK before each LLM call; it
    rebuilds the static prompt + dynamic context fresh from the
    TudouClaw Agent state. This means everything that works in
    legacy A continues to work in C: persona, soul_md, granted skill
    list, project context, scheduled jobs, recent artifacts, plan
    state, etc.
    """
    # Capture the user_message at build time so dynamic context can
    # use it as the "current query" hint (some dynamic blocks use
    # this to filter — e.g. wiki recall, plan-step routing).
    user_text = (user_message if isinstance(user_message, str)
                 else str(user_message or ""))

    def _instructions(run_ctx: Any = None, agent: Any = None) -> str:
        """SDK-compatible instructions callback. Called by Runner
        before each LLM call. Returning fresh strings each turn
        means dynamic changes (new memory facts, plan progress,
        new artifacts) are picked up automatically."""
        parts = []

        # 1. Static system prompt — persona / soul / scene_prompts /
        #    granted_skills index / handoff role / file_display /
        #    image_display / attachment_contract / ...
        try:
            static = tudou_agent._build_static_system_prompt() or ""
            if static:
                parts.append(static)
        except Exception as e:
            logger.warning(
                "instructions_builder: static prompt failed: %s", e)

        # 2. Dynamic context — env / kb_wiki / scheduled / recent
        #    artifacts / plan state / project chat / meeting digest.
        #    Same call legacy A makes; same content.
        try:
            dynamic = tudou_agent._build_dynamic_context(
                current_query=user_text) or ""
            if dynamic:
                parts.append(dynamic)
        except Exception as e:
            logger.warning(
                "instructions_builder: dynamic ctx failed: %s", e)

        return "\n\n".join(parts) if parts else ""

    return _instructions
