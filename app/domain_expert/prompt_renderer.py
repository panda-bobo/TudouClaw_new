"""Compose a system prompt from a SpecialtyTemplate's PromptBlock.

The output is the ① piece of the 4-piece formula assembled into a
single Markdown-flavored string suitable for use as a system message:

    # 角色
    {prompt.role}

    # 范围
    {prompt.scope}

    # ⚠️ 不能做的事(硬护栏)
      - {rl.id}: {rl.message}
      - ...

    # ⚠️ 需要谨慎(软提醒)
      - ...

    # 输出格式
    {prompt.output_format}

Empty sections are skipped so a partially-filled PromptBlock still
renders cleanly. Missing or None template returns "".

The render is pure: no LLM call, no IO, no template-loader access.
Callers (agent.chat() in R4, the cultivation API in R8) decide whether
to use it as the WHOLE system message or to merge it with a per-agent
profile override.
"""
from __future__ import annotations

from .template import SpecialtyTemplate


def render_specialty_system_prompt(template: SpecialtyTemplate | None) -> str:
    """Compose the system prompt from ``template.prompt``.

    Returns "" when the template is None or has no PromptBlock content.
    """
    if template is None or template.prompt is None:
        return ""
    p = template.prompt
    parts: list[str] = []

    if p.role:
        parts.append(f"# 角色\n{p.role.strip()}")
    if p.scope:
        parts.append(f"# 范围\n{p.scope.strip()}")

    hard = [rl for rl in p.core_red_lines if rl.severity == "HARD_REFUSE"]
    if hard:
        lines = ["# ⚠️ 不能做的事(硬护栏)"]
        for rl in hard:
            msg = rl.message.strip() if rl.message else "(无说明)"
            lines.append(f"  - {rl.id}: {msg}")
        parts.append("\n".join(lines))

    soft = [rl for rl in p.core_red_lines if rl.severity == "SOFT_WARN"]
    if soft:
        lines = ["# ⚠️ 需要谨慎(软提醒)"]
        for rl in soft:
            msg = rl.message.strip() if rl.message else "(无说明)"
            lines.append(f"  - {rl.id}: {msg}")
        parts.append("\n".join(lines))

    if p.output_format:
        parts.append(f"# 输出格式\n{p.output_format.strip()}")

    return "\n\n".join(parts)
