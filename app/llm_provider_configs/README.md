# LLM Provider Schema (yaml overlay)

每个文件 = 一个 provider 的 schema，文件名 `<provider.name>.yaml`。
启动时 yaml 字段写到 Python provider 实例上,作为 single source of truth.

约定:**每个 provider 都建一份 yaml**,即使所有字段跟 base default 一致 — 这样新成员看 `ls llm_provider_configs/` 就知道支持哪些 provider + 各自的怪癖。

当前已建 (11 个):
- `mimo.yaml`        — Xiaomi MiMo (思考模式)
- `deepseek.yaml`    — DeepSeek (思考模式)
- `glm.yaml`         — 智谱 GLM
- `qwen.yaml`        — 通义千问
- `volces.yaml`      — 火山引擎 Ark
- `groq.yaml`        — Groq Cloud (LPU 加速)
- `openai.yaml`      — OpenAI 基线
- `anthropic.yaml`   — Claude
- `ollama.yaml`      — 本地 :11434
- `lmstudio.yaml`    — 本地 :1234
- `mlx.yaml`         — 本地 Apple Silicon MLX :10240

加新 provider: 写 Python class (subclass `LLMProvider`) **+** 同名 yaml.

## 可覆盖字段 (白名单 `LLMProvider._OVERLAY_KEYS`)

| 字段 | 类型 | 说明 |
|------|------|------|
| `hosts` | list[str] | URL 子串匹配关键词,用来路由到这个 provider |
| `model_fragments` | list[str] | model 名子串路由 (URL 不匹配时兜底,用于 proxy / 测试) |
| `drop_reasoning_content` | bool | 发送前删除 `reasoning_content` 字段 |
| `backfill_reasoning_content` | bool | 给每条 assistant 补 `reasoning_content: ""` (DeepSeek/MiMo thinking-mode 必需) |
| `drop_empty_content_with_tools` | bool | assistant 同时有 tool_calls 且 content 为空时,删 content 字段 |
| `coerce_list_content_to_string` | bool | list 形式的 content 强制扁平化为字符串 |
| `drop_assistant_name` | bool | 删 assistant 上的 `name` 字段 |
| `supports_parallel_tool_calls_param` | bool | 是否在 payload 里发 `parallel_tool_calls: true` |
| `supports_vision` | bool | 是否接受 image_url content parts (false 时自动剥图) |
| `supports_temperature_param` | bool | 是否接受 temperature 参数 |
| `max_tool_call_rounds` | int | 历史里最多保留几轮 `(asst+tool_calls, tool*)`,超过的折叠成 user 文本 |

## 例: 临时给 Qwen 关闭并行 tool_call 字段

修 `qwen.yaml`:

```yaml
supports_parallel_tool_calls_param: false
max_tool_call_rounds: 1
```

重启进程即生效。

## 适用场景

- 接到新的模型变体 (如 `glm-4.5-air-vL` 行为变了),快速试改
- 操作员临时关闭某个怪癖修复来验证它是否还在生效
- 不同部署环境的同一 provider 行为略有差别

## 不适用场景

- 复杂逻辑 (自定义 `transform_message` / 合成 content 等) — 必须改 Python
- 加新 provider — 必须**同时**写 Python class **和** yaml
