"""LLM Provider Adapters — one autonomous object per provider.

Design: every LLM is its own self-contained object. It owns:
  - URL detection (which endpoints it serves)
  - field schema (what fields it requires/forbids on assistant/tool msgs)
  - any payload transformation (default: noop)

Adding a new LLM = subclass `LLMProvider`, set `hosts`, override hooks
you care about, and `register_provider(MyProvider())`. No central
if/else, no shared mutation of global state, no edits to chat path.

Hook points (override what you need):
  matches(url)         → bool, default: substring match on `hosts`
  transform_message(m) → mutate one msg in place; default: strip
                         reasoning_content (the most universally-rejected
                         unknown field)
  transform_payload(p) → mutate request payload before send (default:
                         noop); use for things like custom headers in
                         payload, vendor-specific top-level fields
  fold_excess_tool_rounds(messages) → list[dict]; cap multi-round tool
                         history (some providers reject >N rounds)

Each subclass declares only what's different. Common universal cleanup
stays in the sanitizer; per-provider quirks stay in the class.

YAML config overlay:
  At init time, if `app/llm_provider_configs/<name>.yaml` exists, its
  fields override the class attributes. Lets operators flip flags for
  a provider without editing code (e.g. trying a new model variant
  with different quirks). See `_load_yaml_overrides`.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

logger = logging.getLogger("tudou.llm.providers")

# Directory of optional YAML overlays. Files named after provider.name
# (e.g. `glm.yaml`, `qwen.yaml`) override class attributes at init.
_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "llm_provider_configs")


def _ensure_str(content: Any) -> str:
    """Normalize message content to a plain string.

    Some code paths may store content as a list of content blocks
    (OpenAI multimodal format) or other non-string types. APIs like
    Qwen / LM Studio reject non-string content with 400. This helper
    ensures a plain string.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif "text" in block:
                    parts.append(block["text"])
                else:
                    parts.append(json.dumps(block, ensure_ascii=False))
            elif isinstance(block, str):
                parts.append(block)
            else:
                parts.append(str(block))
        return "\n".join(parts)
    if isinstance(content, dict):
        return json.dumps(content, ensure_ascii=False)
    return str(content)


# ─────────────────────────────────────────────────────────────────────
# Base adapter
# ─────────────────────────────────────────────────────────────────────

