"""Preprocessor module — small local LLM running at 3 fixed nodes:

  ① first-turn task setup (prompt rewrite + task understanding)
  ② task decomposition (rough draft before propose_decomposition refines)
  ③ prompt optimization (compress oversized system/user prompts)

Per-agent opt-in via ``Agent.preprocessor_model``. Empty model = disabled.
``Agent.preprocessor_modes`` further selects which sub-behaviours run at
node ① (``optimize_prompt`` and/or ``task_understanding``).

The bridge ALWAYS falls back to the original code path on:
  - empty preprocessor_model
  - small-model timeout (3s)
  - small-model error
  - failure rate > threshold (auto-pause for 60s)

So the worst case is "preprocessor adds zero benefit"; it can never break
the main pipeline.
"""
from .bridge import (
    is_enabled,
    get_model,
    get_modes,
    PreprocessorResult,
    invoke,
    get_metrics,
    get_breaker_state,
)

__all__ = [
    "is_enabled", "get_model", "get_modes",
    "PreprocessorResult", "invoke",
    "get_metrics", "get_breaker_state",
]

# Mode constants for Agent.preprocessor_modes
MODE_OPTIMIZE_PROMPT = "optimize_prompt"
MODE_TASK_UNDERSTANDING = "task_understanding"
ALL_MODES = (MODE_OPTIMIZE_PROMPT, MODE_TASK_UNDERSTANDING)
