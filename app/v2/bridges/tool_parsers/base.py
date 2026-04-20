"""
Core interface + registry for tool-call parsers.

Design goals:
    * No parser references another parser. Each is pluggable in isolation.
    * Registry resolves ``model_name → parser`` by fnmatch pattern, with
      "most specific pattern wins" semantics (longer literal prefix =
      more specific).
    * A passthrough fallback means the system is always usable — missing
      a specialised parser degrades to "trust the provider", never errors.
    * Registration is decoupled from import: parsers can be registered
      via ``@register(match=...)`` at import time, by YAML config, or by
      ``importlib.metadata`` entry points — all three feed the same
      registry.

Public surface:
    NormalizedMessage   — dataclass returned by every parser
    ToolCallParser      — Protocol every parser implements
    ParserRegistry      — manages model→parser lookup
    get_registry()      — process-wide singleton
    register(match=...) — decorator for auto-registration
"""
from __future__ import annotations

import fnmatch
import logging
import threading
from dataclasses import dataclass, field
from typing import Callable, Protocol, runtime_checkable


logger = logging.getLogger("tudouclaw.v2.tool_parsers")


# ── normalized shape ──────────────────────────────────────────────────


@dataclass
class NormalizedMessage:
    """OpenAI-shaped assistant message.

    ``tool_calls`` list items follow the OpenAI v1 schema::

        {"id": "call_xxx",
         "type": "function",
         "function": {"name": "<tool>",
                      "arguments": "<json string>"}}

    ``role`` is always ``"assistant"`` for LLM outputs; preserved as a
    field for forward compat (judge / evaluator parsers may return
    other roles in future).
    """
    role: str = "assistant"
    content: str = ""
    tool_calls: list[dict] = field(default_factory=list)

    def to_openai_dict(self) -> dict:
        """Render as the dict shape V1/V2 bridges have historically used."""
        out: dict = {"role": self.role, "content": self.content or ""}
        if self.tool_calls:
            out["tool_calls"] = list(self.tool_calls)
        return out


# ── parser protocol ───────────────────────────────────────────────────


@runtime_checkable
class ToolCallParser(Protocol):
    """Every parser implements this.

    ``name`` is a stable identifier (used in logs and YAML config).
    ``parse`` receives the raw assistant message dict from the provider
    and returns a ``NormalizedMessage``.

    Parsers MUST be side-effect free and thread-safe (we call them from
    any TaskExecutor thread). They SHOULD be cheap to instantiate so the
    registry can cache by class.
    """

    name: str

    def parse(self, raw_message: dict) -> NormalizedMessage: ...


# ── registry ──────────────────────────────────────────────────────────


@dataclass(order=True)
class _RegistryEntry:
    # Longer literal prefix wins at matching time. Sorting keeps the
    # "most specific first" iteration order on reads.
    sort_key: tuple
    pattern: str = field(compare=False)
    parser: ToolCallParser = field(compare=False)


