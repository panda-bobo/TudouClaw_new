"""Per-(client, path) rate limiter.

Defensive: a frontend bug or a runaway integration can hammer a single
endpoint hundreds of times a second. This middleware caps each
(client_ip, path) pair to a sliding-window rate. Excess requests get
HTTP 429.

Static files and SSE streams are exempt.

Pure ASGI to stay compatible with the existing SecurityHeaders /
GZip pipeline (BaseHTTPMiddleware buffers SSE responses — we don't).
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import deque

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger("tudouclaw.ratelimit")


class RateLimitMiddleware:
    """Sliding-window per-(ip, path) limiter.

    Defaults: 10 requests / 5 seconds per (ip, path). Override via env:
      TUDOU_RATELIMIT_MAX     — int, requests allowed per window (default 10)
      TUDOU_RATELIMIT_WINDOW  — float, window seconds (default 5.0)
      TUDOU_RATELIMIT_OFF     — set to "1" to disable entirely

    Exempts paths starting with: /static, /favicon, and any path
    containing "/stream" (SSE) or "/sse".
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        # key = (ip, path) → deque[timestamp]
        self._buckets: dict[tuple[str, str], deque[float]] = {}
        # Rough cap on bucket dict size to prevent memory growth.
        self._max_buckets = 2048
        # Recent 429-emit log to avoid spamming the same warning.
        self._last_warned: dict[tuple[str, str], float] = {}
        # Env-only kill switch (the "I broke production, need to disable
        # NOW without HTTP" escape). All tunables otherwise come from the
        # SystemSettingsStore so admin can change them via Settings UI
        # at runtime.
        self._env_off = os.environ.get("TUDOU_RATELIMIT_OFF", "0") == "1"

    def _live_settings(self) -> tuple[bool, int, float]:
        """Read (enabled, max_requests, window_s) from the store on every
        request. Cheap — one dict.get per call. Lets admin tune from the
        Settings UI without restart. Falls back to factory defaults if
        the store isn't initialized yet (e.g. very early startup)."""
        try:
            from ...system_settings import get_store, DEFAULTS
        except Exception:
            return True, 10, 5.0
        store = get_store()
        if store is None:
            d = DEFAULTS["rate_limit"]
            return bool(d["enabled"]), int(d["max_requests"]), float(d["window_seconds"])
        return (
            bool(store.get("rate_limit.enabled", True)),
            int(store.get("rate_limit.max_requests", 10)),
            float(store.get("rate_limit.window_seconds", 5.0)),
        )

    @staticmethod
    def _exempt(path: str) -> bool:
        if path.startswith("/static/") or path.startswith("/favicon"):
            return True
        if "/stream" in path or "/sse" in path:
            return True
        return False

    def _client_ip(self, scope: Scope) -> str:
        # Honor X-Forwarded-For if present (single-process dev usually doesn't).
        for k, v in scope.get("headers") or []:
            if k == b"x-forwarded-for":
                return v.decode("latin-1").split(",")[0].strip()
        client = scope.get("client") or ("unknown", 0)
        return str(client[0])

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if self._env_off or scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Live read so admin Settings UI changes take effect on next request.
        enabled, max_requests, window_s = self._live_settings()
        if not enabled:
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if self._exempt(path):
            await self.app(scope, receive, send)
            return

        ip = self._client_ip(scope)
        key = (ip, path)
        now = time.monotonic()

        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = deque()
            self._buckets[key] = bucket
            # GC old buckets if we hit the cap.
            if len(self._buckets) > self._max_buckets:
                cutoff = now - window_s * 4
                stale = [k for k, b in self._buckets.items() if not b or b[-1] < cutoff]
                for k in stale[: self._max_buckets // 4]:
                    self._buckets.pop(k, None)

        # Drop timestamps outside the window.
        cutoff = now - window_s
        while bucket and bucket[0] < cutoff:
            bucket.popleft()

        if len(bucket) >= max_requests:
            # Throttle — 429 with a short Retry-After hint.
            retry = max(1.0, window_s - (now - bucket[0]))
            # Log at most once every 10s per key to avoid noise.
            if (now - self._last_warned.get(key, 0)) > 10:
                logger.warning(
                    "ratelimit: %s %s — %d req in %.1fs window (cap=%d)",
                    ip, path, len(bucket), window_s, max_requests,
                )
                self._last_warned[key] = now

            body = json.dumps({
                "error": "rate_limited",
                "detail": (
                    f"Too many requests to {path} from {ip}: "
                    f"{len(bucket)} in last {window_s}s "
                    f"(cap={max_requests}). Retry in ~{retry:.1f}s."
                ),
                "retry_after_s": round(retry, 1),
            }).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 429,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"retry-after", str(int(retry) or 1).encode()),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        bucket.append(now)
        await self.app(scope, receive, send)
