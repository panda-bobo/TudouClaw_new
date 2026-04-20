"""
Parser discovery + registry bootstrap.

Three layered sources are consulted in order. Each layer's registrations
go through the same ``ParserRegistry.register`` API, so specificity
rules decide the winning parser — not declaration order.

    1. Built-in classes               (``builtin.BUILTIN_CLASSES``)
    2. YAML config                    (``config/tool_parsers.yaml``)
    3. User drop-in modules           (``user_parsers/*.py``)
    4. Python entry points            (``tudouclaw.tool_parsers``)

YAML is the recommended path for "this model uses an existing format".
Drop-in / entry-point is for "this model needs a new format".
"""
from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import pkgutil
from typing import Iterable

from .base import ParserRegistry, _drain_pending
from . import builtin as _builtin


logger = logging.getLogger("tudouclaw.v2.tool_parsers")


_CONFIG_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "config", "tool_parsers.yaml",
)


def bootstrap(registry: ParserRegistry) -> None:
    """Populate ``registry`` from every configured source. Idempotent."""
    # Fallback first so downstream bugs can't strand callers.
    registry.set_fallback(_builtin.OpenAIPassthroughParser())

    _load_builtins_from_yaml(registry)
    _load_user_parsers(registry)
    _load_entry_points(registry)
    _drain_pending(registry)


# ── 1. YAML config ────────────────────────────────────────────────────


def _load_builtins_from_yaml(registry: ParserRegistry) -> None:
    """Read ``config/tool_parsers.yaml`` and register each entry.

    Schema::

        parsers:
          - match: "qwen*"
            class: XMLTagJSONParser
            config: {open_tag: "<tool_call>", close_tag: "</tool_call>"}
          - match: "*-hermes-*"
            class: XMLTagJSONParser
          - match: "glm-4*"
            class: JSONOnlyParser
            config: {name_key: "tool", args_key: "args"}

    Missing file = silent no-op. Bad entries log a warning but don't
    abort — one broken line shouldn't crash the whole registry.
    """
    cfg_path = os.path.normpath(_CONFIG_PATH)
    if not os.path.exists(cfg_path):
        logger.debug("no tool-parser YAML at %s — skipping", cfg_path)
        return
    try:
        import yaml  # type: ignore
    except ImportError:
        logger.warning("PyYAML not installed; skipping tool_parsers.yaml")
        return
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:  # noqa: BLE001
        logger.warning("failed to load %s: %s", cfg_path, e)
        return

    for entry in (data.get("parsers") or []):
        if not isinstance(entry, dict):
            continue
        match = entry.get("match")
        cls_name = entry.get("class")
        cfg = entry.get("config") or {}
        if not match or not cls_name:
            continue
        cls = _builtin.BUILTIN_CLASSES.get(cls_name)
        if cls is None:
            logger.warning("unknown parser class %r in %s", cls_name, cfg_path)
            continue
        try:
            parser = cls(**cfg) if cfg else cls()
            registry.register(parser, match=match)
        except Exception as e:  # noqa: BLE001
            logger.warning("bad parser entry match=%r class=%r: %s",
                           match, cls_name, e)


# ── 2. User drop-in modules ───────────────────────────────────────────


def _load_user_parsers(registry: ParserRegistry) -> None:
    """Import every ``.py`` under ``tool_parsers/user_parsers/``.

    Modules register themselves via the ``@register(match=...)``
    decorator; ``_drain_pending`` in ``bootstrap`` flushes them into the
    registry after all imports are done. This two-phase approach means a
    single user module can register multiple parsers and the ordering of
    imports doesn't affect specificity matching.
    """
    pkg_name = "app.v2.bridges.tool_parsers.user_parsers"
    try:
        pkg = importlib.import_module(pkg_name)
    except ImportError:
        logger.debug("no user_parsers package — skipping drop-in scan")
        return

    pkg_path = getattr(pkg, "__path__", None)
    if not pkg_path:
        return

    for info in pkgutil.iter_modules(pkg_path):
        full_name = f"{pkg_name}.{info.name}"
        try:
            importlib.import_module(full_name)
        except Exception as e:  # noqa: BLE001
            logger.warning("failed to import user parser %r: %s", full_name, e)


# ── 3. Entry-point plugins (third-party packages) ────────────────────


def _load_entry_points(registry: ParserRegistry) -> None:
    """Load parsers advertised via the ``tudouclaw.tool_parsers``
    entry-point group.

    Each entry point's target must be callable and return a
    ``(parser_instance, match_pattern)`` tuple, or a list of such tuples.
    """
    try:
        from importlib.metadata import entry_points
    except ImportError:
        return

    try:
        eps = entry_points()
        group = getattr(eps, "select", None)
        candidates = (
            group(group="tudouclaw.tool_parsers")
            if group else eps.get("tudouclaw.tool_parsers", [])
        )
    except Exception as e:  # noqa: BLE001
        logger.debug("entry_points lookup failed: %s", e)
        return

    for ep in candidates:
        try:
            factory = ep.load()
            result = factory() if callable(factory) else factory
            if isinstance(result, tuple) and len(result) == 2:
                parser, pattern = result
                registry.register(parser, match=pattern)
            elif isinstance(result, list):
                for parser, pattern in result:
                    registry.register(parser, match=pattern)
            else:
                logger.warning("entry point %r returned unexpected shape", ep.name)
        except Exception as e:  # noqa: BLE001
            logger.warning("entry point %r failed: %s", ep.name, e)


__all__ = ["bootstrap"]
