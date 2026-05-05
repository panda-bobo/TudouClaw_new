"""Prompt amplifier — rewrite a short / vague user prompt into a
structured, specific one before it's sent to an agent.

Single endpoint; reuses the agent's own LLM provider/model so we don't
ship yet another LLM dependency. Failure is non-fatal: the UI falls
back to the user's original input on any error.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Body, Depends, HTTPException

from ..deps.auth import CurrentUser, get_current_user
from ..deps.hub import get_hub

logger = logging.getLogger("tudouclaw.api.prompt_amplifier")

router = APIRouter(prefix="/api/portal", tags=["prompt-amplifier"])


# ── System prompt ────────────────────────────────────────────────────
# Designed to:
#   1. Match the user's language (CN in → CN out, EN in → EN out).
#   2. Leave already-specific prompts mostly untouched (don't bloat).
#   3. For vague input: add structure (output format, sections), surface
#      implicit assumptions, list what info would help.
#   4. Never invent domain facts (no fake numbers / fake sources) —
#      structure only, content stays the user's responsibility.
#   5. Return a JSON object so the UI can render `amplified` separately
#      from a one-line `rationale` explaining the change.
_AMPLIFY_SYSTEM_PROMPT = """你是一个提示词优化助手。把用户给 AI agent 的简短/模糊提示词改写得更具体、结构化,提升输出质量。

# 改写原则
1. **保持用户原语言**(中文进 → 中文出;English in → English out)。
2. **不无脑加戏**:如果用户输入已经具体清晰(目标 + 输出格式 + 约束齐全),只做轻微润色,在 rationale 里说明"原输入已清晰"。
3. **针对模糊输入**:
   - 补**输出格式**(markdown 表格 / 段落 / bullet list / JSON schema 等,看场景选)
   - 补**衡量标准 / 验收口径**(N 条 / N 字 / 必含字段)
   - 补**结构化分节**(背景 / 目标 / 边界 / 交付物)
   - 列出**为完成此任务建议追加的信息**(如缺少时间窗、地理范围、目标受众,提醒用户补)
4. **不要编造领域事实**:不要凭空加具体数字、机构名、案例。结构是你的;事实是用户的。
5. **agent 上下文相关**:如果给了 agent 的 role/expertise/skills,改写后的 prompt 要**贴近该 agent 实际能做的事**——优先调用列出的 skills,不要让 agent 干列表里没有的事(例:agent 没装 `html-ppt` 就别要求"用 html-ppt 渲染")。

