"""
prompt_schemas — Single source of truth for "what data is sent to the LLM".

Every block of info we put in front of the LLM (skills, tools, rules,
project state, memory, etc.) is defined here as a typed schema. Each
field carries a ``metadata={"llm": True/False}`` flag. Use
``to_llm_dict()`` / ``to_llm_markdown()`` helpers to extract / render
ONLY the LLM-visible payload — no chance of accidentally leaking
internal IDs, audit timestamps, server-side paths, or admin-only flags.

Why this exists
---------------
Today's prompt assembly is scattered:
  - ``agent.py:_build_granted_skills_roster`` builds the skill list
    inline with hand-rolled string concatenation.
  - ``middleware.py:_format_schema_signature`` builds a tool signature
    inline.
  - ``agent_llm.py`` does ``_try_add(...)`` for plan state, intent
    hints, playbook, knowledge wiki, scheduled context, git, memory,
    rules — all inline.
  - Each call site picks fields ad-hoc; "is this ID exposed to the
    LLM?" is answered by reading code, not by querying a schema.

The schemas here let us audit visibility at one place, share renderer
logic, and ship single-shot helpers (e.g. ``ToolSchema.signature()``)
to validation errors / system prompts / docs.

Conventions
-----------
  * ``LLMVisibleSchema`` is a mixin: subclass alongside ``@dataclass``.
  * Every field gets metadata: ``field(metadata={"llm": True/False})``.
  * The default if metadata absent is **False** (fail closed — don't
    silently leak unannotated fields).
  * ``to_llm_dict()`` returns a plain dict of LLM-visible fields.
  * ``to_llm_markdown()`` returns a human-readable markdown block,
    rendered from the same set.

Tool parameters need their description alongside (per-param doc), so
``ParamSpec`` is its own schema with description always sent.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields as dc_fields
from typing import Any, ClassVar


# ─────────────────────────────────────────────────────────────────────
# Base
# ─────────────────────────────────────────────────────────────────────


class LLMVisibleSchema:
    """Mixin for dataclasses whose contents reach an LLM.

    Use ``field(metadata={"llm": True})`` on each attribute that the
    LLM may see. Anything without explicit ``llm=True`` is treated as
    server-side / admin / audit only and stripped by ``to_llm_dict``.
    """

    @classmethod
    def llm_field_names(cls) -> list[str]:
        return [f.name for f in dc_fields(cls) if f.metadata.get("llm")]

    @classmethod
    def internal_field_names(cls) -> list[str]:
        return [f.name for f in dc_fields(cls) if not f.metadata.get("llm")]

    def to_llm_dict(self) -> dict:
        """Plain-dict projection of LLM-visible fields only.

        Recursively projects nested objects that expose a ``to_llm_dict``
        method (duck typed — works for both ``LLMVisibleSchema``
        subclasses AND standalone schemas like ``ParamSpec`` that
        provide their own projection without inheriting).
        """
        out: dict = {}
        for f in dc_fields(self):
            if not f.metadata.get("llm"):
                continue
            v = getattr(self, f.name)
            if hasattr(v, "to_llm_dict") and callable(v.to_llm_dict):
                out[f.name] = v.to_llm_dict()
            elif isinstance(v, list):
                out[f.name] = [
                    (item.to_llm_dict()
                     if hasattr(item, "to_llm_dict") and callable(item.to_llm_dict)
                     else item)
                    for item in v
                ]
            else:
                out[f.name] = v
        return out

    def to_llm_markdown(self) -> str:
        """Default markdown render — subclasses override for prettier
        output. Falls back to a key:value list of LLM-visible fields."""
        d = self.to_llm_dict()
        lines = []
        for k, v in d.items():
            if v in (None, "", [], {}):
                continue
            if isinstance(v, list):
                lines.append(f"- **{k}**: {', '.join(str(x) for x in v)}")
            else:
                lines.append(f"- **{k}**: {v}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# ParamSpec — one parameter of a tool, with description always sent
# ─────────────────────────────────────────────────────────────────────


@dataclass
class ParamSpec:
    """One parameter of a callable tool. ALL fields sent to LLM —
    parameters without descriptions are useless to the model, so we
    always include them and never strip.

    For parameters that use complex JSON-Schema constructs (``oneOf`` /
    ``anyOf`` / ``allOf`` / ``$ref``), the round-trip can't be fully
    expressed via simple ``type``+default+enum fields. ``raw_schema``
    holds the original property dict so ``to_openai_payload`` can
    re-emit it byte-for-byte; this preserves valid JSON Schema for
    strict providers (DeepSeek rejects ``{"type": "any"}`` even though
    OpenAI tolerates it).
    """

    name: str
    type: str              # "string" / "integer" / "number" / "boolean" / "object" / "array"
    required: bool
    description: str = ""
    default: Any = None
    enum_values: list = field(default_factory=list)
    example: str = ""
    # When original schema used oneOf/anyOf/allOf or other constructs
    # we can't losslessly round-trip via the flat fields above, this
    # holds the original property dict. to_openai_payload prefers it.
    raw_schema: dict = field(default_factory=dict)

    def to_llm_dict(self) -> dict:
        out: dict = {
            "name": self.name,
            "type": self.type,
            "required": self.required,
        }
        if self.description:
            out["description"] = self.description
        if not self.required and self.default is not None:
            out["default"] = self.default
        if self.enum_values:
            out["enum"] = list(self.enum_values)
        if self.example:
            out["example"] = self.example
        return out

    def to_signature_segment(self, with_desc: bool = True,
                              desc_cap: int = 60) -> str:
        """One-line segment for compact tool signature.

        Example::

            path: string [REQUIRED]  # absolute or workspace-relative
            offset: integer = 0  # starting line
        """
        if self.required:
            marker = " [REQUIRED]"
        elif self.default is not None:
            marker = f" = {self.default!r}"
        else:
            marker = " (optional)"
        seg = f"{self.name}: {self.type}{marker}"
        if with_desc and self.description:
            short = self.description.replace("\n", " ").strip()
            if len(short) > desc_cap:
                short = short[:desc_cap].rstrip() + "…"
            seg += f"  # {short}"
        return seg


# ─────────────────────────────────────────────────────────────────────
# ToolSchema — a callable tool the agent may invoke
# ─────────────────────────────────────────────────────────────────────


@dataclass
class ToolSchema(LLMVisibleSchema):
    name: str = field(default="", metadata={"llm": True})
    description: str = field(default="", metadata={"llm": True})
    # Optional structured-description sections (Claude Code-style).
    # When populated, to_openai_payload merges them into the
    # description string the LLM sees, in a stable order. Existing
    # tools can leave these blank — description is the fallback.
    # use_when     : 1-2 sentences on when to call this tool
    # not_for      : when NOT to use it (cross-tool guidance)
    # output_format: what the result looks like / how to read it
    # gotcha       : non-obvious pitfalls / 1-based vs 0-based / encoding
    use_when: str = field(default="", metadata={"llm": True})
    not_for: str = field(default="", metadata={"llm": True})
    output_format: str = field(default="", metadata={"llm": True})
    gotcha: str = field(default="", metadata={"llm": True})
    params: list = field(default_factory=list, metadata={"llm": True})  # list[ParamSpec]
    aliases: list = field(default_factory=list, metadata={"llm": False})
    handler_name: str = field(default="", metadata={"llm": False})
    risk_level: str = field(default="", metadata={"llm": False})  # safe|risky|dangerous
    category: str = field(default="", metadata={"llm": False})
    audit_tags: list = field(default_factory=list, metadata={"llm": False})

    def composite_description(self) -> str:
        """Merge the base description + the 4 structured sections in
        a stable order. Used by to_openai_payload to ship a single
        ``description`` string to the LLM that carries the richer
        guidance when authors filled it in.
        """
        parts: list[str] = []
        if self.description:
            parts.append(self.description.strip())
        if self.use_when:
            parts.append(f"Use when: {self.use_when.strip()}")
        if self.not_for:
            parts.append(f"Not for: {self.not_for.strip()}")
        if self.output_format:
            parts.append(f"Output: {self.output_format.strip()}")
        if self.gotcha:
            parts.append(f"GOTCHA: {self.gotcha.strip()}")
        return "\n".join(parts)

    @property
    def required_params(self) -> list[str]:
        return [p.name for p in self.params if isinstance(p, ParamSpec) and p.required]

    @property
    def optional_params(self) -> list[str]:
        return [p.name for p in self.params if isinstance(p, ParamSpec) and not p.required]

    def signature(self, multiline: bool = True, with_desc: bool = True) -> str:
        """Compact tool signature.

        ``multiline=True`` → one param per line (good for error messages):

            read_file(
              path: string [REQUIRED]  # absolute or workspace-relative file path
              offset: integer = 0  # 0-indexed starting line
              limit: integer = -1  # -1 = read entire file
              _reason: string [REQUIRED]  # 为什么这次需要调用…
            )

        ``multiline=False`` → one-line compact form.

        Same _reason injection as ``to_openai_payload`` — keeps the
        error-signature renderer and the tools[] payload in lockstep
        (test_tool_payload_and_error_signature_single_source).
        """
        segs = [
            p.to_signature_segment(with_desc=with_desc)
            for p in self.params if isinstance(p, ParamSpec)
        ]
        # Mirror the _reason injection from to_openai_payload so the
        # error message the LLM sees on a validation failure carries
        # the same required-set as the schema it was given. Reads the
        # same setting so disabling one disables the other.
        try:
            from ..system_settings import get_store
            _ss = get_store()
            _enabled = bool(_ss.get("tool_reason.enabled", True)) if _ss else True
        except Exception:
            _enabled = True
        if _enabled and not any(
                isinstance(p, ParamSpec) and p.name == self.REASON_PARAM_NAME
                for p in self.params):
            reason_seg = (
                f"{self.REASON_PARAM_NAME}: string [REQUIRED]  "
                f"# Why this specific call is needed (<={self.REASON_MAX_CHARS} chars). "
                f"No filler like 'continue'/'check'."
                if with_desc else
                f"{self.REASON_PARAM_NAME}: string [REQUIRED]"
            )
            segs.append(reason_seg)
        if not segs:
            return f"{self.name}()"
        if multiline:
            # Drop the comma separator in multi-line — descriptions
            # often end at the line boundary and a trailing "," after
            # "# 0-indexed starting line" reads as part of the comment.
            return f"{self.name}(\n  " + "\n  ".join(segs) + "\n)"
        return f"{self.name}(" + ", ".join(segs) + ")"

    def to_llm_markdown(self) -> str:
        lines = [f"### `{self.name}`"]
        # Prefer the composite description (base + 4 structured
        # sections) over the bare description so the markdown view
        # stays in sync with what the LLM sees in tools[] payload.
        composite = self.composite_description()
        if composite:
            lines.append(composite.strip())
        elif self.description:
            lines.append(self.description.strip())
        if self.params:
            lines.append("")
            lines.append("**参数:**")
            for p in self.params:
                if not isinstance(p, ParamSpec):
                    continue
                marker = "**[REQUIRED]**" if p.required else "(optional)"
                line = f"- `{p.name}` (*{p.type}*) {marker}"
                if p.description:
                    line += f" — {p.description}"
                if not p.required and p.default is not None:
                    line += f" 默认: `{p.default!r}`"
                if p.enum_values:
                    line += f" 枚举: `{p.enum_values}`"
                lines.append(line)
        return "\n".join(lines)

    # Universal "_reason" param injected into every tool's schema.
    # Forces the LLM to articulate WHY before calling — strong
    # self-check against the "read same file 5 times" loop and similar
    # wandering. Stripped server-side before reaching the underlying
    # tool function (prefix-underscore convention; see agent dispatch).
    REASON_PARAM_NAME: ClassVar[str] = "_reason"
    REASON_MAX_CHARS: ClassVar[int] = 100
    REASON_DESCRIPTION: ClassVar[str] = (
        "Why this specific call is needed right now (<=100 chars). "
        "State the concrete unknown or sub-task this resolves; do NOT "
        "write filler like 'continue' or 'check'. If you just made the "
        "same call, give a substantively different reason or switch tools."
    )

    def to_openai_payload(self) -> dict:
        """Render this ToolSchema as an OpenAI tools[] entry — the
        ``{"type": "function", "function": {...}}`` shape consumed by
        chat.completions endpoints.

        Single-source-of-truth: when ``app.tools.TOOL_DEFINITIONS`` is
        eventually replaced by a registry of ToolSchemas, the payload
        sent to the LLM and the signature shown in error messages will
        come from the SAME object (no chance for tools[] schema and
        validation-error schema to drift apart).

        Universal _reason param: every tool gets a required ``_reason``
        string (≤100 chars) so the LLM must articulate WHY before
        calling. Stripped server-side before dispatch — never reaches
        the underlying tool function. Underscore prefix is the
        long-standing convention for server-injected params.
        """
        properties: dict = {}
        required: list = []
        for p in self.params:
            if not isinstance(p, ParamSpec):
                continue
            # If the original schema had complex constructs (oneOf /
            # anyOf / allOf / $ref / not / no top-level type), use the
            # raw schema dict directly — strict providers like
            # DeepSeek reject {"type":"any"} but accept oneOf with
            # explicit member types.
            if p.raw_schema:
                properties[p.name] = dict(p.raw_schema)
                if p.required:
                    required.append(p.name)
                continue
            prop: dict = {"type": p.type or "string"}
            if p.description:
                prop["description"] = p.description
            if not p.required and p.default is not None:
                prop["default"] = p.default
            if p.enum_values:
                prop["enum"] = list(p.enum_values)
            properties[p.name] = prop
            if p.required:
                required.append(p.name)
        # Inject universal _reason param (always last in property
        # order; required). Skip if:
        #   - source tool already declared it explicitly (let the
        #     tool's own description win), OR
        #   - admin disabled the feature in System Settings
        #     (system_settings.tool_reason.enabled = false).
        # Setting lookup is best-effort — if the store isn't ready
        # (very early boot, tests), fall through to the default-on path.
        _reason_enabled = True
        _reason_max_chars = self.REASON_MAX_CHARS
        try:
            from ..system_settings import get_store
            _ss = get_store()
            if _ss is not None:
                _reason_enabled = bool(
                    _ss.get("tool_reason.enabled", True))
                _reason_max_chars = int(
                    _ss.get("tool_reason.max_chars", self.REASON_MAX_CHARS))
        except Exception:
            pass
        if _reason_enabled and self.REASON_PARAM_NAME not in properties:
            properties[self.REASON_PARAM_NAME] = {
                "type": "string",
                "description": self.REASON_DESCRIPTION,
                "maxLength": _reason_max_chars,
            }
            required.append(self.REASON_PARAM_NAME)
        fn: dict = {"name": self.name}
        # Prefer the composite description (description + use_when /
        # not_for / output / gotcha sections) when any structured field
        # is filled. Falls back to the plain description when those are
        # empty so tools that haven't been migrated still ship cleanly.
        desc = self.composite_description() or self.description
        if desc:
            fn["description"] = desc
        params_dict: dict = {"type": "object", "properties": properties}
        if required:
            params_dict["required"] = required
        fn["parameters"] = params_dict
        return {"type": "function", "function": fn}


# ─────────────────────────────────────────────────────────────────────
# SkillSchema — a granted skill package the agent may consult / invoke
# ─────────────────────────────────────────────────────────────────────


@dataclass
class SkillSchema(LLMVisibleSchema):
    """A granted skill package the agent may consult / invoke.

    Field convention:
      - ``description`` / ``summary_zh`` should answer **"WHEN do I
        call this skill?"** — i.e. the trigger scenarios in plain
        sentences. Not "what this skill is" in the abstract; the LLM
        decides whether to use a skill based on situational fit, so
        the description must speak to that. Good example:
            "Use this when working on any non-trivial dev task that
             needs design + implementation + review."
        Bad example:
            "Engineering workflow pack with 12 sub-skills."
      - ``scenarios`` is a tag list (短语 / 关键词), complementing the
        description sentence. e.g. ["新功能开发", "bug 修复"]
      - ``applicable_roles`` is a hard filter (which roles even see
        this skill). The skill description / scenarios still drive
        the LLM's choice within those roles.
    """

    # 100-char cap on description (matches Skill Store edit form +
    # backend validation in SkillStore.update_entry_metadata). Long
    # blurbs hurt prompt economy and the LLM uses description to decide
    # WHEN to call the skill, not WHAT it is in detail.
    # ClassVar so it stays a class constant, not a dataclass field.
    DESCRIPTION_MAX_CHARS: ClassVar[int] = 100

    name: str = field(default="", metadata={"llm": True})
    id: str = field(default="", metadata={"llm": True})
    path: str = field(default="", metadata={"llm": True})
    # WHEN to call (English) — see class docstring for convention
    description: str = field(default="", metadata={"llm": True})
    # WHEN to call (zh-CN) — preferred when agent operates in zh
    summary_zh: str = field(default="", metadata={"llm": True})
    applicable_roles: list = field(default_factory=list, metadata={"llm": True})
    scenarios: list = field(default_factory=list, metadata={"llm": True})
    rules: list = field(default_factory=list, metadata={"llm": True})
    # Server-side / audit only — NOT sent to LLM:
    version: str = field(default="", metadata={"llm": False})
    install_dir: str = field(default="", metadata={"llm": False})
    granted_at: float = field(default=0.0, metadata={"llm": False})
    granted_by: str = field(default="", metadata={"llm": False})
    risk_level: str = field(default="", metadata={"llm": False})
    manifest_raw: dict = field(default_factory=dict, metadata={"llm": False})

    def to_llm_markdown(self) -> str:
        # Header — name + id
        lines = [f"### `{self.name}`" + (f" (id: `{self.id}`)" if self.id else "")]
        # WHEN to call — lead with "何时调用:" so the LLM treats the
        # description as a trigger, not a passive blurb. Defensive
        # truncate at DESCRIPTION_MAX_CHARS — backend validates on save
        # but legacy / external imports may exceed.
        when = (self.summary_zh or self.description).strip()
        if when:
            if len(when) > self.DESCRIPTION_MAX_CHARS:
                when = when[: self.DESCRIPTION_MAX_CHARS - 1].rstrip() + "…"
            lines.append(f"**何时调用:** {when}")
        # Tag list of scenarios (complements the sentence above)
        if self.scenarios:
            lines.append(f"**典型场景:** {', '.join(self.scenarios)}")
        if self.applicable_roles:
            lines.append(f"**适用角色:** {', '.join(self.applicable_roles)}")
        # Path so agent can read_file SKILL.md for full usage
        if self.path:
            lines.append(f"📂 `{self.path}` — `read_file <path>/SKILL.md` 查完整用法")
        # Rules (skill-specific must_do / forbid)
        if self.rules:
            lines.append("**规则:**")
            for r in self.rules:
                lines.append(f"- {r}")
        return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────
# RuleSchema — a Rule Engine rule applicable to current scope
# ─────────────────────────────────────────────────────────────────────


@dataclass
class RuleSchema(LLMVisibleSchema):
    id: str = field(default="", metadata={"llm": True})
    name: str = field(default="", metadata={"llm": True})
    description: str = field(default="", metadata={"llm": True})
    trigger: str = field(default="", metadata={"llm": True})  # e.g. "before_tool_call"
    action: str = field(default="", metadata={"llm": True})   # warn|deny|approve
    message: str = field(default="", metadata={"llm": True})  # what LLM sees on fire
    condition_summary: str = field(default="", metadata={"llm": True})
    # Internal:
    full_condition: dict = field(default_factory=dict, metadata={"llm": False})
    priority: int = field(default=0, metadata={"llm": False})
    source: str = field(default="", metadata={"llm": False})
    enabled: bool = field(default=True, metadata={"llm": False})
    created_by: str = field(default="", metadata={"llm": False})

    def to_llm_markdown(self) -> str:
        head = f"- **{self.name}** (`{self.action}`"
        if self.trigger:
            head += f" @ `{self.trigger}`"
        head += ")"
        if self.description:
            head += f" — {self.description}"
        elif self.message:
            head += f" — {self.message}"
        if self.condition_summary:
            head += f"\n  条件: {self.condition_summary}"
        return head


# ─────────────────────────────────────────────────────────────────────
# Adapters — convert from existing internal types to schemas
# ─────────────────────────────────────────────────────────────────────


def from_tool_definition(td: dict) -> ToolSchema:
    """Build ToolSchema from a TOOL_DEFINITIONS entry.

    Accepts either of:
      - the outer OpenAI tools[] entry: ``{"type": "function",
        "function": {"name": ..., "parameters": ...}}``
      - the inner function dict directly: ``{"name": ..., "parameters": ...}``
        — what ``middleware._find_tool_schema`` returns.

    Drops underscore-prefixed params (those are server-side injected,
    not LLM-visible).
    """
    if not isinstance(td, dict):
        return ToolSchema()
    # Unwrap if outer shape; else treat as the function dict
    fn = td.get("function") if isinstance(td.get("function"), dict) else td
    if not isinstance(fn, dict):
        return ToolSchema()
    params_def = fn.get("parameters") or {}
    props = params_def.get("properties") or {}
    required = set(params_def.get("required") or [])
    params: list = []
    if isinstance(props, dict):
        for pname, pdef in props.items():
            if pname.startswith("_"):
                continue
            if not isinstance(pdef, dict):
                continue
            # Detect complex JSON-Schema constructs that flat fields
            # can't round-trip — preserve the raw property dict so
            # to_openai_payload can re-emit it verbatim.
            _is_complex = any(
                k in pdef for k in ("oneOf", "anyOf", "allOf", "$ref", "not")
            )
            ptype = pdef.get("type", "")
            if not ptype:
                # No top-level type → must be a complex schema (or
                # legacy "any"). Mark as complex so we keep raw.
                ptype = "any" if not _is_complex else "object"
                _is_complex = True
            params.append(ParamSpec(
                name=pname,
                type=ptype,
                required=pname in required,
                description=str(pdef.get("description") or "").strip(),
                default=pdef.get("default"),
                enum_values=list(pdef.get("enum") or []),
                example=str(pdef.get("example") or ""),
                raw_schema=dict(pdef) if _is_complex else {},
            ))
    # Best-effort: split a Claude-Code-style description into the
    # 4 structured sections (use_when / not_for / output / gotcha)
    # by recognising the common section markers. Falls through on
    # anything it doesn't match — base description still ships.
    raw_desc = str(fn.get("description") or "").strip()
    base_desc, sections = _split_tool_description_sections(raw_desc)
    return ToolSchema(
        name=fn.get("name", ""),
        description=base_desc,
        use_when=sections.get("use_when", ""),
        not_for=sections.get("not_for", ""),
        output_format=sections.get("output", ""),
        gotcha=sections.get("gotcha", ""),
        params=params,
    )


# Section markers recognised in a tool description string. Order
# matters — earlier entries are searched first so a Use-when paragraph
# doesn't get swallowed by a later Not-for. Case-insensitive match on
# the leading word(s) only.
_TOOL_DESC_SECTION_MARKERS: tuple = (
    ("use_when", ("Use when:", "When to use:")),
    ("not_for",  ("Not for:", "Do NOT use:", "Avoid when:")),
    ("output",   ("Output:", "Output format:", "Returns:")),
    ("gotcha",   ("GOTCHA:", "Gotcha:", "Pitfall:", "Caveat:")),
)


def _split_tool_description_sections(desc: str) -> tuple[str, dict]:
    """Parse a tool description into (base_text, {section: content}).

    Recognises the Claude-Code-style markers (``Use when:`` /
    ``Not for:`` / ``Output:`` / ``GOTCHA:``). Each section runs from
    its marker line until the next recognised marker or end-of-string.
    Anything before the first marker is the ``base_text`` (the
    summary sentence(s)).
    """
    if not desc:
        return "", {}
    # Find all marker hits with their positions. Dedupe by (start, key)
    # — case-insensitive matching can produce two hits at the same
    # position (e.g. "GOTCHA:" matches both the "GOTCHA:" and "Gotcha:"
    # variants), and the second one would compute an empty body that
    # overwrites the first. Keep the first marker variant per (pos, key).
    import re
    seen_pos_key: set = set()
    hits: list = []
    for key, markers in _TOOL_DESC_SECTION_MARKERS:
        for m in markers:
            pat = re.compile(r"(?m)^\s*" + re.escape(m), re.IGNORECASE)
            for match in pat.finditer(desc):
                tag = (match.start(), key)
                if tag in seen_pos_key:
                    continue
                seen_pos_key.add(tag)
                hits.append((match.start(), key, m, match.end()))
    if not hits:
        return desc, {}
    hits.sort(key=lambda t: t[0])
    base_text = desc[: hits[0][0]].rstrip()
    sections: dict = {}
    for i, (start, key, marker, content_start) in enumerate(hits):
        end = hits[i + 1][0] if i + 1 < len(hits) else len(desc)
        body = desc[content_start:end].strip()
        # If a key shows up twice (rare — e.g. someone writes both
        # "Output:" and "Returns:"), keep the first occurrence.
        if key not in sections:
            sections[key] = body
    return base_text, sections


def from_skill_install(install: Any) -> SkillSchema:
    """Build SkillSchema from a SkillRegistry SkillInstall object.

    Defensive: any missing attribute / failed sub-call falls back to
    sensible empty defaults. Never raises.
    """
    if install is None:
        return SkillSchema()
    m = getattr(install, "manifest", None)
    if m is None:
        return SkillSchema(id=getattr(install, "id", ""))

    desc_en = ""
    desc_zh = ""
    try:
        if hasattr(m, "get_description"):
            desc_zh = m.get_description("zh-CN") or ""
            desc_en = m.get_description("en") or ""
    except Exception:
        pass
    if not desc_en:
        desc_en = getattr(m, "description", "") or ""

    return SkillSchema(
        name=getattr(m, "name", "") or "",
        id=getattr(install, "id", "") or "",
        path=str(getattr(install, "install_dir", "") or ""),
        description=desc_en,
        summary_zh=desc_zh,
        applicable_roles=list(getattr(m, "applicable_roles", []) or []),
        scenarios=list(getattr(m, "scenarios", []) or []),
        rules=list(getattr(m, "rules", []) or []),
        version=str(getattr(m, "version", "") or ""),
        install_dir=str(getattr(install, "install_dir", "") or ""),
        granted_at=float(getattr(install, "granted_at", 0) or 0),
        granted_by=str(getattr(install, "granted_by", "") or ""),
        risk_level=str(getattr(m, "risk_level", "") or ""),
    )


def from_rule_engine_rule(rule: Any) -> RuleSchema:
    """Build RuleSchema from a Rule Engine Rule object."""
    if rule is None:
        return RuleSchema()
    actions = list(getattr(rule, "actions", []) or [])
    primary_action = ""
    primary_message = ""
    if actions and isinstance(actions[0], dict):
        primary_action = str(actions[0].get("type") or "")
        primary_message = str(actions[0].get("message") or "")
    return RuleSchema(
        id=str(getattr(rule, "id", "") or ""),
        name=str(getattr(rule, "name", "") or ""),
        description=str(getattr(rule, "description", "") or ""),
        trigger=str(getattr(rule, "trigger", "") or ""),
        action=primary_action,
        message=primary_message,
        full_condition=dict(getattr(rule, "condition", {}) or {}),
        priority=int(getattr(rule, "priority", 0) or 0),
        source=str(getattr(rule, "source", "") or ""),
        enabled=bool(getattr(rule, "enabled", True)),
        created_by=str(getattr(rule, "created_by", "") or ""),
    )


# ─────────────────────────────────────────────────────────────────────
# Renderers — block-level markdown for prompt assembly
# ─────────────────────────────────────────────────────────────────────


def render_tools_block(tools: list[ToolSchema], heading: str = "## Tools") -> str:
    """Markdown block listing every tool — used as a quick-reference
    insert when the OpenAI ``tools[]`` payload alone isn't enough
    (some models drop schema awareness mid-turn under stress).

    Returns "" if no tools.
    """
    if not tools:
        return ""
    parts = [heading]
    for t in tools:
        if not isinstance(t, ToolSchema):
            continue
        parts.append("")
        parts.append(t.to_llm_markdown())
    return "\n".join(parts)


def render_skills_block(skills: list[SkillSchema],
                         heading: str = "## 你已装配的技能 (Installed Skills)") -> str:
    """Markdown block listing every granted skill, LLM-visible fields
    only. Returns "" if no skills."""
    if not skills:
        return ""
    parts = [heading, "", (
        "下表是你可用的 skill。**调用前先 `read_file <path>/SKILL.md` "
        "看完整用法**(每个 skill 自带的 must_do / QA gate / 样例都在那)。"
    )]
    for s in skills:
        if not isinstance(s, SkillSchema):
            continue
        parts.append("")
        parts.append(s.to_llm_markdown())
    return "\n".join(parts)


def render_rules_block(rules: list[RuleSchema],
                        heading: str = "## 当前生效的规则 (Rule Engine)") -> str:
    """Markdown block listing applicable Rule Engine rules so the LLM
    knows IN ADVANCE what will be enforced (saves the cycle of: try →
    deny → retry).
    """
    if not rules:
        return ""
    parts = [heading]
    for r in rules:
        if not isinstance(r, RuleSchema):
            continue
        if not r.enabled:
            continue
        parts.append(r.to_llm_markdown())
    return "\n".join(parts) if len(parts) > 1 else ""


# ─────────────────────────────────────────────────────────────────────
# Convenience — single-tool signature for tool-validation errors
# ─────────────────────────────────────────────────────────────────────


def render_tool_signature(tool: ToolSchema, multiline: bool = True) -> str:
    """Compact tool signature with per-param descriptions — used by
    middleware.py when a tool call fails validation, so the LLM sees
    the FULL parameter contract on its retry, not just "missing 'path'".
    """
    if not isinstance(tool, ToolSchema):
        return ""
    return tool.signature(multiline=multiline, with_desc=True)


# ═════════════════════════════════════════════════════════════════════
#  PHASE-2 SCHEMAS — system_prompt blocks
#
# Each block of dynamic context that gets _try_add()'d into the agent's
# system_prompt is now a typed schema. Field-level llm visibility lets
# us audit "what reaches the LLM" at one place; ``markdown_fallback``
# lets adapters wrap legacy string-returning formatters during the
# migration period (renderer prefers structured fields when populated).
# ═════════════════════════════════════════════════════════════════════


# ─── Plan state ──────────────────────────────────────────────────────


@dataclass
class PlanStepSchema:
    """One step in an ExecutionPlan, projected for LLM visibility."""

    order: int = 0
    title: str = ""
    status: str = ""              # pending|in_progress|completed|skipped|failed
    acceptance: str = ""
    result_summary: str = ""
    blocked_by: list = field(default_factory=list)  # list[int] step orders

    def to_llm_dict(self) -> dict:
        out: dict = {"order": self.order, "title": self.title,
                     "status": self.status}
        if self.acceptance:
            out["acceptance"] = self.acceptance
        if self.result_summary:
            out["result_summary"] = self.result_summary
        if self.blocked_by:
            out["blocked_by"] = list(self.blocked_by)
        return out


@dataclass
class PlanStateSchema(LLMVisibleSchema):
    """Current ExecutionPlan snapshot. Mirrors Agent.format_plan_state_for_llm
    output but in structured form so the LLM-visible projection is
    auditable.
    """

    task_summary: str = field(default="", metadata={"llm": True})
    current_steps: list = field(default_factory=list, metadata={"llm": True})
    done_steps: list = field(default_factory=list, metadata={"llm": True})
    pending_steps: list = field(default_factory=list, metadata={"llm": True})
    failed_steps: list = field(default_factory=list, metadata={"llm": True})
    rules_text: str = field(default="", metadata={"llm": True})
    markdown_fallback: str = field(default="", metadata={"llm": True})
    # Internal:
    plan_id: str = field(default="", metadata={"llm": False})
    plan_status: str = field(default="", metadata={"llm": False})

    def is_empty(self) -> bool:
        return not (self.task_summary or self.current_steps or self.done_steps
                    or self.pending_steps or self.failed_steps
                    or self.markdown_fallback)

    def to_llm_markdown(self) -> str:
        # Prefer structured render; fall back to legacy markdown
        if self.markdown_fallback and not (self.current_steps or self.done_steps):
            return self.markdown_fallback
        if self.is_empty():
            return ""
        lines = ["<plan_state>"]
        if self.task_summary:
            lines.append(f"task: {self.task_summary}")
        if self.current_steps:
            lines.append("current:")
            for s in self.current_steps:
                if isinstance(s, PlanStepSchema):
                    lines.append(f"  [{s.order}] {s.title}  ({s.status})")
                    if s.acceptance:
                        lines.append(f"    acceptance: {s.acceptance}")
        if self.done_steps:
            lines.append("done:")
            for s in self.done_steps[-5:]:
                if isinstance(s, PlanStepSchema):
                    line = f"  [{s.order}] {s.title}"
                    if s.result_summary:
                        line += f' — "{s.result_summary}"'
                    lines.append(line)
            if len(self.done_steps) > 5:
                lines.append(f"  (+{len(self.done_steps) - 5} earlier)")
        if self.pending_steps:
            lines.append("pending:")
            for s in self.pending_steps[:5]:
                if isinstance(s, PlanStepSchema):
                    line = f"  [{s.order}] {s.title}"
                    if s.blocked_by:
                        line += f"  blocked_by={s.blocked_by}"
                    lines.append(line)
        if self.failed_steps:
            lines.append("failed:")
            for s in self.failed_steps:
                if isinstance(s, PlanStepSchema):
                    lines.append(f"  [{s.order}] {s.title}")
        if self.rules_text:
            lines.append("rules:")
            for r in self.rules_text.splitlines():
                if r.strip():
                    lines.append(f"  - {r.strip().lstrip('- ')}")
        lines.append("</plan_state>")
        return "\n".join(lines)


# ─── Intent hint ─────────────────────────────────────────────────────


@dataclass
class IntentHintSchema(LLMVisibleSchema):
    """Result of IntentResolver classification — surfaced upfront so
    the LLM knows what category of help the user is asking for.
    """

    category: str = field(default="", metadata={"llm": True})
    hint_text: str = field(default="", metadata={"llm": True})
    extracted_slots: dict = field(default_factory=dict, metadata={"llm": True})
    confidence: float = field(default=0.0, metadata={"llm": False})

    def is_empty(self) -> bool:
        return not (self.category or self.hint_text)

    def to_llm_markdown(self) -> str:
        if self.is_empty():
            return ""
        body = self.hint_text or self.category
        if self.extracted_slots:
            slot_info = "; ".join(f"{k}={v}" for k, v in self.extracted_slots.items())
            body += f"\n提取参数: {slot_info}"
        return f"<intent_hint>\n{body}\n</intent_hint>"


# ─── Playbook (role_preset_v2 wrapper) ──────────────────────────────


@dataclass
class PlaybookSchema(LLMVisibleSchema):
    """Wraps a role_preset_v2.Playbook + active scopes, projected for
    LLM. Heavy lifting lives in playbook_runtime.build_playbook_context;
    this schema just binds its output + provides the scope tag for
    audit.
    """

    role_id: str = field(default="", metadata={"llm": True})
    active_scopes: list = field(default_factory=list, metadata={"llm": True})
    markdown_fallback: str = field(default="", metadata={"llm": True})
    # Internal:
    preset_version: int = field(default=0, metadata={"llm": False})

    def is_empty(self) -> bool:
        return not self.markdown_fallback

    def to_llm_markdown(self) -> str:
        if not self.markdown_fallback:
            return ""
        scopes_attr = ",".join(self.active_scopes) if self.active_scopes else "default"
        return f'<playbook scope="{scopes_attr}">\n{self.markdown_fallback}\n</playbook>'


# ─── Memory recall ──────────────────────────────────────────────────


@dataclass
class FactSchema:
    """One memory fact — always sent fully to LLM (truncated text)."""

    text: str = ""
    source: str = ""        # L2 | L3 | wiki | ...
    category: str = ""
    similarity: float = 0.0
    timestamp: float = 0.0
    scope: str = ""         # global | agent:X | project:Y | ...

    def to_llm_dict(self) -> dict:
        out: dict = {"text": self.text}
        if self.source:
            out["source"] = self.source
        if self.category:
            out["category"] = self.category
        if self.similarity:
            out["similarity"] = round(self.similarity, 2)
        return out


@dataclass
class MemoryRecallSchema(LLMVisibleSchema):
    """Result of memory_manager.retrieve_for_prompt.

    The retrieve_for_prompt() output is currently a markdown blob; we
    keep it under markdown_fallback while the memory module gradually
    yields structured facts. ``facts`` field reserved for future
    structured population.
    """

    query: str = field(default="", metadata={"llm": False})  # for audit
    facts: list = field(default_factory=list, metadata={"llm": True})
    markdown_fallback: str = field(default="", metadata={"llm": True})
    # Internal:
    total_chars_used: int = field(default=0, metadata={"llm": False})
    budget_chars: int = field(default=0, metadata={"llm": False})

    def to_llm_markdown(self) -> str:
        if self.facts:
            lines = ["## Recent Memory Recall"]
            for f in self.facts:
                if isinstance(f, FactSchema):
                    lines.append(f"- {f.text}" + (f"  ({f.source})" if f.source else ""))
            return "\n".join(lines)
        return self.markdown_fallback


# ─── Git context ────────────────────────────────────────────────────


@dataclass
class GitContextSchema(LLMVisibleSchema):
    """Project's git state at agent's working dir. Lightweight."""

    branch: str = field(default="", metadata={"llm": True})
    status_short: str = field(default="", metadata={"llm": True})
    recent_commits: list = field(default_factory=list, metadata={"llm": True})  # list[str]
    diff_stat: str = field(default="", metadata={"llm": True})
    markdown_fallback: str = field(default="", metadata={"llm": True})

    def is_empty(self) -> bool:
        return not (self.branch or self.markdown_fallback)

    def to_llm_markdown(self) -> str:
        if self.markdown_fallback:
            return self.markdown_fallback
        if self.is_empty():
            return ""
        parts = ["<git_context>"]
        if self.branch:
            parts.append(f"branch: {self.branch}")
        if self.status_short:
            parts.append(f"[git status]\n{self.status_short}")
        if self.recent_commits:
            parts.append("[git log]\n" + "\n".join(self.recent_commits[:5]))
        if self.diff_stat:
            parts.append(f"[git diff --stat]\n{self.diff_stat}")
        parts.append("</git_context>")
        return "\n".join(parts)


# ─── Knowledge wiki ─────────────────────────────────────────────────


@dataclass
class KnowledgeWikiSchema(LLMVisibleSchema):
    """Lightweight wiki summary (page titles + ids) — full content
    fetched on-demand via ``wiki_lookup`` tool.
    """

    pages: list = field(default_factory=list, metadata={"llm": True})  # list[{"title", "id"}]
    total_count: int = field(default=0, metadata={"llm": True})
    markdown_fallback: str = field(default="", metadata={"llm": True})

    def to_llm_markdown(self) -> str:
        return self.markdown_fallback  # let knowledge.py keep ownership of format


# ─── Workspace files (project shared) ──────────────────────────────


@dataclass
class WorkspaceFilesSchema(LLMVisibleSchema):
    """Project shared workspace summary. Reuses the Tier-2 PEP context
    fields (has_design_doc / has_plan_md / workspace_files) so the LLM
    sees the SAME view that Rule Engine evaluates against.
    """

    workspace_root: str = field(default="", metadata={"llm": True})
    has_design_doc: bool = field(default=False, metadata={"llm": True})
    has_plan_md: bool = field(default=False, metadata={"llm": True})
    top_level_entries: list = field(default_factory=list, metadata={"llm": True})

    def is_empty(self) -> bool:
        return not (self.workspace_root or self.top_level_entries)

    def to_llm_markdown(self) -> str:
        if self.is_empty():
            return ""
        lines = ["## Workspace State"]
        if self.workspace_root:
            lines.append(f"📂 root: `{self.workspace_root}`")
        flags = []
        flags.append(("✓" if self.has_design_doc else "✗") + " has_design_doc")
        flags.append(("✓" if self.has_plan_md else "✗") + " has_plan_md")
        lines.append(" · ".join(flags))
        if self.top_level_entries:
            lines.append("top-level:")
            lines.append("  " + "  ".join(self.top_level_entries[:30]))
            if len(self.top_level_entries) > 30:
                lines.append(f"  (+{len(self.top_level_entries) - 30} more)")
        return "\n".join(lines)


# ─── Scheduled context ─────────────────────────────────────────────


@dataclass
class ScheduledContextSchema(LLMVisibleSchema):
    """Scheduled tasks / cron entries the agent has."""

    scheduled_count: int = field(default=0, metadata={"llm": True})
    markdown_fallback: str = field(default="", metadata={"llm": True})

    def to_llm_markdown(self) -> str:
        return self.markdown_fallback


# ─── Admin instruction block ───────────────────────────────────────


@dataclass
class EnvSchema(LLMVisibleSchema):
    """Compact ``<env>`` block consolidating cwd / project context /
    git status / date / workspace flags into one stable region of
    the system prompt.

    Borrowed from Claude Code's pattern: rather than scatter cwd in
    one place, git_branch in another, has_design_doc in a third,
    fold them all into a single small block at a fixed position.
    Two wins:
      1. **Token economy** — fewer redundant labels ("workspace
         root: ...", "git: branch=...", "Date: ..." each carry
         their own header chars).
      2. **Cache friendliness** — the values inside ``<env>`` change
         slowly within a session (cwd / project rarely flip mid-turn);
         keeping them in one stable block makes the prefix cache
         hit more reliably than scattering them across schemas that
         each render with their own preamble.

    Coexists with WorkspaceFilesSchema and GitContextSchema during
    the migration — _build_dynamic_context can choose to render
    EnvSchema instead, or render both for now while we tune the
    rollout.
    """

    cwd: str = field(default="", metadata={"llm": True})
    project_id: str = field(default="", metadata={"llm": True})
    project_name: str = field(default="", metadata={"llm": True})
    git_branch: str = field(default="", metadata={"llm": True})
    git_status_summary: str = field(default="", metadata={"llm": True})
    date_iso: str = field(default="", metadata={"llm": True})
    has_design_doc: bool = field(default=False, metadata={"llm": True})
    has_plan_md: bool = field(default=False, metadata={"llm": True})
    workspace_root: str = field(default="", metadata={"llm": True})
    # Top-level entries — capped list (workspace overview at a glance)
    top_level_entries: list = field(
        default_factory=list, metadata={"llm": True})
    top_level_cap: int = field(default=20, metadata={"llm": False})

    def is_empty(self) -> bool:
        return not (
            self.cwd or self.project_id or self.project_name
            or self.git_branch or self.workspace_root
            or self.top_level_entries or self.date_iso
        )

    def to_llm_markdown(self) -> str:
        if self.is_empty():
            return ""
        lines = ["<env>"]
        # cwd / workspace_root: emit whichever is set, prefer cwd if
        # they differ (cwd is the active working dir, workspace_root
        # is the project shared dir — both useful when they diverge).
        if self.cwd:
            lines.append(f"cwd: {self.cwd}")
        if self.workspace_root and self.workspace_root != self.cwd:
            lines.append(f"workspace_root: {self.workspace_root}")
        if self.project_id or self.project_name:
            label = self.project_name or self.project_id
            extra = f" (id={self.project_id})" if self.project_id and self.project_name else ""
            lines.append(f"project: {label}{extra}")
        if self.git_branch:
            git_line = f"git_branch: {self.git_branch}"
            if self.git_status_summary:
                git_line += f"  (status: {self.git_status_summary})"
            lines.append(git_line)
        if self.date_iso:
            lines.append(f"date: {self.date_iso}")
        # Workspace flags only emit if there's a project context — for
        # a solo agent these flags carry no signal.
        if self.project_id or self.workspace_root:
            flag_parts = []
            flag_parts.append(
                ("✓" if self.has_design_doc else "✗") + " has_design_doc")
            flag_parts.append(
                ("✓" if self.has_plan_md else "✗") + " has_plan_md")
            lines.append("flags: " + " · ".join(flag_parts))
        if self.top_level_entries:
            entries = list(self.top_level_entries)[: self.top_level_cap]
            extra = ""
            if len(self.top_level_entries) > self.top_level_cap:
                extra = f"  (+{len(self.top_level_entries) - self.top_level_cap} more)"
            lines.append("top_level: " + "  ".join(entries) + extra)
        lines.append("</env>")
        return "\n".join(lines)


@dataclass
class ExecutionDisciplineSchema(LLMVisibleSchema):
    """Fixed rules that cap exploration / wandering. Always-on; the
    rules are constant text so the LLM gets the same prefix every
    turn (cache-friendly).

    Why this is its own schema instead of a portal-managed prompt
    card: rules here are HARD INVARIANTS the framework needs to
    survive (no read-loops, no mid-step exploration, no work without
    actionable task input). User can still add discretionary rules
    via the System Prompts UI; this block is the floor.
    """

    enabled: bool = field(default=True, metadata={"llm": False})

    def is_empty(self) -> bool:
        return not self.enabled

    def to_llm_markdown(self) -> str:
        if not self.enabled:
            return ""
        return (
            "<execution_discipline>\n"
            "1. **Don't re-read files you've already read this turn.** "
            "If write_file/edit_file succeeded (no error in tool_result), "
            "trust it — don't read_file to verify. If it failed, the bug is "
            "your tool args, not the file; fix args, don't re-read.\n"
            "2. **Each tool call's `_reason` must be substantively new.** "
            "Rewording the same intent (\"check X\" → \"verify X config\") "
            "is a red flag — stop. If you can't articulate a NEW unknown "
            "this call resolves, don't make the call.\n"
            "3. **Stop exploring once you have enough.** A turn with 4+ "
            "read_file calls and no write_file is the failure mode. Read "
            "the minimum, then either write code or finalize the step. "
            "Exploration without production is wasted budget.\n"
            "4. **No actionable task ⇒ no exploration.** If the user / "
            "delegating agent gave you a vague trigger (e.g. \"check inbox\", "
            "\"继续\", \"工作\") and your inbox is empty / has no concrete "
            "assignment, DO NOT proactively read project files trying to "
            "find work. Reply with one sentence (\"等待具体任务指令\") and "
            "stop. The orchestrator will wake you when there's real work.\n"
            "5. **Stuck → summarize + ask, don't loop.** After 2-3 reads "
            "with no clear path, emit a one-line \"已知 X / 缺 Y\" summary "
            "and call plan_update(action='blocked'), or @-mention the "
            "delegating agent. Don't keep re-exploring the same paths.\n"
            "6. **Bash mode discipline.** dev servers / `npx http-server` / "
            "`npm run dev` MUST use bash(run_in_background=true). `bash cd "
            "<dir>` as a standalone call is anti-pattern (cd doesn't persist "
            "between bash calls — chain with `&&` instead).\n"
            "7. **Tool catalog is closed.** Tools shown in `tools[]` are the "
            "complete capability set. Don't bash `which X` / `pip show X` "
            "trying to discover other tools. If you need something not in "
            "the list, propose_skill or tell admin.\n"
            "8. **Write completes a step? Use finalize_step.** After "
            "write_file × N, call finalize_step(files=[...], step_id, "
            "milestone_id) ONCE — registers all deliverables, closes the "
            "step, transitions the milestone. Don't manually loop "
            "submit_deliverable + plan_update + update_milestone_status; "
            "that's 6-9 wasted tool calls.\n"
            "</execution_discipline>"
        )


@dataclass
class AdminInstructionSchema(LLMVisibleSchema):
    """ADMIN messages directed at the agent (project chat). Already
    filtered upstream by @-mention + timestamp (see project.py admin
    block builder); this schema just wraps the pre-built markdown.
    """

    has_pending: bool = field(default=False, metadata={"llm": True})
    pause_active: bool = field(default=False, metadata={"llm": True})
    markdown_fallback: str = field(default="", metadata={"llm": True})

    def to_llm_markdown(self) -> str:
        return self.markdown_fallback


# ─── Project group-chat scope ────────────────────────────────────────


@dataclass
class TeamMemberSchema:
    """One project member, surfaced in the system prompt so the LLM
    knows the agent_id to put into create_milestone /
    update_milestone_responsibility / send_message / handoff_request.
    """

    agent_id: str = ""
    name: str = ""
    role: str = ""
    responsibility: str = ""


@dataclass
class ChatTurnSchema:
    """One past message in the project group chat. Phase-3 (#2)
    target: render these into messages[] instead of inlining into the
    user message — for prompt-cache friendliness.
    """

    sender: str = ""           # display label like "user" / "pm-小明"
    sender_id: str = ""        # raw agent_id or "" for human user / admin
    sender_role: str = ""      # "user" | "admin" | agent role
    content: str = ""
    timestamp: float = 0.0


@dataclass
class ProjectScopeSchema(LLMVisibleSchema):
    """Project-chat context handed to an agent during a group-chat
    turn. Replaces the legacy hand-concatenated string in
    project.py:_build_chat_prompt with structured fields.

    For now the rendering still emits the same markdown the legacy
    path produced (so this is a behavior-preserving refactor — diff
    on the LLM side is byte-equivalent). Phase-3 (#2) will move
    ``recent_messages`` out of the rendered string into messages[].
    """

    # Identity
    project_id: str = field(default="", metadata={"llm": True})
    project_name: str = field(default="", metadata={"llm": True})
    paused: bool = field(default=False, metadata={"llm": True})

    # Agent's role within this project
    responsibility: str = field(default="", metadata={"llm": True})

    # Team roster — structured so LLM sees agent_id alongside name/role
    members: list = field(default_factory=list, metadata={"llm": True})

    # Pre-rendered markdown sub-blocks. Each one is independently
    # auditable; later phases can replace any of them with stronger
    # typed sub-schemas without changing this class's shape.
    admin_block_md: str = field(default="", metadata={"llm": True})
    pause_block_md: str = field(default="", metadata={"llm": True})
    task_lines_md: str = field(default="", metadata={"llm": True})
    workflow_status_md: str = field(default="", metadata={"llm": True})
    goals_md: str = field(default="", metadata={"llm": True})
    milestones_md: str = field(default="", metadata={"llm": True})
    other_tasks_md: str = field(default="", metadata={"llm": True})
    save_path_md: str = field(default="", metadata={"llm": True})
    project_tools_md: str = field(default="", metadata={"llm": True})
    delegation_rules_md: str = field(default="", metadata={"llm": True})

    # Recent group-chat backlog. Explicit list so #2 can move them
    # from the rendered user message into messages[].
    recent_messages: list = field(default_factory=list, metadata={"llm": True})

    # The user message that triggered this turn
    user_msg: str = field(default="", metadata={"llm": True})

    # Trailer line ("请以你的角色和职责回复…"). Pre-rendered so caller
    # can swap wording without editing this class.
    trailer_md: str = field(default="", metadata={"llm": True})

    def _format_team_lines(self) -> str:
        if not self.members:
            return ""
        lines = []
        for m in self.members:
            if isinstance(m, TeamMemberSchema):
                lines.append(
                    f"  - {m.role}-{m.name} [id={m.agent_id}]: {m.responsibility}"
                )
            elif isinstance(m, dict):
                lines.append(
                    f"  - {m.get('role','')}-{m.get('name','')} "
                    f"[id={m.get('agent_id','')}]: {m.get('responsibility','')}"
                )
        return "\n".join(lines)

    def _format_recent_messages(self) -> str:
        if not self.recent_messages:
            return ""
        lines = []
        for t in self.recent_messages:
            if isinstance(t, ChatTurnSchema):
                lines.append(f"[{t.sender}]: {t.content}")
            elif isinstance(t, dict):
                lines.append(f"[{t.get('sender','')}]: {t.get('content','')}")
            elif isinstance(t, str):
                lines.append(t)
        return "\n".join(lines)

    def to_llm_string(self) -> str:
        """Render the legacy user-message format. Output is byte-
        equivalent to the previous _build_chat_prompt concatenation.
        """
        team_block = self._format_team_lines()
        ctx = self._format_recent_messages()

        return (
            f"{self.admin_block_md}"
            f"{self.pause_block_md}"
            f"[项目群聊 — {self.project_name}]\n"
            f"你的职责: {self.responsibility}\n"
            f"\n团队成员:\n" + team_block +
            f"{self.task_lines_md}\n"
            f"{self.workflow_status_md}"
            f"{self.goals_md}"
            f"{self.milestones_md}"
            f"{self.other_tasks_md}"
            f"{self.save_path_md}"
            f"{self.project_tools_md}"
            f"{self.delegation_rules_md}"
            f"\n最近聊天记录:\n{ctx}\n"
            f"\n[User]: {self.user_msg}\n"
            f"{self.trailer_md}"
        )

    def to_llm_markdown(self) -> str:
        """LLMVisibleSchema interface — same content as to_llm_string."""
        return self.to_llm_string()


# ─────────────────────────────────────────────────────────────────────
# Adapters (Phase-2 schemas)
# ─────────────────────────────────────────────────────────────────────


def from_execution_plan(plan: Any) -> PlanStateSchema:
    """Build PlanStateSchema from an ExecutionPlan instance."""
    if plan is None:
        return PlanStateSchema()
    try:
        from ..agent_types import StepStatus as _SS
    except Exception:
        _SS = None

    def _proj_step(s: Any) -> PlanStepSchema:
        status_val = s.status
        if _SS is not None and hasattr(status_val, "value"):
            status_str = status_val.value
        else:
            status_str = str(status_val)
        return PlanStepSchema(
            order=int(getattr(s, "order", 0) or 0),
            title=str(getattr(s, "title", "") or ""),
            status=status_str,
            acceptance=str(getattr(s, "acceptance", "") or ""),
            result_summary=str(getattr(s, "result_summary", "") or "")[:200],
            blocked_by=list(getattr(s, "blocked_by", []) or []),
        )

    steps = list(getattr(plan, "steps", []) or [])
    schema = PlanStateSchema(
        task_summary=str(getattr(plan, "task_summary", "") or ""),
        plan_id=str(getattr(plan, "id", "") or ""),
        plan_status=str(getattr(plan, "status", "") or ""),
    )
    if _SS is not None:
        schema.current_steps = [_proj_step(s) for s in steps if s.status == _SS.IN_PROGRESS]
        schema.done_steps = [_proj_step(s) for s in steps
                             if s.status in (_SS.COMPLETED, _SS.SKIPPED)]
        schema.pending_steps = [_proj_step(s) for s in steps if s.status == _SS.PENDING]
        schema.failed_steps = [_proj_step(s) for s in steps if s.status == _SS.FAILED]
    return schema


def from_intent(intent: Any, hint_text: str = "") -> IntentHintSchema:
    """Build IntentHintSchema from a ResolvedIntent instance + hint."""
    if intent is None:
        return IntentHintSchema()
    slots = {}
    try:
        slots = {k: v.value for k, v in (intent.slots or {}).items()
                 if getattr(v, "extracted", False) and getattr(v, "value", None)}
    except Exception:
        slots = {}
    return IntentHintSchema(
        category=str(getattr(intent, "category", "") or ""),
        hint_text=hint_text or "",
        extracted_slots=slots,
        confidence=float(getattr(intent, "confidence", 0.0) or 0.0),
    )


def from_playbook(role_id: str, scopes: list, ctx_text: str,
                   preset_version: int = 0) -> PlaybookSchema:
    """Build PlaybookSchema from playbook_runtime output."""
    return PlaybookSchema(
        role_id=role_id or "",
        active_scopes=list(scopes or []),
        markdown_fallback=ctx_text or "",
        preset_version=int(preset_version or 0),
    )


def from_memory_recall(markdown: str, query: str = "",
                        budget_chars: int = 0) -> MemoryRecallSchema:
    """Build MemoryRecallSchema wrapping memory_manager output."""
    return MemoryRecallSchema(
        query=query,
        markdown_fallback=markdown or "",
        total_chars_used=len(markdown or ""),
        budget_chars=int(budget_chars or 0),
    )


def from_git_context_markdown(markdown: str) -> GitContextSchema:
    """Build GitContextSchema from the legacy markdown blob."""
    return GitContextSchema(markdown_fallback=markdown or "")


def from_knowledge_wiki(markdown: str, total: int = 0) -> KnowledgeWikiSchema:
    """Build KnowledgeWikiSchema from knowledge.get_prompt_summary output."""
    return KnowledgeWikiSchema(
        markdown_fallback=markdown or "",
        total_count=int(total or 0),
    )


def from_scheduled_context(markdown: str, count: int = 0) -> ScheduledContextSchema:
    """Build ScheduledContextSchema from agent._get_scheduled_context."""
    return ScheduledContextSchema(
        markdown_fallback=markdown or "",
        scheduled_count=int(count or 0),
    )


def from_admin_block(markdown: str, pause_active: bool = False) -> AdminInstructionSchema:
    """Build AdminInstructionSchema from project.py admin_cmds_block builder."""
    return AdminInstructionSchema(
        has_pending=bool(markdown.strip()),
        pause_active=bool(pause_active),
        markdown_fallback=markdown or "",
    )


# ─────────────────────────────────────────────────────────────────────
# Block-level renderers (uniform interface for prompt assembly)
# ─────────────────────────────────────────────────────────────────────


def render_block(schema: LLMVisibleSchema) -> str:
    """Generic render — calls schema.to_llm_markdown() with empty-string
    safety. Lets agent_llm.py iterate a list of schemas without each
    site having to worry about None / empty."""
    if schema is None:
        return ""
    try:
        out = schema.to_llm_markdown()
    except Exception:
        return ""
    return out or ""


__all__ = [
    "LLMVisibleSchema",
    "ParamSpec",
    "ToolSchema",
    "SkillSchema",
    "RuleSchema",
    # Phase-2 schemas
    "PlanStepSchema",
    "PlanStateSchema",
    "IntentHintSchema",
    "PlaybookSchema",
    "FactSchema",
    "MemoryRecallSchema",
    "GitContextSchema",
    "KnowledgeWikiSchema",
    "WorkspaceFilesSchema",
    "ScheduledContextSchema",
    "AdminInstructionSchema",
    # Adapters
    "from_tool_definition",
    "from_skill_install",
    "from_rule_engine_rule",
    "from_execution_plan",
    "from_intent",
    "from_playbook",
    "from_memory_recall",
    "from_git_context_markdown",
    "from_knowledge_wiki",
    "from_scheduled_context",
    "from_admin_block",
    # Renderers
    "render_tools_block",
    "render_skills_block",
    "render_rules_block",
    "render_tool_signature",
    "render_block",
]
