"""app.llm — prompt cache token extraction + accounting.

Goal: ensure cache metrics from Anthropic / OpenAI / DeepSeek responses
are extracted, aggregated to global totals + per-model buckets, and
exposed via get_token_totals() with a derived cache_hit_rate.

These are the BASELINE numbers we'll use to judge whether subsequent
prompt-prefix optimizations (block-conditional system prompt, scope-
filtered tools, etc.) actually move cache hit rate.
"""
from __future__ import annotations

import pytest

from app import llm as _llm


@pytest.fixture(autouse=True)
def _reset_totals():
    """Each test starts with fresh _TOKEN_TOTALS."""
    with _llm._TOKEN_LOCK:
        _llm._TOKEN_TOTALS["total_in"] = 0
        _llm._TOKEN_TOTALS["total_out"] = 0
        _llm._TOKEN_TOTALS["calls"] = 0
        _llm._TOKEN_TOTALS["cache_read_total"] = 0
        _llm._TOKEN_TOTALS["cache_write_total"] = 0
        _llm._TOKEN_TOTALS["by_model"] = {}
    yield


# ── _extract_cache_tokens — provider-agnostic extraction ─────────────


def test_extract_anthropic_shape():
    usage = {
        "input_tokens": 12000,
        "output_tokens": 500,
        "cache_read_input_tokens": 8000,
        "cache_creation_input_tokens": 300,
    }
    assert _llm._extract_cache_tokens(usage) == (8000, 300)


def test_extract_openai_shape():
    usage = {
        "prompt_tokens": 5000,
        "completion_tokens": 200,
        "prompt_tokens_details": {"cached_tokens": 1234},
    }
    assert _llm._extract_cache_tokens(usage) == (1234, 0)


def test_extract_deepseek_shape():
    usage = {
        "prompt_tokens": 6000,
        "completion_tokens": 300,
        "prompt_cache_hit_tokens": 4500,
        "prompt_cache_miss_tokens": 1500,
    }
    # We treat hit as read; DeepSeek doesn't bill for writes separately.
    assert _llm._extract_cache_tokens(usage) == (4500, 0)


@pytest.mark.parametrize("usage", [
    {},
    None,
    {"prompt_tokens": 100, "completion_tokens": 50},  # No cache fields
    {"prompt_tokens_details": "not_a_dict"},          # Malformed
    {"prompt_tokens_details": {"cached_tokens": 0}},  # Zero hit
])
def test_extract_returns_zero_when_no_cache_data(usage):
    assert _llm._extract_cache_tokens(usage) == (0, 0)


def test_extract_anthropic_takes_precedence_when_both_present():
    """If both Anthropic and OpenAI fields somehow appear, Anthropic wins."""
    usage = {
        "cache_read_input_tokens": 1000,
        "cache_creation_input_tokens": 50,
        "prompt_tokens_details": {"cached_tokens": 999},
    }
    assert _llm._extract_cache_tokens(usage) == (1000, 50)


# ── _log_token_usage — aggregation into totals + per-model ───────────


def test_log_aggregates_cache_into_totals():
    _llm._log_token_usage(
        "claude", "claude-3-5-sonnet",
        prompt_tokens=10000, completion_tokens=500,
        cache_read=8000, cache_write=300,
    )
    t = _llm.get_token_totals()
    assert t["total_in"] == 10000
    assert t["cache_read_total"] == 8000
    assert t["cache_write_total"] == 300
    assert abs(t["cache_hit_rate"] - 0.8) < 0.001


def test_log_per_model_bucket_tracks_cache():
    _llm._log_token_usage(
        "claude", "claude-3-5-sonnet",
        prompt_tokens=5000, completion_tokens=100,
        cache_read=3000, cache_write=0,
    )
    _llm._log_token_usage(
        "claude", "claude-3-5-sonnet",
        prompt_tokens=4000, completion_tokens=80,
        cache_read=3500, cache_write=200,
    )
    t = _llm.get_token_totals()
    bucket = t["by_model"]["claude/claude-3-5-sonnet"]
    assert bucket["in"] == 9000
    assert bucket["cache_read"] == 6500
    assert bucket["cache_write"] == 200
    assert bucket["calls"] == 2


def test_cache_hit_rate_zero_when_no_input():
    """Edge case: get_token_totals with zero input shouldn't divide by zero."""
    t = _llm.get_token_totals()
    assert t["cache_hit_rate"] == 0.0


def test_cache_hit_rate_combines_across_models():
    _llm._log_token_usage("claude", "sonnet", prompt_tokens=1000,
                          completion_tokens=100, cache_read=900)
    _llm._log_token_usage("openai", "gpt-4o", prompt_tokens=2000,
                          completion_tokens=200, cache_read=0)
    t = _llm.get_token_totals()
    # 900 / 3000 = 0.30
    assert abs(t["cache_hit_rate"] - 0.30) < 0.001


def test_log_without_cache_args_does_not_break():
    """Existing call sites that don't pass cache_* still work (back-compat)."""
    _llm._log_token_usage("openai", "gpt-4o-mini",
                          prompt_tokens=500, completion_tokens=50)
    t = _llm.get_token_totals()
    assert t["total_in"] == 500
    assert t["cache_read_total"] == 0
    assert t["cache_write_total"] == 0


# ── per-scope accumulation (agent / project / etc.) ──────────────────


def test_per_scope_accumulator_includes_cache():
    """When token context names an agent, that agent's _token_stats should
    receive the cache numbers (driven by _log_token_usage's internal
    _accumulate closure).
    """
    from app.llm import set_token_context, clear_token_context

    class _StubAgent:
        pass

    agent = _StubAgent()

    class _StubHub:
        agents = {"a1": agent}
        projects = {}
        meetings = {}

    set_token_context(agent_id="a1")
    prev_hub = _llm._active_hub
    _llm._active_hub = _StubHub()
    try:
        _llm._log_token_usage(
            "claude", "sonnet",
            prompt_tokens=5000, completion_tokens=200,
            cache_read=4000, cache_write=100,
        )
    finally:
        clear_token_context()
        _llm._active_hub = prev_hub

    stats = getattr(agent, "_token_stats", None)
    assert stats is not None
    assert stats["in"] == 5000
    assert stats["cache_read"] == 4000
    assert stats["cache_write"] == 100
    assert stats["by_model"]["claude/sonnet"]["cache_read"] == 4000
