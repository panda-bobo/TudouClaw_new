"""R3 — render_specialty_system_prompt composes ① 专家 Prompt cleanly."""
from __future__ import annotations

from app.domain_expert.prompt_renderer import render_specialty_system_prompt
from app.domain_expert.template import (
    CoreRedLine,
    PromptBlock,
    SpecialtyTemplate,
)


def _tpl(prompt: PromptBlock | None) -> SpecialtyTemplate:
    """Build a minimal SpecialtyTemplate carrying the given prompt."""
    t = SpecialtyTemplate.from_dict({
        "id": "x", "version": "1.0", "name": "x", "specialty": "x",
    })
    if prompt is not None:
        t.prompt = prompt
    return t


def test_render_returns_empty_for_none_template():
    assert render_specialty_system_prompt(None) == ""


def test_render_returns_empty_for_empty_prompt_block():
    """Default PromptBlock (no role/scope/red_lines/format) renders to ""."""
    assert render_specialty_system_prompt(_tpl(PromptBlock())) == ""


def test_render_role_only():
    out = render_specialty_system_prompt(_tpl(PromptBlock(role="民法专家")))
    assert "# 角色\n民法专家" == out


def test_render_role_and_scope():
    out = render_specialty_system_prompt(_tpl(PromptBlock(
        role="民法专家",
        scope="处理民事问题",
    )))
    # Sections separated by a blank line
    assert "# 角色\n民法专家" in out
    assert "# 范围\n处理民事问题" in out
    assert out.index("# 角色") < out.index("# 范围")


def test_render_hard_red_lines_grouped_under_one_header():
    out = render_specialty_system_prompt(_tpl(PromptBlock(
        core_red_lines=[
            CoreRedLine(id="no_guarantee", message="不保证胜诉",
                        severity="HARD_REFUSE"),
            CoreRedLine(id="no_criminal", message="不接刑事案件",
                        severity="HARD_REFUSE"),
        ],
    )))
    assert "# ⚠️ 不能做的事" in out
    # Single header — only one occurrence
    assert out.count("# ⚠️ 不能做的事(硬护栏)") == 1
    assert "no_guarantee: 不保证胜诉" in out
    assert "no_criminal: 不接刑事案件" in out


def test_render_separates_hard_and_soft_into_two_sections():
    out = render_specialty_system_prompt(_tpl(PromptBlock(
        core_red_lines=[
            CoreRedLine(id="hard1", message="硬规则",
                        severity="HARD_REFUSE"),
            CoreRedLine(id="soft1", message="软提醒",
                        severity="SOFT_WARN"),
        ],
    )))
    assert "# ⚠️ 不能做的事(硬护栏)" in out
    assert "# ⚠️ 需要谨慎(软提醒)" in out
    # Hard appears before soft (HARD is more important, render first)
    assert out.index("不能做的事") < out.index("需要谨慎")


def test_render_skips_red_line_section_when_no_red_lines_of_that_severity():
    out = render_specialty_system_prompt(_tpl(PromptBlock(
        core_red_lines=[
            CoreRedLine(id="soft1", message="只有软", severity="SOFT_WARN"),
        ],
    )))
    assert "# ⚠️ 需要谨慎" in out
    assert "# ⚠️ 不能做的事" not in out


def test_render_full_block():
    """Every section together — verifies ordering and separators."""
    out = render_specialty_system_prompt(_tpl(PromptBlock(
        role="民法专家",
        scope="处理民事问题",
        core_red_lines=[
            CoreRedLine(id="rl1", message="不保证胜诉",
                        severity="HARD_REFUSE"),
        ],
        output_format="末尾标注 [来源]",
    )))
    # Expected order: 角色 → 范围 → 不能做的事 → 输出格式
    pos_role = out.index("# 角色")
    pos_scope = out.index("# 范围")
    pos_red = out.index("# ⚠️ 不能做的事")
    pos_fmt = out.index("# 输出格式")
    assert pos_role < pos_scope < pos_red < pos_fmt
    # Sections separated by blank lines (\n\n joins them)
    assert "\n\n" in out


def test_render_handles_red_line_with_no_message():
    out = render_specialty_system_prompt(_tpl(PromptBlock(
        core_red_lines=[
            CoreRedLine(id="silent_rule", severity="HARD_REFUSE"),
        ],
    )))
    assert "silent_rule: (无说明)" in out


def test_render_strips_whitespace_in_role_scope_format():
    out = render_specialty_system_prompt(_tpl(PromptBlock(
        role="  民法专家  \n",
        scope="\n  处理民事  \n",
        output_format="  标注  ",
    )))
    assert "# 角色\n民法专家" in out
    assert "# 范围\n处理民事" in out
    assert "# 输出格式\n标注" in out
