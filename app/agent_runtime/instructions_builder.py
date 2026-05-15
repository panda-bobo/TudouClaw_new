"""Instructions builder — wraps TudouClaw's static + dynamic prompt
machinery into an SDK-compatible callable.

Why a callable (not a static string):
  - Persona / project context / dynamic state changes per turn
  - SDK supports ``Agent(instructions=callable)`` for exactly this
  - Lets us reuse TudouClaw's existing _build_static_system_prompt
    and _inject_dynamic_context **as-is** (no behavior fork)

The returned callable receives the SDK's RunContextWrapper + agent
instance per the SDK contract; we ignore both because TudouClaw's
prompt is built from the TudouClaw Agent state we captured at
construction time.
"""
from __future__ import annotations

from typing import Any, Callable


def build_instructions_callable(
    tudou_agent,
    *,
    user_message: Any,
    context_id: str = "solo",
) -> Callable:
    """Return a callable suitable for SDK ``Agent(instructions=...)``.

    The callable is invoked by the SDK before each LLM call; it
    rebuilds the static prompt + dynamic context fresh from the
    TudouClaw Agent state. This means everything that worked in
    legacy A continues to work in C: persona, soul_md, granted skill
    list, project context, scheduled jobs, recent artifacts, ...
    """
    # NOTE: this is a SCAFFOLD. Phase 1 will fill in the actual
    # rebuild logic. The legacy chat loop calls:
    #   1. self._build_static_system_prompt() — persona / role
    #   2. self._inject_dynamic_context(messages, current_query=...)
    #      — env / kb / plan / scheduled / etc.
    # We need to call both and concat into one instructions string.
    #
    # The SDK invokes the callable per turn, so dynamic changes
    # (new memory, plan progress) are picked up automatically.

    def _instructions(run_ctx: Any = None, agent: Any = None) -> str:
        """SDK-compatible instructions callback."""
        try:
            static = tudou_agent._build_static_system_prompt()
        except Exception:
            static = ""
        # Dynamic context (env / kb_wiki / scheduled / etc.) is built
        # from messages — but in the SDK runtime, messages flow
        # through the Runner. For PoC we start with static only;
        # Phase 1 will add the dynamic block via a RunHook
        # (on_llm_start) that mutates the input list.
        return static or ""

    return _instructions
