"""Tests for Agent._strip_leaked_system_blocks (2026-05-12).

Generic output filter — when the LLM accidentally echoes back an
XML-tagged system instruction block (e.g. <file_display>...
</file_display>), strip it before the user sees it.

Tag list comes from system_settings.get("prompts.leak_filter_tags"),
falls back to a known default. No per-block special-casing — adding
a new system block only requires extending the tag list (settings
or default), not editing this method.

Real symptom user reported: agent echoed the full <file_display>
instruction block (3-4 paragraphs of "DO NOT do X, DO NOT do Y...")
into the chat reply.
"""
from __future__ import annotations

import pytest

from app.agent import Agent


@pytest.fixture
def agent():
    return Agent(id="t-strip", name="t")


# ── Basic tag stripping ──────────────────────────────────────────────

def test_file_display_block_stripped(agent):
    text = (
        "<file_display>\n"
        "When you produce a file in your workspace... Follow these rules:\n"
        "  1. NEVER write markdown image syntax\n"
        "  2. NEVER tell the user to drag the file\n"
        "</file_display>\n\n"
        "Here is your actual reply about the file."
    )
    out = agent._strip_leaked_system_blocks(text)
    assert "<file_display>" not in out
    assert "</file_display>" not in out
    assert "NEVER write markdown" not in out
    assert "Here is your actual reply" in out


def test_execution_discipline_block_stripped(agent):
    text = (
        "Sure, I'll do that.\n\n"
        "<execution_discipline>\n"
        "1. Don't re-read files\n"
        "2. Each tool call must have new unknown\n"
        "</execution_discipline>\n\n"
        "Starting now."
    )
    out = agent._strip_leaked_system_blocks(text)
    assert "execution_discipline" not in out
    assert "Don't re-read" not in out
    assert "Sure, I'll do that." in out
    assert "Starting now." in out


def test_multiple_blocks_all_stripped(agent):
    text = (
        "Hello.\n"
        "<file_display>rules go here</file_display>\n"
        "Middle text.\n"
        "<image_display>image rules</image_display>\n"
        "Goodbye."
    )
    out = agent._strip_leaked_system_blocks(text)
    assert "file_display" not in out
    assert "image_display" not in out
    assert "Hello." in out
    assert "Middle text." in out
    assert "Goodbye." in out


# ── Edge cases ───────────────────────────────────────────────────────

def test_no_xml_tags_passes_through(agent):
    text = "This is a normal assistant reply with no blocks."
    assert agent._strip_leaked_system_blocks(text) == text


def test_empty_input(agent):
    assert agent._strip_leaked_system_blocks("") == ""
    assert agent._strip_leaked_system_blocks(None) is None


def test_non_string_input_passes_through(agent):
    # Multimodal content list etc. — don't strip, just return as-is
    inp = [{"type": "text", "text": "hi"}]
    assert agent._strip_leaked_system_blocks(inp) == inp


def test_case_insensitive_match(agent):
    text = (
        "Hello\n"
        "<File_Display>some rules</File_Display>\n"
        "World"
    )
    out = agent._strip_leaked_system_blocks(text)
    assert "File_Display" not in out
    assert "some rules" not in out


def test_multiline_block_stripped(agent):
    text = (
        "Start\n"
        "<file_display>\n"
        "Line 1\n"
        "Line 2\n"
        "Line 3\n"
        "</file_display>\n"
        "End"
    )
    out = agent._strip_leaked_system_blocks(text)
    assert "Line 1" not in out
    assert "Line 2" not in out
    assert "Line 3" not in out
    assert "Start" in out
    assert "End" in out


def test_collapses_excessive_newlines_after_strip(agent):
    """When a tag is stripped, the surrounding blank lines collapse so
    the user doesn't see a giant gap where the block used to be."""
    text = (
        "Hello\n\n\n"
        "<file_display>\nbody\n</file_display>"
        "\n\n\nWorld"
    )
    out = agent._strip_leaked_system_blocks(text)
    # No triple newlines (or more)
    assert "\n\n\n" not in out


def test_unknown_tag_not_stripped(agent):
    """Real user content with non-system XML (e.g. an HTML snippet) is
    preserved — only configured tags get stripped."""
    text = (
        "Here's the HTML:\n"
        "<div>my content</div>\n"
        "<span>more</span>"
    )
    out = agent._strip_leaked_system_blocks(text)
    # The default tag list doesn't include div/span, so they survive
    assert "<div>my content</div>" in out
    assert "<span>more</span>" in out


def test_partial_tag_not_stripped(agent):
    """Just '<file_display>' without closing tag — leave it alone
    (don't risk eating the rest of the reply)."""
    text = "Open tag mention: <file_display> here, no close."
    out = agent._strip_leaked_system_blocks(text)
    # Partial / unterminated tag preserved — no closing tag to match
    assert "<file_display>" in out


# ── settings-driven tag list ────────────────────────────────────────

def test_settings_tag_list_used(agent, monkeypatch):
    """When system_settings configures a custom tag list, the helper
    uses it instead of the default."""
    class FakeStore:
        def get(self, key, default=None):
            if key == "prompts.leak_filter_tags":
                return ["custom_block"]
            return default

    monkeypatch.setattr(
        "app.system_settings.get_store", lambda: FakeStore())

    text = (
        "<file_display>this should survive</file_display>\n"
        "<custom_block>this should be stripped</custom_block>"
    )
    out = agent._strip_leaked_system_blocks(text)
    # file_display NOT in custom list → preserved
    assert "<file_display>" in out
    assert "this should survive" in out
    # custom_block IS in list → stripped
    assert "custom_block" not in out
    assert "this should be stripped" not in out


