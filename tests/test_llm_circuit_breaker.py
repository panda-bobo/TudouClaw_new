"""Unit tests for the LLM connection-pool circuit breaker.

Scenario these guard against:
  mlx-lm's tool parser crashes on malformed XML → connection drops
  mid-response → our client retries 5x per call → agent loop makes
  20 calls → 100 failed attempts × 30s each = 50-minute agent freeze.

The breaker trips after 2 failures in 120s and blocks subsequent calls
with LLM_CIRCUIT_OPEN for 180s, so the agent fails fast instead of
hammering a crashed server.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def pool():
    """Fresh pool with short cooldown / window for fast tests."""
    from app.llm import LLMConnectionPool
    p = LLMConnectionPool()
    p.cb_threshold = 2
    p.cb_window_s = 10.0
    p.cb_cooldown_s = 5.0
    return p


def test_breaker_starts_closed(pool):
    """Fresh pool lets everything through."""
    pool._cb_check("anyprov:anymodel")   # no raise


def test_breaker_trips_after_N_failures(pool):
    """N failures in window → subsequent _cb_check raises LLM_CIRCUIT_OPEN."""
    key = "provA:modelX"
    pool._cb_record_failure(key)
    pool._cb_check(key)  # still closed
    pool._cb_record_failure(key)  # threshold hit
    with pytest.raises(RuntimeError, match="LLM_CIRCUIT_OPEN"):
        pool._cb_check(key)


def test_breaker_is_key_scoped(pool):
    """Failures on provider A don't open provider B."""
    pool._cb_record_failure("provA:m1")
    pool._cb_record_failure("provA:m1")
    # A is open
    with pytest.raises(RuntimeError):
        pool._cb_check("provA:m1")
    # B is fine
    pool._cb_check("provB:m1")


def test_breaker_success_resets(pool):
    """A successful call clears the failure counter."""
    key = "provA:mX"
    pool._cb_record_failure(key)
    pool._cb_record_success(key)
    pool._cb_record_failure(key)  # back to 1, still below threshold
    pool._cb_check(key)  # no raise


def test_breaker_auto_reopens_after_cooldown(pool):
    """After cooldown, next check stops raising and resets state."""
    import time
    key = "provA:mX"
    pool.cb_cooldown_s = 0.2
    pool._cb_record_failure(key)
    pool._cb_record_failure(key)
    with pytest.raises(RuntimeError):
        pool._cb_check(key)
    time.sleep(0.25)
    pool._cb_check(key)   # cooldown elapsed, no raise
    # Fresh failures start a new window
    pool._cb_record_failure(key)
    pool._cb_check(key)   # still below threshold again


def test_old_failures_drop_out_of_window(pool):
    """Failures older than window_s don't count toward the threshold."""
    import time
    key = "provA:mX"
    pool.cb_window_s = 0.2
    pool._cb_record_failure(key)
    time.sleep(0.25)
    pool._cb_record_failure(key)
    # Only the recent one stays in window → still below threshold
    pool._cb_check(key)