class ParserRegistry:
    """Maps model-name pattern → parser instance.

    Match semantics:
        * ``fnmatch`` glob patterns (``qwen*``, ``*-hermes-*``, ``llama-3.1-*``).
        * On lookup, iterate sorted by specificity (= length of longest
          literal run in the pattern). First match wins.
        * Fallback parser is injected at construction; a registry without
          any matches will always return it rather than raising.
    """

    def __init__(self, fallback: "ToolCallParser | None" = None):
        self._entries: list[_RegistryEntry] = []
        self._lock = threading.RLock()
        self._fallback: "ToolCallParser | None" = fallback

    # ── admin ──────────────────────────────────────────────────────

    def register(self, parser: ToolCallParser, match: str) -> None:
        """Register ``parser`` under glob ``match``."""
        if not hasattr(parser, "parse") or not callable(parser.parse):
            raise TypeError(f"{parser!r} is not a ToolCallParser")
        key = _specificity_key(match)
        with self._lock:
            # Replace if pattern already registered (idempotent re-register).
            self._entries = [e for e in self._entries if e.pattern != match]
            self._entries.append(
                _RegistryEntry(sort_key=key, pattern=match, parser=parser)
            )
            # Most specific first (reverse sort: larger key = more specific).
            self._entries.sort(key=lambda e: e.sort_key, reverse=True)
        logger.debug("registered tool-call parser %r for match=%r",
                     getattr(parser, "name", parser), match)

    def set_fallback(self, parser: ToolCallParser) -> None:
        with self._lock:
            self._fallback = parser

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    # ── query ──────────────────────────────────────────────────────

    def resolve(self, model: str) -> ToolCallParser:
        """Return the most specific parser matching ``model``.

        Never raises: missing matches return the fallback. Callers who
        want to know whether a specialised parser was used should inspect
        ``parser.name``.
        """
        name = (model or "").strip().lower()
        with self._lock:
            for entry in self._entries:
                if fnmatch.fnmatchcase(name, entry.pattern.lower()):
                    return entry.parser
            fb = self._fallback
        if fb is None:
            raise RuntimeError(
                "ParserRegistry has no fallback parser configured — "
                "discovery.bootstrap() must be called before resolve()."
            )
        return fb

    def list_registered(self) -> list[tuple[str, str]]:
        """For diagnostics: ``[(pattern, parser_name), ...]`` sorted by
        specificity."""
        with self._lock:
            return [(e.pattern, getattr(e.parser, "name", "?"))
                    for e in self._entries]


def _specificity_key(pattern: str) -> tuple:
    """Specificity metric.

    Intuition: patterns with more literal (non-``*``/``?``/``[``) chars
    are more specific. Ties broken by pattern length.
    """
    literal = sum(1 for c in pattern if c not in "*?[]")
    return (literal, len(pattern))


# ── singleton ─────────────────────────────────────────────────────────


_registry_singleton: "ParserRegistry | None" = None
_singleton_lock = threading.Lock()


def get_registry() -> ParserRegistry:
    """Process-wide ``ParserRegistry``. Lazily bootstrapped.

    First call triggers discovery (builtin registrations, YAML config,
    ``user_parsers/`` scan, entry-points). Later calls are O(1).
    """
    global _registry_singleton
    with _singleton_lock:
        if _registry_singleton is None:
            _registry_singleton = ParserRegistry()
            # Lazy-import to break circular dep: discovery imports this
            # module's singleton to populate it.
            from . import discovery
            discovery.bootstrap(_registry_singleton)
    return _registry_singleton


# ── decorator ─────────────────────────────────────────────────────────


# Parsers may be declared at module import time; the decorator stages
# them until the registry is bootstrapped. This lets ``user_parsers``
# drop-in modules register themselves without having to know about the
# registry singleton.
_pending: list[tuple[str, type]] = []
_pending_lock = threading.Lock()


def register(*, match: str) -> Callable[[type], type]:
    """Class decorator: register a parser for the given model-name glob.

    Usage::

        @register(match="qwen*")
        class QwenParser:
            name = "qwen"
            def parse(self, raw): ...

    Registration is deferred until the first ``get_registry()`` call,
    so decorator-registered parsers sit in-line with YAML + entry-point
    registrations and play by the same specificity rules.
    """
    def _wrap(cls: type) -> type:
        with _pending_lock:
            _pending.append((match, cls))
        return cls
    return _wrap


def _drain_pending(registry: ParserRegistry) -> None:
    """Internal: flush decorator-staged parsers into ``registry``."""
    with _pending_lock:
        batch = list(_pending)
        _pending.clear()
    for match, cls in batch:
        try:
            registry.register(cls(), match=match)
        except Exception as e:  # noqa: BLE001
            logger.warning("failed to register parser %r via decorator: %s",
                           cls.__name__, e)


__all__ = [
    "NormalizedMessage",
    "ToolCallParser",
    "ParserRegistry",
    "get_registry",
    "register",
    "_drain_pending",
]
