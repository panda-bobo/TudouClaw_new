"""P0 — mid-stream LLM abort: closes TCP in 0ms instead of waiting
for the full LLM stream to finish (was 10-30s on slow models).

User-visible problem: clicking "stop" in chat UI used to wait for
the in-flight LLM call to complete before the abort took effect.
Root cause: abort_check ran at the agent_execution iteration
boundary, AFTER chat_stream_events finished yielding events for
the current LLM call.

Fix: pass `is_aborted` through chat_stream_events →
_dispatch_stream_events → _openai_stream_events. Inside the
iter_lines loop, check abort between every chunk (~50-100ms).
On abort: resp.close() the underlying connection (kills the
TCP stream + lets the server stop generating), yield a final
{type: "aborted"} event, return.

These tests use a mock generator + a controllable abort flag to
verify:
  1. Stream completes normally when abort never fires
  2. Stream stops mid-way when abort flips True between chunks
  3. {"type": "aborted"} marker yielded on abort
  4. iter_lines loop exits without consuming the rest of the response
  5. agent_execution layer treats {"type": "aborted"} as break
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app import llm as _llm


# ── Helpers ────────────────────────────────────────────────────────

class _FakeResp:
    """Mimics requests.Response for the iter_lines streaming case."""
    def __init__(self, lines: list[bytes], status_code: int = 200):
        self._lines = lines
        self._closed = False
        self.status_code = status_code
        self.headers = {}

    def __enter__(self): return self
    def __exit__(self, *a): self.close()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise _llm.requests.HTTPError(f"{self.status_code}")

    def iter_lines(self):
        for line in self._lines:
            if self._closed:
                # Server-style: connection closed → stop
                return
            yield line

    def close(self):
        self._closed = True


def _build_sse_chunks(texts: list[str]) -> list[bytes]:
    """Build OpenAI-style SSE 'data: {json}' lines for the given text
    deltas. Ends with [DONE]."""
    import json as _json
    out = []
    for t in texts:
        chunk = {"choices": [{"delta": {"content": t}}]}
        out.append(b"data: " + _json.dumps(chunk).encode())
        out.append(b"")  # blank line between SSE events
    out.append(b"data: [DONE]")
    return out


def _make_pool(resp):
    """Patch get_connection_pool to return a fake pool that yields our
    FakeResp on .request_with_retry() / session.post()."""
    pool = MagicMock()
    pool.acquire_slot.return_value = None
    pool.release_slot.return_value = None
    session = MagicMock()
    session.post.return_value = resp
    pool.get_session.return_value = session
    # Real numbers (not MagicMock) for arithmetic comparisons
    pool.backoff_factor = 1.0
    pool.max_retries = 1
    return pool


# ── Tests ──────────────────────────────────────────────────────────

def test_no_abort_stream_completes_normally():
    """Sanity: when is_aborted never returns True, the stream yields
    every chunk + ends naturally."""
    chunks = _build_sse_chunks(["Hello", " ", "world"])
    resp = _FakeResp(chunks)
    with patch.object(_llm, "get_connection_pool",
                      return_value=_make_pool(resp)):
        events = list(_llm._openai_stream_events(
            base_url="https://api.example.com/v1",
            api_key="test",
            messages=[{"role": "user", "content": "hi"}],
            model="test",
            is_aborted=lambda: False,
        ))
    text_deltas = [e["text"] for e in events if e.get("type") == "text_delta"]
    assert "".join(text_deltas) == "Hello world"
    # No abort marker
    assert not any(e.get("type") == "aborted" for e in events)
    # Connection NOT manually closed (let context manager close on exit)
    # i.e. resp._closed True only because __exit__ ran, not because of abort
    assert resp._closed   # context manager closed it on exit


def test_abort_mid_stream_yields_aborted_marker():
    """Abort flips True after 1st chunk → loop exits, yields aborted."""
    chunks = _build_sse_chunks(["First", "Second", "Third", "Fourth"])
    resp = _FakeResp(chunks)
    abort_state = {"flag": False}

    def is_aborted():
        return abort_state["flag"]

    yielded = []
    with patch.object(_llm, "get_connection_pool",
                      return_value=_make_pool(resp)):
        gen = _llm._openai_stream_events(
            base_url="https://api.example.com/v1",
            api_key="test",
            messages=[{"role": "user", "content": "hi"}],
            model="test",
            is_aborted=is_aborted,
        )
        # Pull first chunk
        first = next(gen)
        yielded.append(first)
        # Now flip abort
        abort_state["flag"] = True
        # Pull rest — should get aborted marker, then StopIteration
        for ev in gen:
            yielded.append(ev)

    # First text delta arrived
    text_events = [e for e in yielded if e.get("type") == "text_delta"]
    assert text_events, "first chunk should have arrived"
    assert text_events[0]["text"] == "First"

    # An aborted marker was emitted
    aborted = [e for e in yielded if e.get("type") == "aborted"]
    assert len(aborted) == 1
    assert aborted[0].get("reason") == "user_interrupt"

    # Connection closed
    assert resp._closed

    # Did NOT process all chunks (abort cut short)
    assert len(text_events) < 4


def test_abort_before_first_chunk():
    """Abort already True before iter_lines yields anything → still
    yields aborted marker, doesn't read any chunks."""
    chunks = _build_sse_chunks(["A", "B", "C"])
    resp = _FakeResp(chunks)
    with patch.object(_llm, "get_connection_pool",
                      return_value=_make_pool(resp)):
        events = list(_llm._openai_stream_events(
            base_url="https://api.example.com/v1",
            api_key="test",
            messages=[{"role": "user", "content": "hi"}],
            model="test",
            is_aborted=lambda: True,
        ))
    text_events = [e for e in events if e.get("type") == "text_delta"]
    assert len(text_events) == 0
    aborted = [e for e in events if e.get("type") == "aborted"]
    assert len(aborted) == 1


