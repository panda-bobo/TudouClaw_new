"""Verification: SDK path now reuses TudouClaw's
_sanitize_messages_for_openai, so mimo's thinking-mode requirement
(reasoning_content roundtrip on assistant messages) is satisfied
without touching SDK internals.

Pre-fix this would 400 with:
  "The reasoning_content in the thinking mode must be passed back."

Post-fix the wrapper backfills a placeholder reasoning_content on
assistant messages with tool_calls — same behavior legacy already had.

Run: python scripts/sdk_mimo_thinking_verify.py
"""
from __future__ import annotations
import asyncio, json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import llm
from agents import (
    Agent as SDKAgent, Runner, FunctionTool,
    OpenAIChatCompletionsModel,
)
from app.agent_runtime.sdk_adapter import _wrap_client_with_tudou_sanitizer
from openai import AsyncOpenAI

PROVIDER_ID = "537f1d05ab"   # 小米 mimo
MODEL_NAME = "mimo-v2.5-pro"


def make_tool(name: str, description: str, fake_result: str):
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    async def _invoke(ctx, args_json: str):
        def _sync():
            args = json.loads(args_json) if args_json else {}
            print(f"  → tool {name} called args={args}")
            return f"{fake_result} (query={args.get('query', '')})"
        return await asyncio.to_thread(_sync)
    return FunctionTool(
        name=name, description=description,
        params_json_schema=schema,
        on_invoke_tool=_invoke, strict_json_schema=False,
    )


async def main():
    reg = llm.get_registry()
    entry = reg.get(PROVIDER_ID)
    if entry is None:
        print(f"FAIL: provider {PROVIDER_ID} not in registry"); return 1

    client = AsyncOpenAI(base_url=entry.base_url, api_key=entry.api_key)
    # ── Apply the sanitizer wrap (this is what the fix does) ──
    _wrap_client_with_tudou_sanitizer(client, entry.base_url, MODEL_NAME)

    model = OpenAIChatCompletionsModel(
        model=MODEL_NAME, openai_client=client)

    tools = [
        make_tool("get_weather", "Get weather", "Sunny, 24°C"),
        make_tool("get_time", "Get time", "2026-05-16 14:23 PST"),
    ]

    sdk_agent = SDKAgent(
        name="mimo-verifier",
        instructions=(
            "You are a helpful assistant. Use tools to answer factual "
            "questions. You may call tools in parallel."),
        tools=tools, model=model,
    )

    prompt = (
        "What's the weather in Beijing AND current time in PST? "
        "Use tools.")

    print(f"--- mimo non-streaming + tools + sanitizer wrap ---")
    print(f"prompt: {prompt}\n")
    try:
        result = await Runner.run(sdk_agent, prompt)
        print(f"\n✅ SUCCESS — no 400")
        print(f"final_output: {result.final_output[:400]}")
        return 0
    except Exception as e:
        print(f"\n❌ FAIL: {type(e).__name__}: {str(e)[:300]}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
