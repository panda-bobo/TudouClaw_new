"""C: OpenAI Agents SDK adapter for TudouClaw.

Architecture (per docs/MIGRATION_OPENAI_AGENTS_SDK.md):

    A: app/agent.py:Agent.chat()  — legacy self-rolled chat loop
    B: app/runtime/                — shared pure-function helpers
    C: app/agent_runtime/  ★ THIS PACKAGE ★ — SDK adapter

This is the experimental NEW chat loop. It wraps OpenAI Agents SDK
(``pip install openai-agents``) so TudouClaw can replace its 14K-line
self-rolled streaming + tool dispatch + parser code with the SDK's
production-grade equivalents, while keeping all of TudouClaw's
distinguishing layers (multi-agent / persona / skill / portal /
memory / Chinese-first) above the SDK.

Status: PoC scaffold. The Agent dataclass has a ``runtime_mode``
field (default "legacy"); only when set to "sdk" does Agent.chat()
route here. Currently NOT enabled for any production agent — admin
opts in per-agent via portal Tool Permissions UI (toggle TBD).

Lazy SDK import: this package imports cleanly even WITHOUT the
``openai-agents`` package installed. The SDK is loaded on first
actual run; ``is_sdk_available()`` lets callers gate calls.

Modules:

  sdk_adapter.py         — main SDKAgentRunner class
  instructions_builder.py — wraps TudouClaw _build_static_system_prompt
                            into SDK-compatible callable instructions
  tool_registry.py       — converts TudouClaw tools.py registry to
                            SDK @function_tool decorators
  event_bridge.py        — translates SDK stream events to portal UI
                            event shape (text_delta / tool_call_start
                            / artifact_refs / etc.)
  hooks.py               — RunHooks that call into B's nudge evaluator,
                            L3 memory flush, history compaction trigger

Common pattern in every module: try-import the SDK at the top; if
ImportError, set a sentinel and have callers raise a clear
"openai-agents not installed; pip install openai-agents" error
when invoked. This way the package CAN be imported in environments
without the SDK (CI, legacy-only deployments).
"""
from __future__ import annotations

from .sdk_adapter import SDKAgentRunner, is_sdk_available, SDKNotInstalledError

__all__ = [
    "SDKAgentRunner",
    "is_sdk_available",
    "SDKNotInstalledError",
]