# 输出格式 — 严格 JSON,不要包 ```json``` 围栏
{
  "amplified": "<改写后的完整 prompt 文本,直接发给 agent 用>",
  "rationale": "<一句话说明为什么这么改 / 加了什么>"
}
"""


def _looks_already_specific(raw: str) -> bool:
    """Heuristic: does the input already carry enough structure that
    amplification would just be expensive padding?

    Rules (any-of triggers skip):
      - >= 200 chars (long prompts are almost always already considered)
      - contains a structural keyword indicating output format / length
        constraint / explicit deliverable (markdown, json, 表格, 字数,
        步骤, schema, regex, 输出, …)
      - looks code-like (starts with ``/``, ``#``, ``$``, contains
        ``def `` / ``class `` / ``SELECT ``)

    Conservative on purpose — false positives (skipping when amplify
    would help) just keep the user's behavior unchanged; false
    negatives (amplifying when not needed) cost a round-trip but the
    LLM usually returns the input near-unchanged.
    """
    if len(raw) >= 200:
        return True
    low = raw.lower()
    # Output-format / length / structure keywords.
    structural_markers = (
        "markdown", "json", "yaml", "csv", "schema", "regex",
        "表格", "列表", "字数", "字符", "字段", "格式", "结构",
        "schema", "步骤", "步骤如下", "至少", "不超过", "请输出",
        "请生成", "用以下格式", "按照",
    )
    if any(m in low for m in structural_markers):
        return True
    # Code-like inputs (commands, code snippets, queries).
    if raw.startswith(("/", "#", "$", "```", "SELECT ", "select ")):
        return True
    if "def " in raw or "class " in raw or "function " in raw:
        return True
    return False


def _build_user_message(raw: str, agent_ctx: str = "",
                        agent_skills: str = "") -> str:
    """Combine the agent context (if any) with the raw prompt into a
    single user message. Keep formatting minimal so the model has clear
    boundaries between context and prompt-to-rewrite."""
    parts = []
    if agent_ctx:
        parts.append("# Target agent context")
        parts.append(agent_ctx)
        parts.append("")
    if agent_skills:
        parts.append("# Available skills (rewrite must stay within these capabilities)")
        parts.append(agent_skills)
        parts.append("")
    parts.append("# User's raw prompt (rewrite this)")
    parts.append(raw)
    return "\n".join(parts)


def _agent_context_string(agent) -> str:
    """Pull a compact description of the target agent so the amplifier
    tailors the rewrite (e.g. won't expand a code-review prompt with
    marketing-style verbiage). Best-effort — returns "" if anything
    fails."""
    if agent is None:
        return ""
    try:
        bits = []
        name = (getattr(agent, "name", "") or "").strip()
        role = (getattr(agent, "role_title", "") or
                getattr(agent, "role", "") or "").strip()
        prof = getattr(agent, "profile", None)
        expertise = []
        if prof is not None:
            ex = getattr(prof, "expertise", None) or []
            if isinstance(ex, list):
                expertise = [str(x).strip() for x in ex if str(x).strip()]
        dept = (getattr(agent, "department", "") or "").strip()
        if name:
            bits.append(f"name: {name}")
        if role:
            bits.append(f"role: {role}")
        if dept:
            bits.append(f"department: {dept}")
        if expertise:
            bits.append("expertise: " + ", ".join(expertise[:6]))
        return "\n".join(bits)
    except Exception:
        return ""


def _agent_skills_string(agent, *, max_skills: int = 12,
                          desc_chars: int = 60) -> str:
    """Compact one-line-per-skill roster for the amplifier.

    Deliberately separate from the much heavier
    ``_build_granted_skills_roster`` used in the agent's own system
    prompt — that one carries QA gate boilerplate, instruction text, and
    formatting we don't want to spend tokens on for a single rewrite
    pass. Here we just want: "what can this agent actually do?"

    Format:
        - skill_name: short description (≤ desc_chars chars, single line)
        ...
        ... (+N more)

    Returns "" when the agent has no skills granted or the registry
    isn't reachable.
    """
    if agent is None:
        return ""
    try:
        from ...skills.engine import get_registry as _get_skill_registry
        reg = _get_skill_registry()
        if reg is None:
            return ""
        installs = reg.list_for_agent(agent.id)
    except Exception as e:
        logger.debug("amplifier skill roster lookup failed: %s", e)
        return ""
    if not installs:
        return ""
    sorted_installs = sorted(
        installs,
        key=lambda i: ((i.manifest.name or i.id or "").lower(), i.id),
    )
    lines: list[str] = []
    for inst in sorted_installs[:max_skills]:
        m = inst.manifest
        name = (m.name or inst.id or "?").strip()
        desc = ""
        try:
            if hasattr(m, "get_description"):
                desc = m.get_description("zh-CN") or ""
        except Exception:
            pass
        if not desc:
            desc = getattr(m, "description", "") or ""
        desc = str(desc).replace("\n", " ").strip()
        if len(desc) > desc_chars:
            desc = desc[: desc_chars - 1].rstrip() + "…"
        lines.append(f"- {name}: {desc}" if desc else f"- {name}")
    overflow = len(sorted_installs) - max_skills
    if overflow > 0:
        lines.append(f"… (+{overflow} more)")
    return "\n".join(lines)


def _resolve_provider_model(agent) -> tuple[str, str]:
    """Get (provider, model) for the LLM call. Prefer the agent's own
    bound provider/model; fall back to empty strings (llm.chat picks
    a system default)."""
    if agent is None:
        return "", ""
    try:
        if hasattr(agent, "_resolve_effective_provider_model"):
            p, m = agent._resolve_effective_provider_model()
            return (p or ""), (m or "")
    except Exception as e:
        logger.debug("agent provider resolve failed: %s", e)
    return (getattr(agent, "provider", "") or ""),  \
           (getattr(agent, "model", "") or "")


@router.post("/amplify-prompt")
async def amplify_prompt(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Rewrite a raw user prompt via LLM. Synchronous (no streaming) —
    UI shows a brief loading state, then a preview modal.

    Body:
      raw_prompt:  required. The user's original input.
      agent_id:    optional. If provided, the agent's role/expertise
                   gets folded into the amplifier's context, AND its
                   bound LLM provider/model is used.

    Returns:
      {ok: true, amplified, rationale, used_provider, used_model}
      OR {ok: false, error} on failure (UI should fall back to raw).
    """
    raw = (body.get("raw_prompt") or "").strip()
    if not raw:
        raise HTTPException(400, "raw_prompt is required")
    if len(raw) > 4000:
        # Long prompts are usually already specific; amplifying them is
        # both wasteful (token cost) and risky (LLM may truncate). Bail
        # back to the original — UI just sends as-is.
        return {
            "ok": True,
            "skipped": True,
            "amplified": raw,
            "rationale": "原始输入已较长(>4000 字符),无需改写",
        }

    # ── Cheap short-circuit: skip the LLM call when the input is
    # already structurally rich. Saves a round-trip on the common case
    # where power users hand-write specific prompts. Heuristic is
    # deliberately conservative — we'd rather pay for one extra LLM
    # call than miss a vague prompt that needed amplification. ──
    if _looks_already_specific(raw):
        return {
            "ok": True,
            "skipped": True,
            "amplified": raw,
            "rationale": "原输入已具体(含明确动词/格式/约束),无需改写",
        }

    agent_id = (body.get("agent_id") or "").strip()
    agent = hub.get_agent(agent_id) if agent_id else None
    agent_ctx = _agent_context_string(agent) if agent else ""
    agent_skills = _agent_skills_string(agent) if agent else ""
    provider, model = _resolve_provider_model(agent)

    messages = [
        {"role": "system", "content": _AMPLIFY_SYSTEM_PROMPT},
        {"role": "user",
         "content": _build_user_message(raw, agent_ctx, agent_skills)},
    ]

    try:
        from ... import llm
        # response_format=json_object asks the model to emit valid JSON.
        # Most OpenAI-compat providers honor it; for those that don't,
        # we still parse defensively below.
        resp = llm.chat_no_stream(
            messages=messages,
            provider=provider, model=model,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
    except Exception as e:
        logger.warning("amplify-prompt LLM call failed: %s", e)
        raise HTTPException(502, f"LLM call failed: {e}")

    # chat_no_stream returns either {"message":{"content":...},...} or
    # {"content": ...} depending on which code path produced it. Cover
    # both shapes.
    if isinstance(resp, dict):
        msg = resp.get("message") if isinstance(resp.get("message"), dict) else resp
        content = (msg or {}).get("content", "") or resp.get("content", "")
    else:
        content = str(resp)
    content = (content or "").strip()
    if not content:
        raise HTTPException(502, "LLM returned empty response")

    amplified = ""
    rationale = ""
    parsed = None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        # Some providers wrap JSON in ```json fences despite the format
        # hint. Strip them once and retry.
        stripped = content
        if stripped.startswith("```"):
            stripped = stripped.strip("`")
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None

    if isinstance(parsed, dict):
        amplified = str(parsed.get("amplified") or "").strip()
        rationale = str(parsed.get("rationale") or "").strip()

    if not amplified:
        # JSON parse failed entirely — treat the whole response as the
        # amplified prompt. Rationale stays empty; UI will note "无法解析理由".
        amplified = content
        rationale = "(LLM 未按 JSON 格式返回,直接展示全文)"

    return {
        "ok": True,
        "amplified": amplified,
        "rationale": rationale,
        "used_provider": provider or "(default)",
        "used_model": model or "(default)",
    }