class LLMProvider:
    """Base class for an LLM provider adapter.

    Subclass per provider. All quirks are encapsulated; framework code
    treats this as an opaque object via the public methods below.
    """
    # Identity — override in subclass
    name: str = "generic"
    hosts: tuple = ()

    # Field policy — declare quirks via class fields where possible
    # (subclass overrides these as simple attributes; rarely need to
    # override `transform_message` itself)
    drop_reasoning_content: bool = True
    backfill_reasoning_content: bool = False
    drop_empty_content_with_tools: bool = False
    coerce_list_content_to_string: bool = False
    drop_assistant_name: bool = False

    # Whether this provider's OpenAI-compat endpoint accepts the
    # `parallel_tool_calls: bool` request parameter. OpenAI ships it,
    # DeepSeek and most major US providers ship it; some Chinese
    # vendors (zhipu/GLM) don't recognize it and 400 on the field.
    # Default False — be conservative; opt in per-provider when verified.
    supports_parallel_tool_calls_param: bool = False

    # Whether this provider's chat-completions endpoint accepts the
    # multimodal content shape `[{type:"image_url", image_url:{url:...}}]`.
    # When False, the sanitizer downgrades image parts to text placeholders
    # before sending — otherwise the API 400s with "unknown variant
    # `image_url`" (DeepSeek as of 2026-04). Default True — be permissive
    # for vision-capable providers (OpenAI / Anthropic / Qwen-VL); opt out
    # per-provider when the chat endpoint is verified text-only.
    supports_vision: bool = True

    # Whether this provider's chat-completions endpoint accepts the
    # `temperature` parameter at all. Most do; OpenAI's o1 family rejects
    # it. Default True. (Caller still gates on temperature >= 0; this is
    # a hard gate on top of that — even temperature=0 gets stripped when
    # the provider doesn't accept the field.)
    supports_temperature_param: bool = True

    # Max number of (assistant+tool_calls, tool*) rounds the provider
    # accepts in one request. 0 = unlimited.
    #
    # Default = 1 (universal lowest-common-denominator). Older tool
    # rounds get folded into a single user-role text message so the wire
    # format always looks like:  sys, user(history), asst+tc, tool ...
    # Every OpenAI-compat provider accepts this shape.
    max_tool_call_rounds: int = 1

    # Class attribute names that the YAML overlay is allowed to set.
    # Whitelist so a stray YAML field can't poison unrelated behavior.
    _OVERLAY_KEYS: tuple = (
        "hosts",
        "drop_reasoning_content",
        "backfill_reasoning_content",
        "drop_empty_content_with_tools",
        "coerce_list_content_to_string",
        "drop_assistant_name",
        "supports_parallel_tool_calls_param",
        "supports_vision",
        "supports_temperature_param",
        "max_tool_call_rounds",
    )

    def __init__(self) -> None:
        self._load_yaml_overrides()

    # ── YAML overlay (operator-tunable quirks) ────────────────────
    def _load_yaml_overrides(self) -> None:
        """Apply overrides from `llm_provider_configs/<name>.yaml`.

        File format::
          hosts: [some-host.com]
          drop_empty_content_with_tools: true
          max_tool_call_rounds: 1
          ...

        Only keys in `_OVERLAY_KEYS` are honored. `hosts` is normalized
        to a tuple. Missing file = no-op.
        """
        path = os.path.join(_CONFIG_DIR, f"{self.name}.yaml")
        if not os.path.exists(path):
            return
        try:
            import yaml  # type: ignore
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            if not isinstance(data, dict):
                return
            applied = []
            for k, v in data.items():
                if k not in self._OVERLAY_KEYS:
                    continue
                if k == "hosts" and isinstance(v, list):
                    v = tuple(v)
                setattr(self, k, v)
                applied.append(k)
            if applied:
                logger.info(
                    "LLMProvider %s: YAML overlay applied (%s) from %s",
                    self.name, ",".join(applied), path,
                )
        except Exception as e:
            logger.warning(
                "LLMProvider %s: YAML overlay failed (%s): %s",
                self.name, path, e,
            )

    # ── URL routing ────────────────────────────────────────────────
    def matches(self, url: str) -> bool:
        if not url or not self.hosts:
            return False
        u = url.lower()
        return any(h in u for h in self.hosts)

    # ── Per-message transform (data-driven by class attrs) ────────
    def transform_message(self, m: dict) -> None:
        if m.get("role") != "assistant":
            return
        if self.backfill_reasoning_content and "reasoning_content" not in m:
            m["reasoning_content"] = ""
        elif self.drop_reasoning_content:
            m.pop("reasoning_content", None)
        if self.drop_empty_content_with_tools \
                and m.get("tool_calls") \
                and m.get("content") in (None, ""):
            m.pop("content", None)
        if self.coerce_list_content_to_string \
                and isinstance(m.get("content"), list):
            m["content"] = _ensure_str(m["content"])
        if self.drop_assistant_name:
            m.pop("name", None)

    # ── Per-payload transform (override for vendor-specific top-level
    #     payload mutations e.g. extra header fields injected into body)
    def transform_payload(self, payload: dict) -> dict:
        return payload

    # ── Fold excess tool-call rounds for providers that cap them ──
    def fold_excess_tool_rounds(self, messages: list) -> list:
        """Compress old (assistant+tool_calls, tool*) rounds into a
        single user-role text message when the provider caps rounds.

        Preserves the assistant prose, the tool name + arguments, and
        the FULL tool result (no truncation). Only the most recent
        ``max_tool_call_rounds`` rounds keep their real
        ``tool_calls``/``tool`` structure.
        """
        cap = self.max_tool_call_rounds
        if cap <= 0 or not messages:
            return messages

        # Index every (assistant+tool_calls, tool, tool, ...) round.
        rounds: list[tuple[int, int]] = []
        i = 0
        n = len(messages)
        while i < n:
            m = messages[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                j = i + 1
                while j < n and messages[j].get("role") == "tool":
                    j += 1
                rounds.append((i, j))
                i = j
            else:
                i += 1

        if len(rounds) <= cap:
            return messages

        fold_until = len(rounds) - cap   # fold rounds [0 .. fold_until)
        fold_ranges = {rounds[k] for k in range(fold_until)}

        out: list = []
        skip_until = -1
        for idx, m in enumerate(messages):
            if idx < skip_until:
                continue
            match = next((r for r in fold_ranges if r[0] == idx), None)
            if match is not None:
                s, e = match
                folded = self._fold_round_to_text(messages[s:e])
                # Merge into the previous folded user msg if it sits
                # immediately before us — many providers reject two
                # consecutive same-role messages.
                if (out
                        and out[-1].get("role") == "user"
                        and isinstance(out[-1].get("content"), str)
                        and isinstance(folded.get("content"), str)):
                    out[-1] = {
                        "role": "user",
                        "content": out[-1]["content"] + "\n\n" + folded["content"],
                    }
                else:
                    out.append(folded)
                skip_until = e
                continue
            out.append(m)
        return out

    @staticmethod
    def _fold_round_to_text(round_msgs: list) -> dict:
        """Render one tool-call round as a single ``user`` text message.

        Uses ``user`` role (not ``assistant``) so that the wire format
        always alternates sys → user → assistant → tool — the OpenAI
        lowest-common-denominator that GLM-4.5-air, Qwen, etc. accept.
        Prefixed so the model parses it as historical context, not a
        live user request.
        """
        asst = round_msgs[0]
        tools = round_msgs[1:]

        parts: list[str] = []
        c = asst.get("content")
        if isinstance(c, str) and c.strip():
            parts.append(c.strip())

        results_by_id: dict[str, str] = {}
        for t in tools:
            tcid = t.get("tool_call_id") or ""
            if not tcid:
                continue
            tc_content = t.get("content")
            if isinstance(tc_content, str):
                results_by_id[tcid] = tc_content
            elif tc_content is not None:
                results_by_id[tcid] = str(tc_content)
            else:
                results_by_id[tcid] = ""

        for tc in (asst.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = fn.get("name", "?") if isinstance(fn, dict) else "?"
            args = fn.get("arguments", "") if isinstance(fn, dict) else ""
            tcid = tc.get("id") or ""
            result = results_by_id.get(tcid, "")
            # Tool args can be huge — write_file's `content` arg is
            # typically a multi-KB code body. Folding pushes that mass
            # into a user-role text message that's then re-sent every
            # turn AND surfaced verbatim in chat. Truncate aggressively;
            # the tool's RESULT (already executed) is what matters for
            # context, not re-quoting the input.
            if isinstance(args, str) and len(args) > 600:
                head = args[:400]
                args_summary = (
                    f"{head}…[truncated {len(args)-400} chars]"
                )
            else:
                args_summary = args
            parts.append(
                f"[Called {name}({args_summary})]\n[Result]\n{result}"
            )

        body = "\n\n".join(parts) if parts else "(folded tool round)"
        return {
            "role": "user",
            "content": "[Earlier tool execution]\n\n" + body,
        }


# ─────────────────────────────────────────────────────────────────────
# Registry (runtime, replaceable)
# ─────────────────────────────────────────────────────────────────────

_provider_adapters: list[LLMProvider] = []
_default_adapter: LLMProvider = LLMProvider()


def register_provider(adapter: LLMProvider) -> None:
    """Register an LLM adapter. Call at import time from anywhere.

    Last-registered wins on URL ambiguity → user plugins override built-ins.
    """
    _provider_adapters.append(adapter)


def resolve_strategy(url: str) -> LLMProvider:
    """Return the most-recently-registered adapter that matches the URL,
    or the default adapter if none match."""
    for adapter in reversed(_provider_adapters):
        if adapter.matches(url):
            return adapter
    return _default_adapter


def _detect_provider_kind(target_url: str) -> str:   # legacy import name
    return resolve_strategy(target_url).name


# Backward-compat alias — some external callers/tests might import this.
ProviderStrategy = LLMProvider


# ─────────────────────────────────────────────────────────────────────
# Built-in adapters
# Each is one self-contained class declaring its own quirks via class
# attributes. To add a new LLM: subclass LLMProvider, set fields,
# call register_provider(YourProvider()).
# ─────────────────────────────────────────────────────────────────────

class OpenAIProvider(LLMProvider):
    name = "openai"
    hosts = ("openai.com", "api.openai")
    supports_parallel_tool_calls_param = True


class AnthropicProvider(LLMProvider):
    name = "anthropic"
    hosts = ("anthropic.com",)


class DeepSeekProvider(LLMProvider):
    name = "deepseek"
    hosts = ("deepseek",)
    drop_reasoning_content = False           # MUST keep
    backfill_reasoning_content = True        # required on every assistant
    drop_empty_content_with_tools = True
    supports_parallel_tool_calls_param = True
    # DeepSeek's `api.deepseek.com/v1/chat/completions` is text-only — the
    # JSON schema rejects `image_url` content parts with 400 "unknown
    # variant". Their vision model (deepseek-vl) lives on a separate
    # endpoint that the framework would target via a different provider.
    supports_vision = False


class GLMProvider(LLMProvider):
    name = "glm"
    hosts = ("bigmodel.cn", "open.bigmodel")
    coerce_list_content_to_string = True
    # max_tool_call_rounds inherits = 1 from base (GLM-4.5-air requires it).
    # NB: do NOT set drop_empty_content_with_tools — GLM rejects both
    # missing and null content. We synthesize a description below.

    def transform_message(self, m: dict) -> None:
        super().transform_message(m)
        # GLM-4.5-air rejects assistant content that is empty, null, or
        # whitespace/punctuation-only. When the assistant turn is purely
        # tool-call(s) with no prose, synthesize a short description from
        # the tool name(s).
        if m.get("role") == "assistant":
            c = m.get("content")
            has_text = isinstance(c, str) and any(ch.isalnum() for ch in c)
            if not has_text:
                tcs = m.get("tool_calls") or []
                names = []
                for tc in tcs:
                    if not isinstance(tc, dict):
                        continue
                    fn = tc.get("function") or {}
                    nm = fn.get("name") if isinstance(fn, dict) else None
                    if nm:
                        names.append(nm)
                m["content"] = f"调用 {', '.join(names)}" if names else "继续"


class QwenProvider(LLMProvider):
    name = "qwen"
    hosts = ("dashscope", "aliyuncs", "qwen")
    drop_empty_content_with_tools = True
    coerce_list_content_to_string = True


class VolcesProvider(LLMProvider):
    name = "volces"
    hosts = ("volces.com", "ark.cn-beijing")
    coerce_list_content_to_string = True


class OllamaProvider(LLMProvider):
    name = "ollama"
    hosts = ("11434", "/ollama")
    drop_empty_content_with_tools = True
    drop_assistant_name = True


class LMStudioProvider(LLMProvider):
    name = "lmstudio"
    hosts = ("1234", "lmstudio")
    drop_empty_content_with_tools = True


# Register at import time. Order: less-specific first, more-specific last
# (since last-registered wins on URL ambiguity).
for _adapter in (OpenAIProvider(), AnthropicProvider(),
                 DeepSeekProvider(), GLMProvider(), QwenProvider(),
                 VolcesProvider(), OllamaProvider(), LMStudioProvider()):
    register_provider(_adapter)


__all__ = [
    "LLMProvider",
    "ProviderStrategy",
    "register_provider",
    "resolve_strategy",
    "_detect_provider_kind",
    "OpenAIProvider",
    "AnthropicProvider",
    "DeepSeekProvider",
    "GLMProvider",
    "QwenProvider",
    "VolcesProvider",
    "OllamaProvider",
    "LMStudioProvider",
]
