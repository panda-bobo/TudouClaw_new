"""End-to-end verification: TudouClawModel.stream_response works
with Runner.run_streamed + DeepSeek + parallel tool_calls.

This is the scenario that originally bit us (commit 86212c5 fix was
to switch to non-streaming Runner.run). If THIS verifies passes,
streaming is back online and the original SDK bug is bypassed by
yielding complete tool_calls instead of letting SDK accumulate
deltas.

Also asserts ResponseTextDeltaEvent fires — which is what voice
mode's sentence-level TTS needs.

Run: python scripts/sdk_streaming_verify.py
"""
from __future__ import annotations
import asyncio, json, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agents import Agent as SDKAgent, Runner, FunctionTool
from app.agent_runtime.tudou_model import build_tudou_model
from app import llm


def make_tool(name: str, desc: str, fake: str):
    schema = {
        "type": "object",
        "properties": {"q": {"type": "string"}},
        "required": ["q"],
    }
    async def _invoke(ctx, args_json):
        def _sync():
            args = json.loads(args_json) if args_json else {}
            print(f"   → {name}({args.get('q', '')})")
            return f"{fake} (q={args.get('q', '')})"
        return await asyncio.to_thread(_sync)
    return FunctionTool(
        name=name, description=desc,
        params_json_schema=schema,
        on_invoke_tool=_invoke, strict_json_schema=False,
    )


async def go():
    reg = llm.get_registry()
    entry = reg.get("d263d3a72b")  # DeepSeek
    model = build_tudou_model(
        provider_id="d263d3a72b",
        model_name="deepseek-v4-flash",
        base_url=entry.base_url)

    tools = [
        make_tool("get_weather", "weather", "Sunny 24C"),
        make_tool("get_time", "time", "2pm PST"),
    ]
    agent = SDKAgent(
        name="streaming-test", model=model, tools=tools,
        instructions="Use tools (can be parallel). Then summarize.")

    prompt = ("天气 + 时间 PST? 用工具。")
    print(f"=== Runner.run_streamed with TudouClawModel.stream_response ===")
    print(f"prompt: {prompt}\n")

    stream = Runner.run_streamed(agent, prompt, max_turns=10)
    text_delta_count = 0
    tool_call_count = 0
    text_accum = ""
    async for ev in stream.stream_events():
        ev_type = getattr(ev, "type", "")
        if ev_type == "raw_response_event":
            data = getattr(ev, "data", None)
            dt = getattr(data, "type", "") if data else ""
            if dt == "response.output_text.delta":
                chunk = getattr(data, "delta", "")
                text_accum += chunk
                text_delta_count += 1
        elif ev_type == "run_item_stream_event":
            item = getattr(ev, "item", None)
            it = getattr(item, "type", "") if item else ""
            if it == "tool_call_item":
                tool_call_count += 1
                raw = getattr(item, "raw_item", None)
                if raw:
                    print(f"   tool_call dispatched: {getattr(raw, 'name', '?')}")

    final = getattr(stream, "final_output", "") or ""
    print(f"\n=== Results ===")
    print(f"text_delta events: {text_delta_count}")
    print(f"tool_calls: {tool_call_count}")
    print(f"final text: {final[:200]}")
    print(f"accumulated stream text: {text_accum[:200]}")

    # Assertions
    if tool_call_count != 2:
        print(f"❌ FAIL: expected 2 tool calls, got {tool_call_count}")
        return 1
    if text_delta_count < 5:
        print(f"❌ FAIL: too few text_delta events ({text_delta_count}) — "
              f"streaming likely not working")
        return 1
    if not final:
        print(f"❌ FAIL: no final text")
        return 1
    print(f"\n✅ SUCCESS — streaming works, no 400, no orphan tool_calls")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(go()))
