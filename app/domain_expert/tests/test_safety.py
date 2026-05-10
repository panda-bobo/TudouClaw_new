"""R4 — find_red_line_hit fires on HARD_REFUSE pattern matches."""
from __future__ import annotations

from app.domain_expert.safety import find_red_line_hit
from app.domain_expert.template import (
    CoreRedLine,
    PromptBlock,
    SpecialtyTemplate,
)


def _tpl(red_lines: list[CoreRedLine]) -> SpecialtyTemplate:
    t = SpecialtyTemplate.from_dict({
        "id": "x", "version": "1.0", "name": "x", "specialty": "x",
    })
    t.prompt = PromptBlock(core_red_lines=red_lines)
    return t


# ── Happy path ──

def test_hit_returns_rule_on_pattern_match():
    rl = CoreRedLine(id="no_lawsuit_guarantee",
                     pattern=r"保证.*胜诉",
                     message="我无法保证胜诉。",
                     severity="HARD_REFUSE")
    hit = find_red_line_hit("请保证我能胜诉", _tpl([rl]))
    assert hit is not None
    assert hit.id == "no_lawsuit_guarantee"


def test_hit_is_case_insensitive():
    rl = CoreRedLine(id="lawsuit_en", pattern=r"GUARANTEE.*win",
                     message="x", severity="HARD_REFUSE")
    hit = find_red_line_hit("can you guarantee I will win the case?",
                            _tpl([rl]))
    assert hit is not None
    assert hit.id == "lawsuit_en"


def test_returns_first_matching_rule():
    rules = [
        CoreRedLine(id="rule1", pattern=r"AAA", message="m1",
                    severity="HARD_REFUSE"),
        CoreRedLine(id="rule2", pattern=r"AAA", message="m2",
                    severity="HARD_REFUSE"),
    ]
    hit = find_red_line_hit("AAA xxx", _tpl(rules))
    assert hit.id == "rule1"


# ── Skip cases ──

def test_returns_none_for_empty_text():
    rl = CoreRedLine(id="x", pattern=r"AAA", message="m",
                     severity="HARD_REFUSE")
    assert find_red_line_hit("", _tpl([rl])) is None
    assert find_red_line_hit(None, _tpl([rl])) is None


def test_returns_none_for_none_template():
    assert find_red_line_hit("anything", None) is None


def test_returns_none_when_template_has_no_prompt():
    """SpecialtyTemplate built from minimal dict still has a default
    PromptBlock (empty), which has no red-lines — should not match."""
    t = SpecialtyTemplate.from_dict({
        "id": "x", "version": "1.0", "name": "x", "specialty": "x",
    })
    assert find_red_line_hit("any text", t) is None


def test_returns_none_when_no_rule_matches():
    rl = CoreRedLine(id="x", pattern=r"AAA", message="m",
                     severity="HARD_REFUSE")
    assert find_red_line_hit("plain question", _tpl([rl])) is None


def test_skips_rules_without_pattern():
    """A rule with no pattern is instruction-only — never auto-fires."""
    rl = CoreRedLine(id="instruction_only", pattern="",
                     message="don't do bad stuff", severity="HARD_REFUSE")
    assert find_red_line_hit("bad stuff", _tpl([rl])) is None


def test_filters_by_severity_default_hard_only():
    rules = [
        CoreRedLine(id="soft1", pattern=r"AAA", message="soft",
                    severity="SOFT_WARN"),
        CoreRedLine(id="hard1", pattern=r"BBB", message="hard",
                    severity="HARD_REFUSE"),
    ]
    # Only hard fires by default
    assert find_red_line_hit("AAA xxx", _tpl(rules)) is None
    assert find_red_line_hit("BBB xxx", _tpl(rules)).id == "hard1"


def test_filters_by_severity_explicit():
    rules = [
        CoreRedLine(id="soft1", pattern=r"AAA", message="soft",
                    severity="SOFT_WARN"),
        CoreRedLine(id="hard1", pattern=r"BBB", message="hard",
                    severity="HARD_REFUSE"),
    ]
    # Asking for SOFT_WARN gives the soft rule
    assert find_red_line_hit("AAA xxx", _tpl(rules),
                             severity="SOFT_WARN").id == "soft1"
    assert find_red_line_hit("BBB xxx", _tpl(rules),
                             severity="SOFT_WARN") is None


# ── Bad input safety ──

def test_invalid_regex_pattern_is_skipped_not_raised():
    """One broken regex must not break the whole check."""
    rules = [
        CoreRedLine(id="broken", pattern=r"(unclosed",  # invalid regex
                    message="broken", severity="HARD_REFUSE"),
        CoreRedLine(id="good", pattern=r"AAA",
                    message="good", severity="HARD_REFUSE"),
    ]
    hit = find_red_line_hit("AAA matches good", _tpl(rules))
    assert hit is not None
    assert hit.id == "good"
