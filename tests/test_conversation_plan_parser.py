"""Tests for plan / step-completion extraction.

The parser must handle messy real-world LLM output — these cases
mirror the kinds of drift we see from quantized Qwen / small models.
"""
from __future__ import annotations

import pytest

from app.conversation_plan_parser import (
    extract_plan, find_completed_step_markers, ExtractedStep,
)


# ── extract_plan happy paths ──────────────────────────────────────────

def test_clean_plan_cn():
    text = (
        "好的，我来规划一下：\n"
        "\n"
        "📋 计划\n"
        "1. 搜索中东公有云市场数据 — 工具: web_search\n"
        "2. 抓取详情页面 — 工具: web_fetch\n"
        "3. 生成 PPTX — 工具: bash\n"
        "4. 发邮件 — 工具: send_email\n"
        "\n"
        "开始第一步。\n"
    )
    steps = extract_plan(text)
    assert len(steps) == 4
    assert steps[0].goal == "搜索中东公有云市场数据"
    assert steps[0].tool_hint == "web_search"
    assert steps[3].tool_hint == "send_email"


def test_clean_plan_en():
    text = (
        "Plan:\n"
        "1. Search the market data — tool: web_search\n"
        "2. Extract details — tool: web_fetch\n"
        "3. Write report\n"
        "\n"
        "Starting now.\n"
    )
    steps = extract_plan(text)
    assert len(steps) == 3
    assert steps[2].goal == "Write report"
    assert steps[2].tool_hint == ""   # no tool hint given


def test_plan_inside_fenced_block():
    text = (
        "Here is what I'll do:\n"
        "```\n"
        "📋 计划\n"
        "1. 检索 — 工具: web_search\n"
        "2. 汇总 — 工具: text_process\n"
        "```\n"
        "Now step 1...\n"
    )
    steps = extract_plan(text)
    assert len(steps) == 2


def test_no_plan_header_returns_empty():
    assert extract_plan("Hello there, how are you?") == []
    assert extract_plan("I searched for X and found Y.") == []


def test_plan_with_chinese_punctuation_and_step_prefix():
    text = (
        "计划\n"
        "第1步. 调研数据 — 用: web_search\n"
        "第2步. 整理成报告 — 工具: write_file\n"
    )
    steps = extract_plan(text)
    # ``第1步.`` matches "第 Step 1." form — our regex doesn't require
    # pure number — rely on numbered-line fallback
    assert len(steps) == 2
    assert "调研数据" in steps[0].goal
    assert steps[1].tool_hint == "write_file"


def test_plan_breaks_at_blank_line():
    text = (
        "📋 计划\n"
        "1. 第一步\n"
        "2. 第二步\n"
        "\n"
        "其他无关内容：\n"
        "3. 这是一段杂谈\n"
    )
    steps = extract_plan(text)
    assert len(steps) == 2   # the "3." after blank doesn't get swept up


def test_plan_breaks_at_non_numbered_line():
    text = (
        "📋 计划\n"
        "1. 第一步\n"
        "2. 第二步\n"
        "> 这是引用\n"
        "3. 这一行应该不被采到\n"
    )
    steps = extract_plan(text)
    assert len(steps) == 2


# ── step completion markers ───────────────────────────────────────────

def test_step_done_markers_cn():
    text = "完成搜索。\n✓ 第 1 步：已获取 10 个来源\n继续下一步...\n✓ 第 2 步：下载完毕\n"
    assert find_completed_step_markers(text) == [1, 2]


def test_step_done_markers_en():
    text = "✓ step 1: searched\n✓ step 2 completed\n"
    assert find_completed_step_markers(text) == [1, 2]


def test_step_done_markers_checkbox():
    text = "Progress:\n[x] step 1\n[x] step 2\n[ ] step 3"
    assert find_completed_step_markers(text) == [1, 2]


def test_step_done_markers_idempotent_on_duplicates():
    text = "✓ 第 1 步 done\n之前已说过 ✓ 第 1 步 done again"
    assert find_completed_step_markers(text) == [1]


def test_step_done_markers_empty_and_noise():
    assert find_completed_step_markers("") == []
    assert find_completed_step_markers(None) == []
    assert find_completed_step_markers("just a message") == []


def test_step_done_variant_completed_form():
    # Trailing "完成" without a check mark, e.g. "第 3 步 完成"
    text = "第 3 步 完成 了"
    assert find_completed_step_markers(text) == [3]