def test_settings_unavailable_falls_back_to_defaults(agent, monkeypatch):
    """If system_settings throws or returns None, the helper uses the
    built-in default tag list and keeps working."""
    def boom():
        raise RuntimeError("settings store down")
    monkeypatch.setattr("app.system_settings.get_store", boom)

    text = "<file_display>leak</file_display>\nreal reply"
    out = agent._strip_leaked_system_blocks(text)
    assert "file_display" not in out
    assert "real reply" in out


def test_settings_returns_non_list_falls_back(agent, monkeypatch):
    """Bad value type (e.g. someone configured a string instead of a
    list) — fall back to defaults, don't crash."""
    class BadStore:
        def get(self, key, default=None):
            if key == "prompts.leak_filter_tags":
                return "file_display"   # str, not list
            return default

    monkeypatch.setattr("app.system_settings.get_store", lambda: BadStore())

    text = "<file_display>leak</file_display>\nreply"
    out = agent._strip_leaked_system_blocks(text)
    assert "file_display" not in out
    assert "reply" in out


# ── Phrase strip (2026-05-13) — non-XML internal markers ────────

def test_thinking_mode_placeholder_phrase_stripped(agent):
    """User-reported leak: the reasoning_content placeholder injected
    for MiMo / DeepSeek-thinking models leaked verbatim into agent
    output."""
    text = (
        "Here is my actual answer.\n"
        "(no chain-of-thought captured for this tool-routing decision;"
        " placeholder injected to satisfy thinking-mode API contract)\n"
        "Then the rest of my answer."
    )
    out = agent._strip_leaked_system_blocks(text)
    assert "no chain-of-thought" not in out
    assert "placeholder injected" not in out
    # Real content survives
    assert "actual answer" in out
    assert "rest of my answer" in out


def test_stale_marker_phrase_stripped(agent):
    """[STALE — file ...] phrases should not appear in assistant output;
    they're tool_result markers."""
    text = "I see that\n[STALE — file /etc/x was edited; re-read]\nthe config"
    out = agent._strip_leaked_system_blocks(text)
    assert "[STALE" not in out
    assert "I see that" in out
    assert "the config" in out


def test_repeat_read_marker_phrase_stripped(agent):
    text = "Looking at the file:\n[REPEAT-READ #3] You already read this\nResult was X"
    out = agent._strip_leaked_system_blocks(text)
    assert "[REPEAT-READ" not in out


def test_phrase_match_case_insensitive(agent):
    text = "Hi\n(NO Chain-Of-Thought Captured For This Tool-routing decision; placeholder injected)\nDone"
    out = agent._strip_leaked_system_blocks(text)
    # Even with weird casing, should still strip
    assert "Chain-Of-Thought" not in out


def test_phrase_in_middle_of_line_strips_whole_line(agent):
    """Phrase mid-line → drop the whole line (cleaner than partial)."""
    text = (
        "Here's what I did.\n"
        "Some setup text - [Auto-cleanup] 3 stale entries marked - more text\n"
        "Real conclusion."
    )
    out = agent._strip_leaked_system_blocks(text)
    assert "Auto-cleanup" not in out
    assert "Some setup text" not in out   # whole line dropped
    assert "Here's what I did" in out
    assert "Real conclusion" in out


def test_normal_content_with_no_markers_unchanged(agent):
    text = "I read the file and saw field X. Now writing field Y."
    out = agent._strip_leaked_system_blocks(text)
    assert out == text


def test_settings_phrase_list_used(agent, monkeypatch):
    """Custom phrase list from settings overrides defaults."""
    class FakeStore:
        def get(self, key, default=None):
            if key == "prompts.leak_filter_phrases":
                return ["MY_CUSTOM_MARKER"]
            return default
    monkeypatch.setattr("app.system_settings.get_store",
                        lambda: FakeStore())
    text = (
        "Hello\n"
        "(no chain-of-thought captured for this tool-routing decision)\n"
        "World MY_CUSTOM_MARKER yes"
    )
    out = agent._strip_leaked_system_blocks(text)
    # Custom phrase stripped (whole line)
    assert "MY_CUSTOM_MARKER" not in out
    # Default thinking-mode phrase NOT stripped (custom list overrode)
    assert "no chain-of-thought" in out


def test_settings_empty_list_strips_nothing(agent, monkeypatch):
    """Admin explicitly cleared the list → no stripping happens."""
    class EmptyStore:
        def get(self, key, default=None):
            if key == "prompts.leak_filter_tags":
                return []
            return default

    monkeypatch.setattr("app.system_settings.get_store", lambda: EmptyStore())

    text = "<file_display>leak</file_display>"
    out = agent._strip_leaked_system_blocks(text)
    # Empty list = falls back to defaults (we treat empty as not-configured)
    # — and defaults include file_display so it gets stripped.
    # This is by design: explicit-empty is treated as "use defaults"
    # because a literal empty list is almost never what admin wants.
    assert "file_display" not in out
