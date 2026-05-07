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
    """

    name: str
    type: str              # "string" / "integer" / "number" / "boolean" / "object" / "array"
    required: bool
    description: str = ""
    default: Any = None
    enum_values: list = field(default_factory=list)
    example: str = ""

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
    params: list = field(default_factory=list, metadata={"llm": True})  # list[ParamSpec]
    aliases: list = field(default_factory=list, metadata={"llm": False})
    handler_name: str = field(default="", metadata={"llm": False})
    risk_level: str = field(default="", metadata={"llm": False})  # safe|risky|dangerous
    category: str = field(default="", metadata={"llm": False})
    audit_tags: list = field(default_factory=list, metadata={"llm": False})

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
            )

        ``multiline=False`` → one-line compact form.
        """
        if not self.params:
            return f"{self.name}()"
        segs = [
            p.to_signature_segment(with_desc=with_desc)
            for p in self.params if isinstance(p, ParamSpec)
        ]
        if multiline:
            # Drop the comma separator in multi-line — descriptions
            # often end at the line boundary and a trailing "," after
            # "# 0-indexed starting line" reads as part of the comment.
            return f"{self.name}(\n  " + "\n  ".join(segs) + "\n)"
        return f"{self.name}(" + ", ".join(segs) + ")"

    def to_llm_markdown(self) -> str:
        lines = [f"### `{self.name}`"]
        if self.description:
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
            params.append(ParamSpec(
                name=pname,
                type=pdef.get("type", "any"),
                required=pname in required,
                description=str(pdef.get("description") or "").strip(),
                default=pdef.get("default"),
                enum_values=list(pdef.get("enum") or []),
                example=str(pdef.get("example") or ""),
            ))
    return ToolSchema(
        name=fn.get("name", ""),
        description=str(fn.get("description") or "").strip(),
        params=params,
    )


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


__all__ = [
    "LLMVisibleSchema",
    "ParamSpec",
    "ToolSchema",
    "SkillSchema",
    "RuleSchema",
    "from_tool_definition",
    "from_skill_install",
    "from_rule_engine_rule",
    "render_tools_block",
    "render_skills_block",
    "render_rules_block",
    "render_tool_signature",
]