def test_is_aborted_none_does_not_break():
    """When is_aborted is None (default), behaves like the old code."""
    chunks = _build_sse_chunks(["X"])
    resp = _FakeResp(chunks)
    with patch.object(_llm, "get_connection_pool",
                      return_value=_make_pool(resp)):
        events = list(_llm._openai_stream_events(
            base_url="https://api.example.com/v1",
            api_key="test",
            messages=[{"role": "user", "content": "hi"}],
            model="test",
            is_aborted=None,
        ))
    assert any(e.get("type") == "text_delta" for e in events)
    assert not any(e.get("type") == "aborted" for e in events)


def test_chat_stream_events_signature_accepts_is_aborted():
    """Public API: chat_stream_events must accept is_aborted kwarg."""
    import inspect
    sig = inspect.signature(_llm.chat_stream_events)
    assert "is_aborted" in sig.parameters


def test_dispatch_signature_accepts_is_aborted():
    """Internal dispatch must accept and forward is_aborted."""
    import inspect
    sig = inspect.signature(_llm._dispatch_stream_events)
    assert "is_aborted" in sig.parameters


# ── Integration: agent_execution wrapper handles aborted marker ──

def test_agent_execution_breaks_on_aborted_event():
    """_stream_chat_to_response should treat {"type": "aborted"} as
    end-of-stream, NOT as another text event. Verified by checking
    that text after an 'aborted' event does NOT land in the result."""
    from app.agent_execution import _stream_chat_to_response

    fake_events = [
        {"type": "text_delta", "text": "First chunk"},
        {"type": "aborted", "reason": "user_interrupt"},
        # If the wrapper kept iterating, it would see this too — should NOT
        {"type": "text_delta", "text": "POISON_should_not_appear"},
    ]

    def fake_chat_stream_events(*args, **kwargs):
        for ev in fake_events:
            yield ev

    fake_llm = MagicMock()
    fake_llm.chat_stream_events = fake_chat_stream_events
    fake_llm._postprocess_xml_tool_calls = lambda r, model="": r

    result = _stream_chat_to_response(
        fake_llm,
        messages=[{"role": "user", "content": "hi"}],
        tools=[], provider="test", model="test",
        temperature=None,
        is_aborted=lambda: False,
    )

    content = (result.get("message") or {}).get("content", "") or ""
    # First chunk landed
    assert "First chunk" in content
    # Late chunk did NOT (loop broke on aborted)
    assert "POISON_should_not_appear" not in content
