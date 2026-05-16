# OpenAI Agents SDK — TudouClaw cheatsheet

Quick reference for working with `openai-agents` in TudouClaw.
Generated during 0c (Phase 0 connectivity validation).

## Install

```bash
pip install openai-agents
```

Currently pinned at `0.17.2` (2026-05-16). Bumps a transitive
`openai` package dependency from `2.24.0` → `2.37.0`; **this
breaks `litellm 1.83.10` which pins `openai==2.24.0`**. We don't
use litellm in production, but be aware if you `pip list`.

## Connectivity verified

`scripts/sdk_mimo_connectivity_poc.py` proves the SDK can talk to
TudouClaw's existing OpenAI-compat providers (BigModel/GLM tested;
mimo / qwen / deepseek expected to work the same way — they all
use the OpenAI Chat Completions protocol).

```bash
# Quickest run (uses first provider in ~/.tudou_claw/providers.json)
python scripts/sdk_mimo_connectivity_poc.py

# Override provider explicitly
SDK_POC_BASE_URL=https://open.bigmodel.cn/api/paas/v4 \
SDK_POC_API_KEY=$KEY \
SDK_POC_MODEL=glm-4-flash \
  python scripts/sdk_mimo_connectivity_poc.py
```

Successful output ends with:

```
Step 4: build Agent + Runner.run_sync
  ✓ Agent constructed
  ✓ Run completed

  Final output: 'Echoed message: TUDOUCLAW'
```

## Core API surface

```python
from agents import (
    Agent,                          # the agent dataclass
    Runner,                         # sync / async / streaming runner
    OpenAIChatCompletionsModel,     # OpenAI-compat backend
    function_tool,                  # @decorator turning a Python fn into a tool
    set_tracing_disabled,           # turn off tracing if no Anthropic API key
)
from openai import AsyncOpenAI
```

### Build a Model pointing at any OpenAI-compat URL

```python
client = AsyncOpenAI(
    base_url="http://localhost:11434/v1",   # mimo / ollama / vLLM / etc.
    api_key="dummy-or-real",                 # per provider
)
model = OpenAIChatCompletionsModel(
    model="mimo-v2.5-pro",                   # exact name on the endpoint
    openai_client=client,
)
```

This is **the** integration point — no LiteLLM, no ModelProvider
indirection, no extras. Same pattern works for mimo / qwen /
deepseek / glm / openrouter / anything OpenAI-shaped.

### Define a function_tool

```python
@function_tool
def get_weather(city: str) -> str:
    """Return the weather for a city. Just an example."""
    return f"It's sunny in {city}."
```

Decorator inspects the type annotations + docstring to build the
JSON schema. Sync or async functions both work.

### Build + run an Agent

```python
agent = Agent(
    name="MyAgent",
    instructions="You are a helpful assistant.",
    model=model,
    tools=[get_weather],
)

# Sync (blocks, simplest)
result = Runner.run_sync(agent, "What's the weather in Tokyo?")
print(result.final_output)

# Async
import asyncio
result = await Runner.run(agent, "What's the weather?")

# Streaming (we'll need this in 0d)
async for event in Runner.run_streamed(agent, "...").stream_events():
    if event.type == "raw_response_event":
        # text_delta / tool_use / etc.
        ...
```

### Lifecycle hooks

```python
from agents import RunHooks

class MyHooks(RunHooks):
    async def on_agent_start(self, ctx, agent): ...
    async def on_agent_end(self, ctx, agent, output): ...
    async def on_llm_start(self, ctx, agent, system, input_items): ...
    async def on_llm_end(self, ctx, agent, response): ...
    async def on_tool_start(self, ctx, agent, tool): ...
    async def on_tool_end(self, ctx, agent, tool, result): ...
    async def on_handoff(self, ctx, from_agent, to_agent): ...

Runner.run_sync(agent, "...", hooks=MyHooks())
```

We use these in `app/agent_runtime/hooks.py` to:
  - `on_llm_end` — call `app.runtime.evaluate_nudge`, inject any
    nudge into the conversation
  - `on_tool_end` — update L3 action buffer
  - `on_agent_end` — flush L3 facts via memory manager

## Disable tracing (no Anthropic API key)

```python
from agents import set_tracing_disabled
set_tracing_disabled(disabled=True)
```

Otherwise the SDK tries to emit traces to the Anthropic platform
and warns about a missing key.

## TudouClaw integration points

```
A: app/agent.py                      ← legacy chat loop (still default)
B: app/runtime/                      ← intent / nudges / filters (shared)
C: app/agent_runtime/                ← SDK adapter (lazy-load SDK)
   ├── sdk_adapter.py:SDKAgentRunner ← entry point; called from A.chat()
   │                                   when agent.runtime_mode == "sdk"
   ├── instructions_builder.py        ← wraps _build_static_system_prompt
   ├── tool_registry.py               ← TudouClaw tools → @function_tool
   ├── event_bridge.py                ← SDK events → portal AgentEvent
   └── hooks.py                       ← RunHooks calling B + L3 memory
```

## Common gotchas

- **`final_output` is the LAST text** the model produced; tool
  results are NOT in there. Walk `result.new_items` for tool
  call/result detail.
- **Sync `Runner.run_sync` blocks**, no asyncio escape. For the
  TudouClaw integration we use async + portal's existing event
  loop.
- **`Agent(model=...)` is per-agent**; if you need per-call model
  override, pass `RunConfig(model=...)` to `Runner.run`.
- **Tracing is on by default** — turn off if you don't have the
  platform key (warning otherwise).
- **Function names** in `@function_tool` come from the Python
  function name; the decorator sets `tool.name`.

## Phase 0 verdict

✅ **Phase 0 PASS** — SDK can connect to TudouClaw's existing
OpenAI-compat providers, dispatch tool calls, and return
structured results. **Phase 1+ migration is unblocked.**

Next (Phase 1 / 0d): fill in `SDKAgentRunner.run()` actual body
to bridge SDK streaming → portal events.
