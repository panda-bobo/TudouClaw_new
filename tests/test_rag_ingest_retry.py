"""app.rag_provider._ingest_remote — @retry wire-in.

Goal: transient network failures get retried; persistent 4xx don't burn
extra requests; sustained outages still return 0 (caller contract).
"""
from __future__ import annotations

from unittest import mock

import pytest
import requests

from app.rag_provider import RAGProviderRegistry


class _StubProvider:
    """Minimal duck-type for RAGProviderEntry: only the fields _ingest_remote reads."""
    name = "test-rag"
    base_url = "http://rag.test"
    id = "test"


def _build_registry():
    """Create a RAGProviderRegistry without going through __init__."""
    reg = RAGProviderRegistry.__new__(RAGProviderRegistry)
    reg._remote_headers = lambda p: {"X-Test": "1"}  # noqa: ARG005
    return reg


@pytest.fixture(autouse=True)
def _patch_sleep():
    """All retry tests must run instantly — patch time.sleep globally."""
    with mock.patch("time.sleep"):
        yield


# ── 200 → no retry ────────────────────────────────────────────────────


def test_200_returns_count_first_attempt():
    reg = _build_registry()
    docs = [{"id": "1", "title": "t", "content": "c"}]

    fake = mock.MagicMock()
    fake.status_code = 200
    fake.json.return_value = {"count": len(docs)}

    with mock.patch("requests.post", return_value=fake) as posted:
        n = reg._ingest_remote(_StubProvider(), "col", docs)

    assert n == 1
    assert posted.call_count == 1


# ── 5xx → retry → succeed ─────────────────────────────────────────────


def test_5xx_retries_until_200():
    reg = _build_registry()
    docs = [{"id": "1", "title": "t", "content": "c"}]

    calls = {"n": 0}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls["n"] += 1
        r = mock.MagicMock()
        if calls["n"] < 3:
            r.status_code = 503  # service unavailable
        else:
            r.status_code = 200
            r.json.return_value = {"count": 1}
        return r

    with mock.patch("requests.post", side_effect=fake_post):
        n = reg._ingest_remote(_StubProvider(), "col", docs)

    assert n == 1
    assert calls["n"] == 3, "expected 2 retries before success"


# ── 4xx → no retry ────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_4xx_no_retry(status):
    reg = _build_registry()
    docs = [{"id": "1", "title": "t", "content": "c"}]

    fake = mock.MagicMock()
    fake.status_code = status

    with mock.patch("requests.post", return_value=fake) as posted:
        n = reg._ingest_remote(_StubProvider(), "col", docs)

    assert n == 0
    assert posted.call_count == 1, f"4xx={status} should not retry"


# ── network errors → retry → exhaust → return 0 ──────────────────────


def test_connection_error_retries_then_returns_zero():
    reg = _build_registry()
    docs = [{"id": "1"}]

    with mock.patch(
        "requests.post",
        side_effect=requests.ConnectionError("econnrefused"),
    ) as posted:
        n = reg._ingest_remote(_StubProvider(), "col", docs)

    assert n == 0
    # 3 attempts total (initial + 2 retries) per @retry(max_attempts=3)
    assert posted.call_count == 3


def test_timeout_retries_then_returns_zero():
    reg = _build_registry()
    docs = [{"id": "1"}]

    with mock.patch(
        "requests.post",
        side_effect=requests.Timeout("read timed out"),
    ) as posted:
        n = reg._ingest_remote(_StubProvider(), "col", docs)

    assert n == 0
    assert posted.call_count == 3


def test_local_bug_in_response_handler_does_not_loop():
    """A local exception (KeyError parsing JSON, etc.) is NOT retryable.

    The retry decorator's retryable_exceptions is RequestException-only;
    other exceptions surface to the outer try/except in _ingest_remote
    and return 0 silently after exactly one attempt.
    """
    reg = _build_registry()
    docs = [{"id": "1"}]

    fake = mock.MagicMock()
    fake.status_code = 200
    fake.json.side_effect = KeyError("malformed payload")

    with mock.patch("requests.post", return_value=fake) as posted:
        n = reg._ingest_remote(_StubProvider(), "col", docs)

    assert n == 0
    assert posted.call_count == 1, "local KeyError must not retry"
