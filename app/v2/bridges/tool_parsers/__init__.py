"""
Tool-call parser plugin layer.

A ``ToolCallParser`` takes a raw LLM response message and returns a
``NormalizedMessage`` whose ``tool_calls`` field is an OpenAI-style list,
regardless of how the underlying model emitted them (native field,
``<tool_call>{...}</tool_call>`` XML markers, bare JSON in content, etc.).

Adding support for a new model family is a plugin operation — no core
module is edited. Three extension paths, from cheapest to richest:

    1. YAML config only     (if the model uses an already-known format)
    2. Drop a .py file      (``user_parsers/`` is auto-scanned)
    3. External pip package (via ``tudouclaw.tool_parsers`` entry-point)

See ``docs/PRD_AGENT_V2.md`` §10.5.1 and this package's ``base.py`` for
the full protocol. ``builtin.py`` ships three sample implementations that
cover most contemporary models without writing any new code.
"""
from .base import (
    NormalizedMessage,
    ToolCallParser,
    ParserRegistry,
    get_registry,
    register,
)

__all__ = [
    "NormalizedMessage",
    "ToolCallParser",
    "ParserRegistry",
    "get_registry",
    "register",
]
