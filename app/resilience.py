"""通用重试 / 熔断装饰器。

来源
----
Port 自 RAG-Anything (Apache-2.0,
https://github.com/HKUDS/RAG-Anything) 的 ``raganything/resilience.py``,
保持 API 兼容,仅本地化注释与日志格式;原作者解决的 GitHub issue #172
"process_document_complete getting stuck due to intermittent network errors"
跟 TudouClaw 的 RAG / shadow / embedding 路径痛点一致。

用在哪
------
适合任何"网络抖动 → 抛瞬态异常 → 重试一两次大概率就好"的场景:

  * ``rag_provider._ingest_remote`` 远端 ingest
  * ``shadow`` 重扫触发的 embedding 调用
  * ``tudou_chromadb_mcp`` 远端 chroma 调用
  * 自定义 skill 里调外部 API

不适合:LLM provider 调用(``llm.py`` 已有 provider-specific 的 400/超时
特判,套这个装饰器会和那一层冲突)。

依赖检测
--------
``httpx`` / ``openai`` 在 import 时探测,装了就把它们的瞬态异常加入默认
重试集合,没装则只重试 stdlib 的 ``ConnectionError`` / ``TimeoutError``。

使用
----
::

    from app.resilience import retry, async_retry, CircuitBreaker

    @retry(max_attempts=4, base_delay=0.5)
    def ingest_one(doc): ...

    @async_retry(max_attempts=3)
    async def fetch(url): ...

    breaker = CircuitBreaker(failure_threshold=5, name="remote-rag")
    @breaker
    def call_remote(): ...
"""

from __future__ import annotations

import asyncio
import functools
import logging
import threading
import time
from typing import Any, Callable, Optional, Sequence, Type, TypeVar

logger = logging.getLogger("tudou.resilience")

F = TypeVar("F", bound=Callable[..., Any])


# 默认可重试的异常 — 故意只覆盖网络/上游问题。
# 本地编程错误 (TypeError / ValueError / KeyError ...) 和大多数 OSError
# 子类 (FileNotFoundError / PermissionError ...) 不该被默认重试。
_DEFAULT_RETRYABLE: tuple[Type[BaseException], ...] = (
    ConnectionError,
    TimeoutError,
)

try:
    import httpx
    _DEFAULT_RETRYABLE = _DEFAULT_RETRYABLE + (
        httpx.ConnectError,
        httpx.ReadTimeout,
        httpx.WriteTimeout,
        httpx.PoolTimeout,
    )
except ImportError:
    pass

try:
    import openai
    _DEFAULT_RETRYABLE = _DEFAULT_RETRYABLE + (
        openai.APIConnectionError,
        openai.APITimeoutError,
        openai.RateLimitError,
        openai.InternalServerError,
    )
except ImportError:
    pass

try:
    # ``requests`` is a hard dep of TudouClaw (see requirements.txt).
    # ``RequestException`` is the parent of ConnectionError / Timeout /
    # ChunkedEncodingError / etc — the transient network failures we
    # want to retry. ``HTTPError`` (4xx/5xx response with raise_for_status)
    # is also a subclass and IS retryable for 5xx/429 — we leave that
    # decision to callers via raise_for_status placement.
    import requests as _requests
    _DEFAULT_RETRYABLE = _DEFAULT_RETRYABLE + (
        _requests.ConnectionError,
        _requests.Timeout,
    )
except ImportError:
    pass


# ──────────────────────────────────────────────────────────────────────
# Retry decorators
# ──────────────────────────────────────────────────────────────────────


def retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    retryable_exceptions: Optional[Sequence[Type[BaseException]]] = None,
    on_retry: Optional[Callable[[BaseException, int, float], None]] = None,
) -> Callable[[F], F]:
    """同步函数的重试装饰器,指数退避 + 可选 jitter。

    Args:
        max_attempts: 包含首次调用在内的总尝试次数。
        base_delay: 第一次重试前的基础等待秒数。
        max_delay: 单次等待时间上限(避免指数膨胀到天文数字)。
        exponential_base: 每次重试等待时间的乘子。
        jitter: 开启则在等待时间上叠加 0-50% 随机抖动,避免羊群效应。
        retryable_exceptions: 触发重试的异常集合。默认见模块文档。
        on_retry: 每次重试睡眠前的回调,签名 ``(exc, attempt, delay)``。
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if base_delay < 0 or max_delay < 0:
        raise ValueError("base_delay and max_delay must be >= 0")
    if exponential_base <= 0:
        raise ValueError("exponential_base must be > 0")

    if retryable_exceptions is None:
        retryable_exceptions = _DEFAULT_RETRYABLE

    def decorator(func: F) -> F:
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return func(*args, **kwargs)
                except tuple(retryable_exceptions) as exc:
                    last_exc = exc
                    if attempt == max_attempts:
                        logger.error(
                            "%s failed after %d attempts: %s",
                            func.__qualname__, max_attempts, exc,
                        )
                        raise
                    delay = min(
                        base_delay * (exponential_base ** (attempt - 1)),
                        max_delay,
                    )
                    if jitter:
                        import random
                        delay *= 1.0 + random.uniform(0, 0.5)
                    if on_retry is not None:
                        on_retry(exc, attempt, delay)
                    logger.warning(
                        "%s attempt %d/%d failed (%s), retrying in %.1fs",
                        func.__qualname__, attempt, max_attempts,
                        type(exc).__name__, delay,
                    )
                    time.sleep(delay)
            raise last_exc  # type: ignore[misc]

        return wrapper  # type: ignore[return-value]

    return decorator


def async_retry(
    max_attempts: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 60.0,
    exponential_base: float = 2.0,
    jitter: bool = True,
    retryable_exceptions: Optional[Sequence[Type[BaseException]]] = None,
    on_retry: Optional[Callable[[BaseException, int, float], Any]] = None,
) -> Callable[[F], F]:
    """异步函数的重试装饰器,与 :func:`retry` 行为一致,使用 ``asyncio.sleep``。

    ``on_retry`` 可以是协程,会被自动 ``await``。
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")
    if base_delay < 0 or max_delay < 0:
        raise ValueError("base_delay and max_delay must be >= 0")
    if exponential_base <= 0:
        raise ValueError("exponential_base must be > 0")

    if retryable_exceptions is None:
        retryable_exceptions = _DEFAULT_RETRYABLE

    def decorator(func: F) -> F:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return await func(*args, **kwargs)
                except tuple(retryable_exceptions) as exc:
                    last_exc = exc
                    if attempt == max_attempts:
                        logger.error(
                            "%s failed after %d attempts: %s",
                            func.__qualname__, max_attempts, exc,
                        )
                        raise
                    delay = min(
                        base_delay * (exponential_base ** (attempt - 1)),
                        max_delay,
                    )
                    if jitter:
                        import random
                        delay *= 1.0 + random.uniform(0, 0.5)
                    if on_retry is not None:
                        result = on_retry(exc, attempt, delay)
                        if asyncio.iscoroutine(result):
                            await result
                    logger.warning(
                        "%s attempt %d/%d failed (%s), retrying in %.1fs",
                        func.__qualname__, attempt, max_attempts,
                        type(exc).__name__, delay,
                    )
                    await asyncio.sleep(delay)
            raise last_exc  # type: ignore[misc]

        return wrapper  # type: ignore[return-value]

    return decorator


# ──────────────────────────────────────────────────────────────────────
# Circuit breaker
# ──────────────────────────────────────────────────────────────────────


