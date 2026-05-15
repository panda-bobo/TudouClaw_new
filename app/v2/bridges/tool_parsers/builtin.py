"""
Built-in sample parsers.

These are REFERENCE implementations — they demonstrate the three common
tool-call emission patterns modern models use. Adding a new model family
often means picking one of these with different config, not writing a
new parser class.

    OpenAIPassthroughParser — provider-normalized tool_calls already
    XMLTagJSONParser        — <tool_call>{...}</tool_call> in content
    JSONOnlyParser          — bare JSON object in content

None of these reference any specific model name. Model-to-parser
mapping lives in ``config/tool_parsers.yaml`` — see ``discovery.py``.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field

from .base import NormalizedMessage, ToolCallParser


logger = logging.getLogger("tudouclaw.v2.tool_parsers")


# ── shared helpers ────────────────────────────────────────────────────


def _robust_json_loads(text: str):
    """Parse JSON with graceful recovery from common LLM mistakes.

    Uses ``json_repair`` when available (trailing commas, unquoted keys,
    truncated output). Falls back to stdlib ``json.loads``. Returns the
    parsed object or ``None`` on irrecoverable failure.
    """
    if not text or not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        import json_repair  # type: ignore
        return json_repair.loads(text)
    except Exception:
        return None


def _new_call_id(prefix: str = "call") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _coerce_tool_call(
    name: str,
    arguments,
    call_id: str = "",
) -> dict:
    """Build an OpenAI-shaped tool_call dict from loose inputs."""
    if isinstance(arguments, (dict, list)):
        args_str = json.dumps(arguments, ensure_ascii=False)
    elif arguments is None:
        args_str = "{}"
    else:
        args_str = str(arguments)
    return {
        "id": call_id or _new_call_id(),
        "type": "function",
        "function": {"name": str(name or ""), "arguments": args_str},
    }


# ── 1. OpenAIPassthrough ──────────────────────────────────────────────


@dataclass
class OpenAIPassthroughParser(ToolCallParser):
    """The provider already returned OpenAI-shaped ``tool_calls`` — trust it.

    This is the safe default for any endpoint that speaks the OpenAI
    Chat Completions wire format correctly (OpenAI, Anthropic via
    adapter, DeepSeek, most cloud APIs).
    """

    name: str = "openai_passthrough"

    def parse(self, raw_message: dict) -> NormalizedMessage:
        role = str(raw_message.get("role") or "assistant")
        content = str(raw_message.get("content") or "")
        tcs: list[dict] = []
        for tc in (raw_message.get("tool_calls") or []):
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            tname = tc.get("name") or fn.get("name") or ""
            targs = tc.get("arguments") if "arguments" in tc else fn.get("arguments")
            # Some Anthropic-adapter shapes use ``input`` dict.
            if targs is None and "input" in tc:
                targs = tc["input"]
            tcs.append(_coerce_tool_call(
                name=tname, arguments=targs,
                call_id=str(tc.get("id") or ""),
            ))
        return NormalizedMessage(role=role, content=content, tool_calls=tcs)


# ── 2. XMLTagJSONParser ───────────────────────────────────────────────


@dataclass
class XMLTagJSONParser(ToolCallParser):
    """Parse tool calls fenced by XML-like markers containing JSON.

    Covers Qwen (``<tool_call>{...}</tool_call>``), Hermes, InternLM2,
    and most open-weight models trained on OpenAI-style function calling
    after 2024.

    Expected JSON shape inside the markers::

        {"name": "<tool>", "arguments": {...}}

    Config (passed by ``discovery`` from YAML)::

        open:        opening marker (default ``<tool_call>``)
        close:       closing marker (default ``</tool_call>``)
        name_key:    key for function name  (default ``name``)
        args_key:    key for arguments      (default ``arguments``)
        strip_content: remove marker blocks from content  (default True)
    """

    name: str = "xml_tag_json"
    open_tag: str = "<tool_call>"
    close_tag: str = "</tool_call>"
    name_key: str = "name"
    args_key: str = "arguments"
    strip_content: bool = True

    # Cache compiled regex per instance.
    _pattern: re.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        pat = re.escape(self.open_tag) + r"(.*?)" + re.escape(self.close_tag)
        self._pattern = re.compile(pat, re.DOTALL)

    def parse(self, raw_message: dict) -> NormalizedMessage:
        role = str(raw_message.get("role") or "assistant")
        content = str(raw_message.get("content") or "")

        # Respect any tool_calls already present on the response — some
        # servers half-parse. Run our extraction and merge.
        base = OpenAIPassthroughParser().parse(raw_message)

        matches = self._pattern.findall(content)
        tcs: list[dict] = list(base.tool_calls)
        for block in matches:
            payload = _robust_json_loads(block.strip())
            if not isinstance(payload, dict):
                continue
            tcs.append(_coerce_tool_call(
                name=payload.get(self.name_key, ""),
                arguments=payload.get(self.args_key),
            ))

        cleaned = content
        if self.strip_content and matches:
            cleaned = self._pattern.sub("", content).strip()

        return NormalizedMessage(role=role, content=cleaned, tool_calls=tcs)


# ── 3. JSONOnlyParser ─────────────────────────────────────────────────


@dataclass
class JSONOnlyParser(ToolCallParser):
    """Parse a bare JSON object in ``content`` as a tool call.

    Covers older / simpler models whose chat template emits the function
    call as pure JSON without any surrounding marker, e.g.::

        {"function_call": {"name": "x", "arguments": {...}}}
        or
        {"tool": "x", "args": {...}}

    Config::

        name_key: dotted path to tool name (default ``function_call.name``)
        args_key: dotted path to args      (default ``function_call.arguments``)
    """

    name: str = "json_only"
    name_key: str = "function_call.name"
    args_key: str = "function_call.arguments"

    def parse(self, raw_message: dict) -> NormalizedMessage:
        base = OpenAIPassthroughParser().parse(raw_message)
        if base.tool_calls:
            return base  # provider parsed already; don't double-emit
        content = base.content
        payload = _robust_json_loads(content)
        if not isinstance(payload, dict):
            return base

        name = _dig(payload, self.name_key)
        if not name:
            return base
        args = _dig(payload, self.args_key)
        tc = _coerce_tool_call(name=str(name), arguments=args)
        return NormalizedMessage(
            role=base.role, content="", tool_calls=[tc],
        )


def _dig(obj, dotted: str):
    cur = obj
    for part in (dotted or "").split("."):
        if not part:
            continue
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


# ── 4. GLMArgKVParser ─────────────────────────────────────────────────


@dataclass
class GLMArgKVParser(ToolCallParser):
    """Parse GLM-style XML tool_call with ``<arg_key>/<arg_value>`` pairs.

    Example content emitted by GLM-4.5-air::

        <tool_call>
        <arg_key>name</arg_key><arg_value>read_file</arg_value>
        <arg_key>path</arg_key><arg_value>x.md</arg_value>
        </tool_call>

    Or sometimes without the wrapper::

        <arg_key>name</arg_key><arg_value>edit_file</arg_value>...

    First ``<arg_key>name</arg_key>`` (case-insensitive) becomes the
    function name; remaining pairs become the arguments dict.
    """

    name: str = "glm_arg_kv"
    strip_content: bool = True

    _block_pat: re.Pattern = field(init=False, repr=False, compare=False)
    _pair_pat:  re.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        # A "block" of consecutive arg_key/arg_value pairs (with or
        # without enclosing <tool_call> wrapper).
        self._block_pat = re.compile(
            r"(?:<tool_call>\s*)?"
            r"((?:<arg_key>[\s\S]*?</arg_key>\s*<arg_value>[\s\S]*?</arg_value>\s*)+)"
            r"(?:</tool_call>)?",
            re.IGNORECASE,
        )
        self._pair_pat = re.compile(
            r"<arg_key>([\s\S]*?)</arg_key>\s*<arg_value>([\s\S]*?)</arg_value>",
            re.IGNORECASE,
        )

    def parse(self, raw_message: dict) -> NormalizedMessage:
        base = OpenAIPassthroughParser().parse(raw_message)
        # If provider already gave us tool_calls, only clean stray XML
        # markup from content and return.
        if base.tool_calls:
            cleaned = self._strip(base.content)
            return NormalizedMessage(
                role=base.role, content=cleaned,
                tool_calls=list(base.tool_calls),
            )

        content = base.content or ""
        if "<arg_key>" not in content:
            return base

        tcs: list[dict] = []
        for m in self._block_pat.finditer(content):
            inner = m.group(1)
            pairs = self._pair_pat.findall(inner)
            if not pairs:
                continue
            name = ""
            args: dict = {}
            for k, v in pairs:
                k_norm = k.strip()
                v_norm = v.strip()
                if not name and k_norm.lower() == "name":
                    name = v_norm
                else:
                    args[k_norm] = v_norm
            if not name:
                continue
            tcs.append(_coerce_tool_call(name=name, arguments=args))

        if not tcs:
            return base

        cleaned = self._strip(content) if self.strip_content else content
        return NormalizedMessage(
            role=base.role, content=cleaned, tool_calls=tcs,
        )

    def _strip(self, content: str) -> str:
        if not content:
            return content
        # Drop the parsed blocks from content so chat doesn't show
        # raw XML to the user.
        out = self._block_pat.sub("", content)
        return out.strip()


# ── 5. DSMLParser ─────────────────────────────────────────────────────


# Pipe character class — DSML uses full-width "｜" (U+FF5C) but some
# clients normalize to regular "|", so accept both. One bar OR more
# (the format in the wild varies between ｜ and ｜｜).
_DSML_BAR = r"[\|｜]+"


@dataclass
class DSMLParser(ToolCallParser):
    """Parse DeepSeek's "DSML" tool-call markup.

    DeepSeek's flash / chat variants sometimes emit tool calls as text
    using a custom XML-like dialect rather than the OpenAI tool_calls
    array, especially when the standard function-calling pathway gets
    stressed (long context, complex tool schema, retry after error).
    Format observed in the wild::

        <｜｜DSML｜｜tool_calls>
        <｜｜DSML｜｜invoke name="glob_files">
        <｜｜DSML｜｜parameter name="pattern" string="true">**/*</｜｜DSML｜｜parameter>
        <｜｜DSML｜｜parameter name="path" string="true">/foo</｜｜DSML｜｜parameter>
        </｜｜DSML｜｜invoke>
        </｜｜DSML｜｜tool_calls>

    Bar character is full-width U+FF5C in practice; we accept both that
    and ASCII "|" with one-or-more occurrences. First runs OpenAI
    passthrough so any provider-supplied tool_calls aren't lost; only
    extracts DSML when content contains the markup AND no native
    tool_calls came back.
    """

    name: str = "dsml"
    strip_content: bool = True

    _wrapper_pat: re.Pattern = field(init=False, repr=False, compare=False)
    _invoke_pat:  re.Pattern = field(init=False, repr=False, compare=False)
    _param_pat:   re.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        bar = _DSML_BAR
        # Outer tool_calls wrapper (optional — invokes can appear unwrapped)
        self._wrapper_pat = re.compile(
            rf"<{bar}DSML{bar}tool_calls>(.*?)</{bar}DSML{bar}tool_calls>",
            re.DOTALL,
        )
        # Each invoke block: name="X" with inner parameters
        self._invoke_pat = re.compile(
            rf"<{bar}DSML{bar}invoke\s+name=\"([^\"]+)\">(.*?)</{bar}DSML{bar}invoke>",
            re.DOTALL,
        )
        # Each parameter: name="key" [string="true"]>value</...>
        self._param_pat = re.compile(
            rf"<{bar}DSML{bar}parameter\s+name=\"([^\"]+)\"[^>]*>(.*?)</{bar}DSML{bar}parameter>",
            re.DOTALL,
        )

    def parse(self, raw_message: dict) -> NormalizedMessage:
        base = OpenAIPassthroughParser().parse(raw_message)
        content = base.content or ""
        if "DSML" not in content:
            return base
        # Find every invoke block (whether or not wrapped). Use the
        # wrapper-aware path first so we know which spans to strip; the
        # invoke pattern works at the top level too because invokes
        # carry their own opening/closing markers.
        invoke_matches = list(self._invoke_pat.finditer(content))
        if not invoke_matches:
            return base

        tcs: list[dict] = list(base.tool_calls)
        for m in invoke_matches:
            tool_name = m.group(1).strip()
            inner = m.group(2)
            args: dict = {}
            for pname, pval in self._param_pat.findall(inner):
                key = pname.strip()
                val = pval.strip()
                # DSML doesn't carry types — try light parsing so numeric
                # arguments don't reach the tool as strings. JSON parse
                # for objects/arrays/numbers/bools, fall back to string.
                if val and val[0] in '[{':
                    parsed = _robust_json_loads(val)
                    if parsed is not None:
                        args[key] = parsed
                        continue
                if val.lower() in ("true", "false"):
                    args[key] = (val.lower() == "true")
                    continue
                try:
                    if "." in val:
                        args[key] = float(val)
                    else:
                        args[key] = int(val)
                    continue
                except (ValueError, TypeError):
                    pass
                args[key] = val
            tcs.append(_coerce_tool_call(name=tool_name, arguments=args))

        cleaned = content
        if self.strip_content:
            # Remove wrapper + standalone invoke blocks
            cleaned = self._wrapper_pat.sub("", cleaned)
            cleaned = self._invoke_pat.sub("", cleaned)
            # Tidy up any leftover blank lines
            cleaned = re.sub(r"\n\s*\n+", "\n\n", cleaned).strip()

        return NormalizedMessage(
            role=base.role, content=cleaned, tool_calls=tcs,
        )


# ── 6. FunctionXMLParser ──────────────────────────────────────────────
#
# Hermes-2 / Functionary / mimo / some Qwen variants emit tool calls
# as nested XML where the function NAME and parameter KEYS are part of
# the tag name (not JSON values):
#
#     <tool_call>
#     <function=bash>
#     <parameter=command>cd /tmp && ls</parameter>
#     <parameter=timeout>30</parameter>
#     </function>
#     </tool_call>
#
# This is structurally distinct from XMLTagJSONParser (which expects
# JSON inside <tool_call>) and GLMArgKVParser (which uses
# <arg_key>/<arg_value> pairs). Real symptom (2026-05-15): mimo-v2.5-pro
# emitted the literal XML as text content, leaked to chat as
# "<tool_call> <function=bash> <parameter=command>cd ...".


# JSON-number regex: optional minus, digits, optional fractional part,
# optional exponent. Anchored — ensures the whole string is a number,
# not just starts with one.
_JSON_NUMBER_RE = re.compile(r"^-?\d+(?:\.\d+)?(?:[eE][+\-]?\d+)?$")


@dataclass
class FunctionXMLParser(ToolCallParser):
    """Parse Hermes/Functionary-style nested-XML tool calls.

    Expected shape::

        <tool_call>
        <function=NAME>
        <parameter=KEY1>VALUE1</parameter>
        <parameter=KEY2>VALUE2</parameter>
        </function>
        </tool_call>

    The outer ``<tool_call>`` wrapper is OPTIONAL (some variants emit
    bare ``<function=...>`` blocks). Multiple function blocks per
    response are supported (parallel calls).
    """

    name: str = "function_xml"
    strip_content: bool = True

    _func_pat: re.Pattern = field(init=False, repr=False, compare=False)
    _param_pat: re.Pattern = field(init=False, repr=False, compare=False)
    _wrapper_pat: re.Pattern = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        # <function=NAME>...</function> — capture name + body
        self._func_pat = re.compile(
            r"<function\s*=\s*([A-Za-z0-9_\-\.]+)\s*>"
            r"([\s\S]*?)"
            r"</function>",
            re.IGNORECASE,
        )
        # <parameter=KEY>VALUE</parameter>
        self._param_pat = re.compile(
            r"<parameter\s*=\s*([A-Za-z0-9_\-\.]+)\s*>"
            r"([\s\S]*?)"
            r"</parameter>",
            re.IGNORECASE,
        )
        # <tool_call>...</tool_call> wrapper (optional in source)
        self._wrapper_pat = re.compile(
            r"<tool_call>\s*([\s\S]*?)\s*</tool_call>",
            re.IGNORECASE,
        )

    def parse(self, raw_message: dict) -> NormalizedMessage:
        base = OpenAIPassthroughParser().parse(raw_message)
        # Provider already normalized → just clean stray XML markup
        # if any (defensive).
        if base.tool_calls:
            cleaned = self._strip(base.content)
            return NormalizedMessage(
                role=base.role, content=cleaned,
                tool_calls=list(base.tool_calls),
            )

        content = base.content or ""
        if "<function" not in content:
            return base

        tcs: list[dict] = []
        # Walk every <function=NAME>...</function> block — wrapper is
        # optional, so we don't require <tool_call>.
        for m in self._func_pat.finditer(content):
            fn_name = m.group(1).strip()
            body = m.group(2) or ""
            args: dict = {}
            for pm in self._param_pat.finditer(body):
                key = pm.group(1).strip()
                val = pm.group(2)
                # Trim trailing whitespace only — don't strip inside
                # since shell commands etc. may legitimately have
                # internal whitespace. Leading newline (from the
                # template `<parameter=K>\nVAL\n</parameter>` shape)
                # is harmless to strip.
                v_stripped = val.strip("\n\r ").rstrip()
                # Only attempt JSON parse when the value LOOKS like
                # JSON. Calling _robust_json_loads on arbitrary shell
                # text returns "" (json_repair is too aggressive),
                # which would erase the actual command. Restrict
                # JSON parsing to obvious JSON shapes:
                #   {...}/[...]/"..."  → object / array / string
                #   true|false|null    → keywords
                #   ^-?\d+(\.\d+)?$    → integer or float
                if (v_stripped[:1] in ("{", "[", '"')
                        or v_stripped in ("true", "false", "null")
                        or _JSON_NUMBER_RE.match(v_stripped)):
                    parsed = _robust_json_loads(v_stripped)
                    args[key] = parsed if parsed is not None else v_stripped
                else:
                    args[key] = v_stripped
            if not fn_name:
                continue
            tcs.append(_coerce_tool_call(name=fn_name, arguments=args))

        if not tcs:
            return base

        cleaned = self._strip(content) if self.strip_content else content
        return NormalizedMessage(
            role=base.role, content=cleaned, tool_calls=tcs,
        )

    def _strip(self, content: str) -> str:
        if not content:
            return content
        # Remove the whole <tool_call>…</tool_call> wrappers first.
        out = self._wrapper_pat.sub("", content)
        # Then remove any bare <function=…>…</function> that survived
        # (no wrapper case).
        out = self._func_pat.sub("", out)
        return out.strip()


# ── registry of parser classes by name ────────────────────────────────


BUILTIN_CLASSES: dict[str, type] = {
    "OpenAIPassthroughParser": OpenAIPassthroughParser,
    "XMLTagJSONParser":        XMLTagJSONParser,
    "JSONOnlyParser":          JSONOnlyParser,
    "GLMArgKVParser":          GLMArgKVParser,
    "DSMLParser":              DSMLParser,
    "FunctionXMLParser":       FunctionXMLParser,
}


__all__ = [
    "OpenAIPassthroughParser",
    "XMLTagJSONParser",
    "JSONOnlyParser",
    "GLMArgKVParser",
    "DSMLParser",
    "FunctionXMLParser",
    "BUILTIN_CLASSES",
]
