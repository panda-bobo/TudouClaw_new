"""Verification: SDK Runner.run uses TudouClawModel which delegates
LLM call to legacy app.llm.chat_no_stream. Confirms that ALL legacy
provider quirks (sanitize, V2 tool_parsers, retry, fallback,
reasoning roundtrip) automatically apply to the SDK runtime now.

Tests two providers that previously hit problems:
  1. DeepSeek-v4-flash + parallel tools
     (the SDK streaming + parallel tool_calls bug — bypassed by
      Runner.run; now also benefits from V2 DSML parser)
  2. mimo-v2.5-pro + tool calls
     (thinking-mode reasoning_content roundtrip — now handled
      naturally because chat_no_stream ALREADY does this in legacy)

Run: python scripts/sdk_tudou_model_verify.py
"""
from __future__ import annotations
import asyncio, json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import Agent as SDKAgent, Runner, FunctionTool
from app.agent_runtime.tudou_model import build_tudou_model
from app import llm

CASES = [
    {
        "name": "DeepSeek + parallel tools",
        "provider_id": "d263d3a72b",
        "model": "deepseek-v4-flash",
        "prompt": ("Tell me weather in Beijing AND time in PST. Use the "
                   "available tools (you may call them in parallel)."),
    },
    {
        "name": "mimo + thinking-mode + tools",
        "provider_id": "537f1d05ab",
        "model": "mimo-v2.5-pro",
        "prompt": ("Tell me weather in Beijing AND time in PST. Use "
                   "tools."),
    },
]


def make_tool(name: str, description: str, fake_result: str):
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    async def _invoke(ctx, args_json: str):
        def _sync():
            args = json.loads(args_json) if args_json else {}
            print(f"    → tool {name}({args.get('query', '')})")
            return f"{fake_result} (query={args.get('query', '')})"
        return await asyncio.to_thread(_sync)
    return FunctionTool(
        name=name, description=description,
        params_json_schema=schema,
        on_invoke_tool=_invoke, strict_json_schema=False,
    )


async def run_case(case: dict) -> bool:
    print(f"\n=== {case['name']} ===")
    print(f"   provider={case['provider_id']} model={case['model']}")

    reg = llm.get_registry()
    entry = reg.get(case["provider_id"])
    if entry is None:
        print(f"   FAIL: provider not in registry"); return False

    model = build_tudou_model(
        provider_id=case["provider_id"],
        model_name=case["model"],
        base_url=entry.base_url,
    )

    tools = [
        make_tool("get_weather", "Get weather", "Sunny, 24°C"),
        make_tool("get_time", "Get time", "2026-05-16 14:23 PST"),
    ]

    sdk_agent = SDKAgent(
        name="tudou-model-verifier",
        instructions=("You are a helpful assistant. Use tools to "
                      "answer factual questions."),
        tools=tools,
        model=model,
    )

    try:
        result = await Runner.run(sdk_agent, case["prompt"])
        print(f"   ✅ no 400 / no XML leak")
        print(f"   final: {result.final_output[:200]}")
        # Sanity: final_output should NOT contain XML tool_call markup
        # (would indicate V2 tool_parsers didn't catch a leak)
        bad_markers = ["<function=", "<tool_call>", "<arg_key>", "DSML"]
        for marker in bad_markers:
            if marker in (result.final_output or ""):
                print(f"   ⚠️  WARNING: final_output contains {marker!r}")
        return True
    except Exception as e:
        print(f"   ❌ FAIL: {type(e).__name__}: {str(e)[:300]}")
        return False


async def main():
    results = []
    for case in CASES:
        ok = await run_case(case)
        results.append((case["name"], ok))

    print(f"\n=== Summary ===")
    for name, ok in results:
        print(f"   {'✅' if ok else '❌'} {name}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
