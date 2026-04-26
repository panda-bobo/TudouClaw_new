"""app.resilience — retry / async_retry / CircuitBreaker.

Covers:
- retry recovers from transient failures, gives up on persistent ones
- non-retryable errors propagate immediately (no wasted attempts)
- jitter actually disperses delays
- async_retry mirrors sync semantics
- CircuitBreaker open → half-open → closed transitions
- CircuitBreaker single-flight in half-open state
"""
from __future__ import annotations

import asyncio
import time
from unittest import mock

import pytest

from app.resilience import retry, async_retry, CircuitBreaker


# ── retry (sync) ─────────────────────────────────────────────────────


def test_retry_recovers_from_transient():
    calls = {"n": 0}

    @retry(max_attempts=4, base_delay=0.001, jitter=False)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("boom")
        return "OK"

    assert flaky() == "OK"
    assert calls["n"] == 3


def test_retry_non_retryable_propagates_without_retry():
    calls = {"n": 0}

    @retry(max_attempts=4, base_delay=0.001)
    def buggy():
        calls["n"] += 1
        raise ValueError("local bug")

    with pytest.raises(ValueError):
        buggy()
    assert calls["n"] == 1, "ValueError should not retry"


def test_retry_exhaustion_reraises_last_exception():
    calls = {"n": 0}

    @retry(max_attempts=3, base_delay=0.001, jitter=False)
    def hopeless():
        calls["n"] += 1
        raise TimeoutError("still down")

    with pytest.raises(TimeoutError, match="still down"):
        hopeless()
    assert calls["n"] == 3


def test_retry_validates_args():
    with pytest.raises(ValueError):
        retry(max_attempts=0)
    with pytest.raises(ValueError):
        retry(base_delay=-1)
    with pytest.raises(ValueError):
        retry(exponential_base=0)


def test_retry_custom_retryable_exceptions():
    calls = {"n": 0}

    @retry(max_attempts=3, base_delay=0.001,
           retryable_exceptions=(KeyError,))
    def picky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise KeyError("k")
        return "ok"

    assert picky() == "ok"
    assert calls["n"] == 2

    # ConnectionError NOT in custom set → no retry
    calls["n"] = 0

    @retry(max_attempts=3, base_delay=0.001,
           retryable_exceptions=(KeyError,))
    def wrong_kind():
        calls["n"] += 1
        raise ConnectionError("bypassed")

    with pytest.raises(ConnectionError):
        wrong_kind()
    assert calls["n"] == 1


def test_retry_on_retry_callback():
    seen = []

    def cb(exc, attempt, delay):
        seen.append((type(exc).__name__, attempt, round(delay, 3)))

    calls = {"n": 0}

    @retry(max_attempts=3, base_delay=0.5, exponential_base=2.0,
           jitter=False, on_retry=cb)
    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("x")
        return "ok"

    with mock.patch("time.sleep"):
        flaky()
    # Two retry callbacks fired (attempts 1 and 2; success on attempt 3)
    assert len(seen) == 2
    assert seen[0] == ("ConnectionError", 1, 0.5)   # 0.5 * 2**0
    assert seen[1] == ("ConnectionError", 2, 1.0)   # 0.5 * 2**1


def test_retry_jitter_inflates_delay():
    """When jitter on, sleep(d) sees d in [base, 1.5*base] range."""
    sleeps: list[float] = []

    @retry(max_attempts=3, base_delay=1.0, exponential_base=1.0,
           jitter=True)
    def flaky():
        raise ConnectionError("x")

    with mock.patch("time.sleep", side_effect=sleeps.append):
        with pytest.raises(ConnectionError):
            flaky()
    # Two retries → 2 sleeps, each in [1.0, 1.5]
    assert len(sleeps) == 2
    for s in sleeps:
        assert 1.0 <= s <= 1.5


# ── async_retry ──────────────────────────────────────────────────────