class CircuitBreaker:
    """简单的熔断器,防止级联失败。

    在 ``reset_timeout`` 窗口内连续失败超过 ``failure_threshold`` 次后
    *打开* 熔断器,后续调用直接抛 ``CircuitBreakerOpen`` 不再执行被保护
    函数;``reset_timeout`` 秒后进入 *半开* 态,允许一次试探调用通过。

    Args:
        failure_threshold: 连续失败多少次后打开。
        reset_timeout: 打开后等待多少秒再切到半开。
        name: 日志里的人类可读名字。
        failure_exceptions: 哪些异常算"上游失败",默认与默认重试集合一致
            (避免本地代码 bug 把熔断器顶开)。
    """

    class CircuitBreakerOpen(Exception):
        """熔断器已打开时抛出。"""

    def __init__(
        self,
        failure_threshold: int = 5,
        reset_timeout: float = 60.0,
        name: str = "default",
        failure_exceptions: Optional[Sequence[Type[BaseException]]] = None,
    ) -> None:
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.name = name
        self._failure_exceptions: tuple[Type[BaseException], ...] = tuple(
            failure_exceptions or _DEFAULT_RETRYABLE
        )

        self._failure_count = 0
        self._last_failure_time: float = 0.0
        self._state: str = "closed"  # closed | open | half-open
        self._lock = threading.Lock()
        self._trial_in_flight: bool = False

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == "open":
                if time.time() - self._last_failure_time >= self.reset_timeout:
                    self._state = "half-open"
            return self._state

    def record_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            self._state = "closed"
            self._trial_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            now = time.time()
            if self._state == "half-open":
                # 半开态下的探测请求失败 → 立即重新打开。
                self._failure_count = self.failure_threshold
            else:
                # 只统计窗口内的失败,陈旧失败不计入下一次突发。
                if (
                    self._last_failure_time
                    and now - self._last_failure_time >= self.reset_timeout
                ):
                    self._failure_count = 0
                self._failure_count += 1
            self._last_failure_time = now
            if self._failure_count >= self.failure_threshold:
                self._state = "open"
                self._trial_in_flight = False
                logger.warning(
                    "Circuit breaker '%s' opened after %d failures",
                    self.name, self._failure_count,
                )

    def _acquire_permission(self) -> None:
        """执行被保护调用前先拿许可。

          - open 且超时未到 → 抛 CircuitBreakerOpen
          - open 且超时已到 → 切到 half-open
          - half-open + 已有 in-flight 探测 → 抛(单飞)
          - half-open + 无 in-flight → 标记 in-flight 放行
          - closed → 放行
        """
        with self._lock:
            if self._state == "open":
                if time.time() - self._last_failure_time >= self.reset_timeout:
                    self._state = "half-open"

            if self._state == "open":
                raise self.CircuitBreakerOpen(
                    f"Circuit breaker '{self.name}' is open — call rejected"
                )

            if self._state == "half-open":
                if self._trial_in_flight:
                    raise self.CircuitBreakerOpen(
                        f"Circuit breaker '{self.name}' is half-open — "
                        "trial in progress"
                    )
                self._trial_in_flight = True
                return

            return  # closed

    def __call__(self, func: F) -> F:
        """同步函数装饰器。"""
        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self._acquire_permission()
            try:
                result = func(*args, **kwargs)
                self.record_success()
                return result
            except tuple(self._failure_exceptions):
                self.record_failure()
                raise
            except Exception:
                # 本地代码 bug 不顶开熔断器,但要清掉 half-open 标记防止永久卡死。
                with self._lock:
                    if self._state == "half-open":
                        self._trial_in_flight = False
                raise

        return wrapper  # type: ignore[return-value]

    def async_call(self, func: F) -> F:
        """异步函数装饰器。"""
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            self._acquire_permission()
            try:
                result = await func(*args, **kwargs)
                self.record_success()
                return result
            except tuple(self._failure_exceptions):
                self.record_failure()
                raise
            except Exception:
                with self._lock:
                    if self._state == "half-open":
                        self._trial_in_flight = False
                raise

        return wrapper  # type: ignore[return-value]


__all__ = [
    "retry",
    "async_retry",
    "CircuitBreaker",
]
