#!/usr/bin/env python3
"""0c PoC: prove OpenAI Agents SDK can talk to TudouClaw's existing
LLM provider (mimo / qwen / deepseek / etc. via OpenAI-compat).

Run:
    python scripts/sdk_mimo_connectivity_poc.py

What this validates:
  1. ``import agents`` works (SDK installed)
  2. We can construct ``OpenAIChatCompletionsModel`` pointing at a
     custom base_url + api_key (TudouClaw provider config)
  3. ``agents.Agent`` + ``agents.Runner.run_sync`` complete a
     round-trip
  4. A simple ``@function_tool`` gets dispatched correctly

Configuration: reads from the TudouClaw provider registry. By
default uses the first provider in ``~/.tudou_claw/providers.json``
that has a non-empty base_url + api_key. Override via env:
    SDK_POC_BASE_URL=... SDK_POC_API_KEY=... SDK_POC_MODEL=...

This script does NOT touch any TudouClaw agent state. It's a
standalone connectivity check; failure means SDK cannot speak the
provider's protocol and Phase 1+ migration is blocked until fixed.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def resolve_provider_config():
    """Return (base_url, api_key, model). Sources, in priority order:
    1. SDK_POC_* env vars (manual override)
    2. ~/.tudou_claw/providers.json first usable entry
    3. Hardcoded mimo defaults (best-guess)
    """
    base_url = os.environ.get("SDK_POC_BASE_URL", "").strip()
    api_key = os.environ.get("SDK_POC_API_KEY", "").strip()
    model = os.environ.get("SDK_POC_MODEL", "").strip()

    if base_url and api_key:
        return (base_url, api_key, model or "mimo-v2.5-pro")

    # Try ~/.tudou_claw/providers.json
    providers_path = Path.home() / ".tudou_claw" / "providers.json"
    if providers_path.exists():
        try:
            with providers_path.open() as f:
                data = json.load(f)
            entries = (data.get("providers") or
                       data.get("entries") or
                       (data if isinstance(data, list) else []))
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                bu = (entry.get("base_url") or
                      entry.get("openai_base_url") or "").strip()
                ak = (entry.get("api_key") or
                      entry.get("openai_api_key") or "").strip()
                if bu and ak:
                    m = (entry.get("model") or
                         entry.get("default_model") or
                         "mimo-v2.5-pro")
                    return (bu, ak, m)
        except Exception as e:
            print(f"[warn] failed to parse providers.json: {e}",
                  file=sys.stderr)

    # Final fallback
    return ("http://localhost:11434/v1", "dummy", "mimo-v2.5-pro")


def main() -> int:
    # ── Step 1: SDK importable ─────────────────────────────────────
    print("─" * 60)
    print("Step 1: import agents (OpenAI Agents SDK)")
    try:
        import agents  # noqa: F401
        from agents import (
            Agent, Runner, OpenAIChatCompletionsModel,
            function_tool, set_tracing_disabled,
        )
        print("  ✓ openai-agents imported")
        print(f"  version: {getattr(agents, '__version__', 'unknown')}")
    except ImportError as e:
        print(f"  ✗ import failed: {e}")
        print("  Run: pip install openai-agents")
        return 1

    # Disable tracing — we don't have an Anthropic-platform key
    set_tracing_disabled(disabled=True)

    # ── Step 2: build a custom-endpoint Model ──────────────────────
    print()
    print("─" * 60)
    print("Step 2: build OpenAIChatCompletionsModel with custom URL")
    base_url, api_key, model_name = resolve_provider_config()
    print(f"  base_url: {base_url}")
    print(f"  api_key:  {'(set, len=%d)' % len(api_key) if api_key != 'dummy' else '(dummy / not configured)'}")
    print(f"  model:    {model_name}")

    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        sdk_model = OpenAIChatCompletionsModel(
            model=model_name, openai_client=client)
        print("  ✓ Model object constructed")
    except Exception as e:
        print(f"  ✗ Model construction failed: {e}")
        return 2

    # ── Step 3: define a simple tool ──────────────────────────────
    print()
    print("─" * 60)
    print("Step 3: define @function_tool")

    @function_tool
    def echo(message: str) -> str:
        """Echo the input message back to the caller. Used purely
        as a connectivity-check tool."""
        return f"echoed: {message}"

    print(f"  ✓ tool defined: {echo.name if hasattr(echo, 'name') else 'echo'}")

    # ── Step 4: build an Agent + Run ──────────────────────────────
    print()
    print("─" * 60)
    print("Step 4: build Agent + Runner.run_sync")
    try:
        agent = Agent(
            name="ConnectivityProbe",
            instructions=(
                "You are a connectivity probe. When the user gives "
                "you a message, call the `echo` tool with that "
                "message. Then report what you got back."
            ),
            model=sdk_model,
            tools=[echo],
        )
        print(f"  ✓ Agent constructed")

        if api_key == "dummy":
            print()
            print("  ⚠ api_key is 'dummy' — skipping live run.")
            print("    Configure SDK_POC_BASE_URL / SDK_POC_API_KEY")
            print("    OR populate ~/.tudou_claw/providers.json to do")
            print("    a real round-trip test.")
            return 0

        result = Runner.run_sync(
            agent,
            "Please echo back the word: TUDOUCLAW",
        )
        print(f"  ✓ Run completed")
        print()
        print(f"  Final output: {result.final_output!r}")
        return 0
    except Exception as e:
        print(f"  ✗ Run failed: {type(e).__name__}: {e}")
        print()
        print("  Common causes:")
        print("  - base_url unreachable (model server not running?)")
        print("  - api_key wrong")
        print("  - model name doesn't exist on the endpoint")
        print("  - endpoint doesn't speak OpenAI Chat Completions API")
        print("  - SDK / openai package version mismatch")
        import traceback
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