def test_async_retry_recovers():
    calls = {"n": 0}

    @async_retry(max_attempts=3, base_delay=0.001, jitter=False)
    async def aflaky():
        calls["n"] += 1
        if calls["n"] < 2:
            raise ConnectionError("flaky")
        return "OK_A"

    assert asyncio.run(aflaky()) == "OK_A"
    assert calls["n"] == 2


def test_async_retry_exhaustion_reraises():
    @async_retry(max_attempts=2, base_delay=0.001, jitter=False)
    async def doomed():
        raise TimeoutError("nope")

    with pytest.raises(TimeoutError):
        asyncio.run(doomed())


def test_async_retry_awaits_coroutine_callback():
    seen = []

    async def cb(exc, attempt, delay):
        seen.append(("async-cb", attempt))

    calls = {"n": 0}

    @async_retry(max_attempts=3, base_delay=0.001, jitter=False, on_retry=cb)
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("x")
        return "ok"

    asyncio.run(flaky())
    assert seen == [("async-cb", 1), ("async-cb", 2)]


# ── CircuitBreaker ───────────────────────────────────────────────────


def test_breaker_opens_after_threshold():
    br = CircuitBreaker(failure_threshold=2, reset_timeout=10, name="t1")

    @br
    def fail():
        raise ConnectionError("down")

    for _ in range(2):
        with pytest.raises(ConnectionError):
            fail()
    assert br.state == "open"

    # Subsequent call rejected without invocation
    with pytest.raises(CircuitBreaker.CircuitBreakerOpen):
        fail()


def test_breaker_half_open_after_reset_then_closed_on_success():
    br = CircuitBreaker(failure_threshold=2, reset_timeout=0.1, name="t2")

    @br
    def maybe_fail(should_fail: bool):
        if should_fail:
            raise ConnectionError("x")
        return "ok"

    for _ in range(2):
        with pytest.raises(ConnectionError):
            maybe_fail(True)
    assert br.state == "open"

    time.sleep(0.12)  # > reset_timeout
    # State property transitions to half-open
    assert br.state == "half-open"

    # Successful trial call → closes the breaker
    assert maybe_fail(False) == "ok"
    assert br.state == "closed"


def test_breaker_half_open_failure_reopens():
    br = CircuitBreaker(failure_threshold=2, reset_timeout=0.1, name="t3")

    @br
    def fail():
        raise ConnectionError("x")

    for _ in range(2):
        with pytest.raises(ConnectionError):
            fail()
    time.sleep(0.12)
    assert br.state == "half-open"

    # Trial fails → snaps back to open with full failure count
    with pytest.raises(ConnectionError):
        fail()
    assert br.state == "open"


def test_breaker_half_open_single_flight():
    """In half-open, only ONE trial call is allowed; concurrent calls reject."""
    br = CircuitBreaker(failure_threshold=1, reset_timeout=0.05, name="t4")

    @br
    def fail():
        raise ConnectionError("x")

    with pytest.raises(ConnectionError):
        fail()
    assert br.state == "open"
    time.sleep(0.06)

    # First peek-at-state transitions to half-open and reserves the trial
    # by passing _acquire_permission. We simulate this by calling the
    # decorated function via a wrapper that holds permission but doesn't
    # complete — easiest is to manually drive state.
    br._acquire_permission()  # this marks _trial_in_flight=True
    assert br.state == "half-open"

    # Second call MUST be rejected (trial in flight)
    with pytest.raises(CircuitBreaker.CircuitBreakerOpen):
        br._acquire_permission()


def test_breaker_local_bug_does_not_open_breaker():
    """Application bugs (ValueError) shouldn't count as upstream failure."""
    br = CircuitBreaker(failure_threshold=2, reset_timeout=10, name="t5")

    @br
    def buggy():
        raise ValueError("local")

    for _ in range(5):
        with pytest.raises(ValueError):
            buggy()
    assert br.state == "closed"
