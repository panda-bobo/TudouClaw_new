"""Verification: SDK non-streaming Runner.run + parallel tool_calls.

Replays 小土's exact failure scenario but with the FIX in place:
  - DeepSeek (deepseek-v4-flash via api.deepseek.com)
  - Multiple FunctionTools (the SDK previously dropped a tool_result
    on the streaming path, producing 400 orphan tool_calls)
  - asyncio.to_thread sync dispatch
  - Hooks attached

Pre-fix this would 400. Post-fix should print the assistant's reply
cleanly with all tools invoked + results.

Run:  python scripts/sdk_nonstreaming_verify.py
"""
from __future__ import annotations
import asyncio, json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import llm
from openai import AsyncOpenAI
from agents import (
    Agent as SDKAgent, Runner, FunctionTool,
    OpenAIChatCompletionsModel,
)

PROVIDER_ID = "d263d3a72b"   # DeepSeek
MODEL_NAME = "deepseek-v4-flash"


def make_tool(name: str, description: str, fake_result: str):
    """Build a FunctionTool that runs in a thread (matches the
    asyncio.to_thread wrap in tool_registry.py)."""
    schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
        },
        "required": ["query"],
    }

    async def _invoke(ctx, args_json: str):
        # Parse + dispatch in a thread, mirroring tool_registry.py
        def _sync():
            args = json.loads(args_json) if args_json else {}
            print(f"  → tool {name} called args={args}")
            return f"{fake_result} (query={args.get('query', '')})"

        return await asyncio.to_thread(_sync)

    return FunctionTool(
        name=name,
        description=description,
        params_json_schema=schema,
        on_invoke_tool=_invoke,
        strict_json_schema=False,
    )


async def main():
    reg = llm.get_registry()
    entry = reg.get(PROVIDER_ID)
    if entry is None:
        print(f"FAIL: provider {PROVIDER_ID} not in registry"); return 1

    client = AsyncOpenAI(
        base_url=entry.base_url, api_key=entry.api_key)
    model = OpenAIChatCompletionsModel(
        model=MODEL_NAME, openai_client=client)

    tools = [
        make_tool("get_weather", "Get weather in a city",
                  "Sunny, 24°C"),
        make_tool("get_time", "Get current time in a timezone",
                  "2026-05-16 14:23 PST"),
        make_tool("get_news", "Get top news headlines",
                  "Tech: New AI breakthrough"),
    ]

    sdk_agent = SDKAgent(
        name="verifier",
        instructions=(
            "You are a helpful assistant. When asked, call tools to "
            "answer. You can call multiple tools in parallel."),
        tools=tools,
        model=model,
    )

    prompt = (
        "Tell me: weather in Beijing, current time in PST timezone, "
        "and top news headline today. Use the available tools.")

    print(f"--- non-streaming Runner.run with {len(tools)} tools ---")
    print(f"prompt: {prompt}\n")
    try:
        result = await Runner.run(sdk_agent, prompt)
        print(f"\n✅ SUCCESS — no 400")
        print(f"final_output: {result.final_output[:500]}")
        print(f"new_items count: {len(result.new_items)}")
        for i, it in enumerate(result.new_items):
            print(f"  [{i}] {type(it).__name__} type={getattr(it, 'type', '?')}")
        return 0
    except Exception as e:
        print(f"\n❌ FAIL: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
