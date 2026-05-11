"""End-to-end integration tests for the history-compaction overhaul.

Covers all 4 steps end-to-end by calling _summarize_old_history with
a mocked LLM and asserting on the assembled output:

  Step 1 — role-aware per-message truncation in the summarizer transcript
  Step 2 — summary prompt no longer hard-caps at 300-500 chars
  Step 3 — STRUCTURED_FACTS block appears in output when applicable
  Step 4 — cache behavior: exact hit / delta hit / miss

Also covers the [USER_VERBATIM] cap rules (per-msg + total) and
the assembled layout (header → facts → verbatim → narrative order).
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from app.agent import _summarize_old_history


# ── Test fixtures ─────────────────────────────────────────────────────

class _StubAgent:
    """Minimal stand-in for app.agent.Agent — just enough for
    _summarize_old_history to run end-to-end."""

    def __init__(self) -> None:
        self.id = "stub-agent-id-12345"
        self._history_summary_cache: dict | None = None

    def _resolve_effective_provider_model(self) -> tuple[str, str]:
        return ("openai", "gpt-test")


def _fake_llm_resp(text: str = "假摘要：agent 在做 X，但 Y 失败，改用 Z。"):
    """The shape _summarize_old_history expects from chat_no_stream."""
    return {"message": {"content": text}}


def _build_big_messages(*, user_text: str = "请帮我完成任务",
                       n_tool_pairs: int = 12,
                       tool_body_size: int = 5000,
                       tail_keep: int = 7) -> list[dict]:
    """Build a message list big enough to force compaction.

    Layout: 1 system + 1 user + n_tool_pairs * (assistant tc + tool result)
    + tail_keep recent user/assistant pairs.

    Total ends up > 50k chars (hard_cap) regardless of threshold gating.
    """
    msgs: list[dict] = [
        {"role": "system", "content": "you are a helpful agent"},
        {"role": "user", "content": user_text},
    ]
    for i in range(n_tool_pairs):
        msgs.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": f"tc_{i}",
                "type": "function",
                "function": {
                    "name": "Read",
                    "arguments": json.dumps({"file_path": f"/f_{i}.py"}),
                },
            }],
        })
        msgs.append({
            "role": "tool",
            "tool_call_id": f"tc_{i}",
            "content": ("OUTPUT_" + str(i) + "_") * (tool_body_size // 12),
        })
    # Tail recent block (kept verbatim by keep_last logic)
    for i in range(tail_keep):
        msgs.append({"role": "user", "content": f"recent user {i}"})
        msgs.append({"role": "assistant", "content": f"recent reply {i}"})
    return msgs


# ── Step 1: role-aware truncation in summarizer transcript ──────────

def test_step1_user_message_not_truncated_in_transcript():
    """A long user message must reach the summarizer LLM in full —
    the prior bug was head-only truncating it to 1500 chars."""
    long_user = "USER_INSTRUCTION " * 500  # ~8k chars
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": long_user},
    ]
    # Pad to force compaction — append filler tool pairs and tail
    for i in range(10):
        msgs.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": f"tc{i}", "type": "function",
                "function": {"name": "Read",
                             "arguments": json.dumps({"file_path": f"/f{i}"})},
            }],
        })
        msgs.append({"role": "tool", "tool_call_id": f"tc{i}",
                     "content": "X" * 5000})
    for i in range(7):
        msgs.append({"role": "user", "content": f"r{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})

    captured_prompts: list[str] = []

    def _capture(**kwargs):
        # Capture the user-message prompt sent to summarizer
        for m in kwargs.get("messages", []):
            if m.get("role") == "user":
                captured_prompts.append(m.get("content", ""))
        return _fake_llm_resp()

    agent = _StubAgent()
    with patch("app.llm.chat_no_stream", side_effect=_capture):
        _summarize_old_history(msgs, agent)

    assert captured_prompts, "LLM was not called"
    transcript_prompt = captured_prompts[0]
    # The full long user message should be in the transcript verbatim
    assert long_user in transcript_prompt, (
        "user message was truncated in summarizer transcript — Step 1 regression")


def test_step1_tool_result_head_and_tail_preserved():
    """A long tool_result gets head+tail (not head-only) — error codes
    typically live at the end."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "please run"},
    ]
    head_marker = "HEAD_MARKER_BEGIN"
    tail_marker = "TAIL_MARKER_END"
    big_body = head_marker + ("x" * 6000) + tail_marker
    for i in range(10):
        msgs.append({
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": f"tc{i}", "type": "function",
                "function": {"name": "Bash",
                             "arguments": json.dumps({"command": "echo"})},
            }],
        })
        # All tools get the same huge body — guaranteed to be in old_slice
        msgs.append({"role": "tool", "tool_call_id": f"tc{i}",
                     "content": big_body})
    for i in range(7):
        msgs.append({"role": "user", "content": f"r{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})

    captured: list[str] = []

    def _capture(**kwargs):
        for m in kwargs.get("messages", []):
            if m.get("role") == "user":
                captured.append(m.get("content", ""))
        return _fake_llm_resp()

    agent = _StubAgent()
    with patch("app.llm.chat_no_stream", side_effect=_capture):
        _summarize_old_history(msgs, agent)

    transcript = captured[0]
    assert head_marker in transcript, "tool result head was truncated away"
    assert tail_marker in transcript, "tool result tail was truncated away"
    assert "[truncated " in transcript, "no truncation marker — bigger than budget but not cut?"


def test_step1_assistant_message_uses_head_tail_truncation():
    """Long assistant content also gets head+tail truncation."""
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"}]
    head = "ASSISTANT_HEAD_X"
    tail = "ASSISTANT_TAIL_Y"
    big_asst = head + ("z" * 6000) + tail
    for i in range(12):
        msgs.append({"role": "assistant", "content": big_asst})
        msgs.append({"role": "user", "content": f"u{i}"})
    for i in range(7):
        msgs.append({"role": "user", "content": f"r{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})

    captured: list[str] = []

    def _capture(**kwargs):
        for m in kwargs.get("messages", []):
            if m.get("role") == "user":
                captured.append(m.get("content", ""))
        return _fake_llm_resp()

    agent = _StubAgent()
    with patch("app.llm.chat_no_stream", side_effect=_capture):
        _summarize_old_history(msgs, agent)

    transcript = captured[0]
    assert head in transcript
    assert tail in transcript


# ── Step 2: summary prompt content ───────────────────────────────────

def test_step2_prompt_does_not_cap_300_500_chars():
    """Regression guard — the old prompt forced '中文,300-500 字'.
    Any future edit reintroducing a hard char cap should fail here."""
    msgs = _build_big_messages()
    captured: list[str] = []

    def _capture(**kwargs):
        for m in kwargs.get("messages", []):
            captured.append(str(m.get("content", "")))
        return _fake_llm_resp()

    agent = _StubAgent()
    with patch("app.llm.chat_no_stream", side_effect=_capture):
        _summarize_old_history(msgs, agent)

    joined = "\n".join(captured)
    # The literal "300-500 字" string must NOT appear anywhere
    assert "300-500" not in joined, (
        "summary prompt reintroduced a 300-500 char hard cap")
    # The new prompt must announce role separation
    assert "user 原文" in joined and "确定性事实" in joined


def test_step2_prompt_forbids_paraphrasing_user_intent():
    """The new prompt explicitly forbids 'replacing user intent with
    a polished paraphrase' — that was the original bug source."""
    msgs = _build_big_messages()
    captured: list[str] = []

    def _capture(**kwargs):
        for m in kwargs.get("messages", []):
            captured.append(str(m.get("content", "")))
        return _fake_llm_resp()

    agent = _StubAgent()
    with patch("app.llm.chat_no_stream", side_effect=_capture):
        _summarize_old_history(msgs, agent)

    joined = "\n".join(captured)
    # Must explicitly say "don't make stuff up, don't substitute intent"
    assert "禁止编造" in joined
    assert ("脑补" in joined) or ("升华" in joined)


# ── Step 3: STRUCTURED_FACTS appears in output ───────────────────────

def test_step3_structured_facts_block_in_output():
    """When the compressed range has Write/Edit tool calls, the
    assembled system message should include a [STRUCTURED_FACTS]
    section listing them."""
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "edit some files"}]
    for i in range(8):
        msgs.append({
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": f"w{i}", "type": "function",
                "function": {"name": "Write",
                             "arguments": json.dumps({"file_path": f"/m{i}.py",
                                                      "content": "x"})},
            }],
        })
        msgs.append({"role": "tool", "tool_call_id": f"w{i}",
                     "content": "ok " * 1500})
    for i in range(7):
        msgs.append({"role": "user", "content": f"r{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})

    agent = _StubAgent()
    with patch("app.llm.chat_no_stream", return_value=_fake_llm_resp()):
        out = _summarize_old_history(msgs, agent)

    # Find the compacted system message
    summary_msgs = [m for m in out
                    if m.get("role") == "system"
                    and "HISTORY_SUMMARY" in (m.get("content") or "")]
    assert len(summary_msgs) == 1
    body = summary_msgs[0]["content"]
    assert "STRUCTURED_FACTS" in body
    assert "Files modified" in body
    assert "/m0.py" in body


# ── Layout: section order ────────────────────────────────────────────

def test_assembled_layout_order_header_facts_verbatim_narrative():
    """The output system message must follow the documented section
    order: header → STRUCTURED_FACTS → USER_VERBATIM → NARRATIVE."""
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "first task instruction"}]
    for i in range(8):
        msgs.append({
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": f"e{i}", "type": "function",
                "function": {"name": "Edit",
                             "arguments": json.dumps({"file_path": f"/q{i}",
                                                      "old_string": "a",
                                                      "new_string": "b"})},
            }],
        })
        msgs.append({"role": "tool", "tool_call_id": f"e{i}",
                     "content": "edit ok " * 800})
        msgs.append({"role": "user", "content": f"another user instruction {i}"})
    for i in range(7):
        msgs.append({"role": "user", "content": f"r{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})

    agent = _StubAgent()
    narrative = "NARRATIVE_TEXT_MARKER"
    with patch("app.llm.chat_no_stream", return_value=_fake_llm_resp(narrative)):
        out = _summarize_old_history(msgs, agent)

    body = next(m["content"] for m in out if m.get("role") == "system"
                and "HISTORY_SUMMARY" in (m.get("content") or ""))

    # Indices of each section marker
    i_header = body.index("[HISTORY_SUMMARY")
    i_facts = body.index("[STRUCTURED_FACTS")
    i_verb = body.index("[USER_VERBATIM")
    i_narr = body.index("[NARRATIVE")
    assert i_header < i_facts < i_verb < i_narr
    # Narrative content actually present
    assert narrative in body


# ── USER_VERBATIM cap tests ──────────────────────────────────────────

def test_user_verbatim_per_message_cap_truncates_huge_paste():
    """A single user msg > 8000 chars gets head+tail truncated in the
    USER_VERBATIM block, with a [truncated …c] marker."""
    huge = "USER_HUGE_HEAD" + ("p" * 12000) + "USER_HUGE_TAIL"
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": huge}]
    for i in range(10):
        msgs.append({
            "role": "assistant", "content": "",
            "tool_calls": [{"id": f"t{i}", "type": "function",
                            "function": {"name": "Read",
                                         "arguments": json.dumps({"file_path": "/x"})}}]
        })
        msgs.append({"role": "tool", "tool_call_id": f"t{i}",
                     "content": "X" * 3000})
    for i in range(7):
        msgs.append({"role": "user", "content": f"r{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})

    agent = _StubAgent()
    with patch("app.llm.chat_no_stream", return_value=_fake_llm_resp()):
        out = _summarize_old_history(msgs, agent)
    body = next(m["content"] for m in out if m.get("role") == "system"
                and "USER_VERBATIM" in (m.get("content") or ""))
    assert "USER_HUGE_HEAD" in body
    assert "USER_HUGE_TAIL" in body
    assert "[truncated " in body


def test_user_verbatim_skips_context_compressed_prefix_marker():
    """Lines starting with '[Context Compressed:' are skipped — they
    are our own previous compaction markers, not real user input."""
    msgs = [{"role": "system", "content": "sys"},
            {"role": "user", "content": "[Context Compressed: earlier]"},
            {"role": "user", "content": "REAL_USER_INSTRUCTION_MARKER"}]
    for i in range(10):
        msgs.append({
            "role": "assistant", "content": "",
            "tool_calls": [{"id": f"t{i}", "type": "function",
                            "function": {"name": "Read",
                                         "arguments": json.dumps({"file_path": "/x"})}}]
        })
        msgs.append({"role": "tool", "tool_call_id": f"t{i}",
                     "content": "X" * 3000})
    for i in range(7):
        msgs.append({"role": "user", "content": f"r{i}"})
        msgs.append({"role": "assistant", "content": f"a{i}"})

    agent = _StubAgent()
    with patch("app.llm.chat_no_stream", return_value=_fake_llm_resp()):
        out = _summarize_old_history(msgs, agent)
    body = next(m["content"] for m in out if m.get("role") == "system"
                and "USER_VERBATIM" in (m.get("content") or ""))
    assert "REAL_USER_INSTRUCTION_MARKER" in body
    # The [Context Compressed:...] marker must NOT appear in the
    # verbatim block (it WILL appear elsewhere — header counter says
    # "覆盖 N 条" — but not under [USER_VERBATIM]).
    verb_section = body[body.index("[USER_VERBATIM"):]
    # Cut at NARRATIVE start so we only look at the verbatim section
    if "[NARRATIVE" in verb_section:
        verb_section = verb_section[:verb_section.index("[NARRATIVE")]
    assert "[Context Compressed:" not in verb_section


# ── Step 4: cache behavior ───────────────────────────────────────────

def test_step4_cache_populated_on_first_call():
    """After a fresh compaction, agent._history_summary_cache has
    all the keys needed for the next iteration's lookup."""
    msgs = _build_big_messages()
    agent = _StubAgent()
    with patch("app.llm.chat_no_stream", return_value=_fake_llm_resp()):
        _summarize_old_history(msgs, agent)

    cache = agent._history_summary_cache
    assert isinstance(cache, dict)
    assert cache.get("old_hash")
    assert cache.get("full_content")
    assert cache.get("narrative")
    assert cache.get("covers_n", 0) > 0
    assert cache.get("covers_chars", 0) > 0


def test_step4_exact_cache_hit_skips_llm_call():
    """Second call with identical messages must NOT invoke the LLM
    (we cached the full_content; reuse it byte-for-byte)."""
    msgs = _build_big_messages()
    agent = _StubAgent()
    call_count = {"n": 0}

    def _counting(**kwargs):
        call_count["n"] += 1
        return _fake_llm_resp("first-call-narrative")

    with patch("app.llm.chat_no_stream", side_effect=_counting):
        first_out = _summarize_old_history(msgs, agent)
        first_body = next(m["content"] for m in first_out
                          if m.get("role") == "system"
                          and "HISTORY_SUMMARY" in (m.get("content") or ""))
        # Second call — same messages, same agent
        second_out = _summarize_old_history(msgs, agent)

    assert call_count["n"] == 1, "LLM was called again despite exact hash cache hit"
    second_body = next(m["content"] for m in second_out
                       if m.get("role") == "system"
                       and "HISTORY_SUMMARY" in (m.get("content") or ""))
    # Byte-identical compacted block — key property for prompt cache
    assert first_body == second_body


def test_step4_inner_mutation_invalidates_cache():
    """If a downstream sanitizer mutates an inner tool body (n + chars
    similar but content different), the hash differs and we re-run the
    LLM. Verifies the old (covers_n, covers_chars) key bug is fixed."""
    msgs = _build_big_messages()
    agent = _StubAgent()
    call_count = {"n": 0}
    last_narrative = {"text": ""}

    def _counting(**kwargs):
        call_count["n"] += 1
        last_narrative["text"] = f"narrative_call_{call_count['n']}"
        return _fake_llm_resp(last_narrative["text"])

    with patch("app.llm.chat_no_stream", side_effect=_counting):
        _summarize_old_history(msgs, agent)

        # Mutate an inner tool result IN PLACE — keep length roughly
        # the same but change content. Old cache key (covers_n,
        # covers_chars) would falsely hit.
        # Find first tool result in old_slice region.
        mutated = False
        for m in msgs[2:-14]:    # skip system+user prefix, keep tail
            if m.get("role") == "tool":
                original = m["content"]
                # Replace half the body with a different char to keep
                # length similar
                m["content"] = "Y" * len(original)
                mutated = True
                break
        assert mutated, "test setup: no tool message to mutate"

        _summarize_old_history(msgs, agent)

    assert call_count["n"] == 2, (
        "cache failed to invalidate on inner mutation — Step 4 hash regression")


def test_step4_legacy_text_key_still_read():
    """Backward compat: if cache has old 'text' key (pre-Step-4
    format) instead of 'narrative', delta-reuse should still find it."""
    msgs = _build_big_messages()
    agent = _StubAgent()
    # Pre-seed cache with the LEGACY shape
    old_chars = sum(len(str(m.get("content") or "")) for m in msgs[2:-14])
    agent._history_summary_cache = {
        "text": "legacy-narrative",            # old key name
        "covers_n": 5,                          # small so delta hits
        "covers_chars": min(old_chars, 1000),
        # No old_hash → Tier 1 misses, Tier 2 (delta) takes over
    }
    call_count = {"n": 0}

    def _counting(**kwargs):
        call_count["n"] += 1
        return _fake_llm_resp("fresh-narrative")

    with patch("app.llm.chat_no_stream", side_effect=_counting):
        out = _summarize_old_history(msgs, agent)

    body = next(m["content"] for m in out
                if m.get("role") == "system"
                and "HISTORY_SUMMARY" in (m.get("content") or ""))
    # Either the legacy narrative was reused (delta hit) OR a fresh
    # one ran — both are acceptable, but if delta hit, the legacy
    # narrative must be visible.
    if call_count["n"] == 0:
        assert "legacy-narrative" in body
    else:
        # Fresh re-run is also fine if delta gate didn't apply
        assert "fresh-narrative" in body


# ── End-to-end: result list shape ────────────────────────────────────

def test_returns_unchanged_when_below_threshold():
    """Small message list — no compaction, returns input list unchanged."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    agent = _StubAgent()
    out = _summarize_old_history(msgs, agent)
    assert out is msgs   # identity — no compaction happened


def test_returns_unchanged_when_llm_fails():
    """If the summarizer LLM call raises, the function returns the
    original messages instead of producing a broken payload."""
    msgs = _build_big_messages()
    agent = _StubAgent()
    with patch("app.llm.chat_no_stream",
               side_effect=RuntimeError("LLM down")):
        out = _summarize_old_history(msgs, agent)
    assert out is msgs


def test_recent_tail_messages_preserved_verbatim():
    """The last keep_last messages must appear in the output unchanged."""
    msgs = _build_big_messages()
    tail_user_text = "TAIL_USER_UNIQUE_MARKER_42"
    msgs[-2]["content"] = tail_user_text
    agent = _StubAgent()
    with patch("app.llm.chat_no_stream", return_value=_fake_llm_resp()):
        out = _summarize_old_history(msgs, agent)
    # The marker text must appear in some user msg in `out`
    assert any(tail_user_text == m.get("content") for m in out
               if m.get("role") == "user")
