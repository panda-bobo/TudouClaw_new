"""
V2 observability — structured logging + counters.

All V2 code that wants to log should use ``get_logger(name)``, which
returns a ``LoggerAdapter`` bound to a per-call ``extra`` dict
(task_id / agent_id / phase / retry_attempt). The adapter serialises
the extras as JSON appended to the log line, so downstream log
aggregation (ELK / Loki / Datadog) can index them as structured fields
without requiring a JSON-format handler upstream.

Counters are in-process only (thread-safe dict of ints). They exist so
the REST layer can expose ``/api/v2/metrics`` for health dashboards
without pulling in Prometheus. If metrics get serious, swap the
implementation for ``prometheus_client`` — the ``record`` API stays.
"""
from __future__ import annotations

import json
import logging
import threading
from typing import Any


_counters: dict[str, int] = {}
_counters_lock = threading.Lock()


class _V2Adapter(logging.LoggerAdapter):
    """LoggerAdapter that appends extras as `` …{json}`` on every record."""

    def process(self, msg, kwargs):
        extra = dict(self.extra or {})
        # Merge caller-supplied extras into the base.
        extra.update(kwargs.pop("extra", None) or {})
        if extra:
            try:
                msg = f"{msg} {json.dumps(extra, ensure_ascii=False, default=str)}"
            except Exception:
                msg = f"{msg} {extra!r}"
        return msg, kwargs


def get_logger(name: str, **extra: Any) -> logging.LoggerAdapter:
    """Return an adapter that embeds ``extra`` in every log line."""
    base = logging.getLogger(f"tudouclaw.v2.{name}")
    return _V2Adapter(base, extra)


def record(counter: str, delta: int = 1) -> None:
    """Increment an in-process counter. Thread-safe."""
    if not counter:
        return
    with _counters_lock:
        _counters[counter] = _counters.get(counter, 0) + int(delta)


def snapshot() -> dict[str, int]:
    """Copy of all counters. Safe to serialise as JSON."""
    with _counters_lock:
        return dict(_counters)


__all__ = ["get_logger", "record", "snapshot"]
