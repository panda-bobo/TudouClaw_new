"""Chunker registry + base class + paragraph fallback.

Per spec §3.9, each specialty declares its chunker strategy in YAML.
This module provides the abstract base + registration mechanism + default.
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class Chunk:
    """One indexable unit produced by a chunker."""
    text: str
    metadata: dict = field(default_factory=dict)


class Chunker(ABC):
    """Base for all chunker strategies."""

    @abstractmethod
    def chunk(self, text: str, source_meta: dict) -> Iterator[Chunk]:
        """Yield Chunks for `text`. `source_meta` is per-document context
        (file path, source_id, etc.) the chunker may merge into chunk.metadata."""
        ...


# ── Registry ──
_REGISTRY: dict[str, type[Chunker]] = {}


def register(strategy_id: str):
    """Decorator: @register('paragraph') class ParagraphChunker(Chunker): ..."""
    def deco(cls):
        if strategy_id in _REGISTRY:
            raise ValueError(f"chunker {strategy_id!r} already registered")
        _REGISTRY[strategy_id] = cls
        return cls
    return deco


def get(strategy_id: str, config: dict | None = None) -> Chunker:
    if strategy_id not in _REGISTRY:
        raise KeyError(f"unknown chunker strategy {strategy_id!r}; "
                       f"registered: {sorted(_REGISTRY.keys())}")
    return _REGISTRY[strategy_id](**(config or {}))


def list_strategies() -> list[str]:
    return sorted(_REGISTRY.keys())


# ── Built-in: paragraph (default fallback) ──
@register("paragraph")
class ParagraphChunker(Chunker):
    """Split on blank-line boundaries, then merge to target size."""

    def __init__(self, min_chars: int = 80, max_chars: int = 800):
        self.min_chars = min_chars
        self.max_chars = max_chars

    def chunk(self, text: str, source_meta: dict) -> Iterator[Chunk]:
        if not text.strip():
            return
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        buf = ""
        for p in paragraphs:
            if len(buf) + len(p) + 2 <= self.max_chars:
                buf = (buf + "\n\n" + p) if buf else p
            else:
                if len(buf) >= self.min_chars:
                    yield Chunk(text=buf, metadata=dict(source_meta))
                    buf = p
                else:
                    # too-small chunk: keep accumulating even past max
                    buf = (buf + "\n\n" + p) if buf else p
        if buf:
            yield Chunk(text=buf, metadata=dict(source_meta))


@register("fixed_window")
class FixedWindowChunker(Chunker):
    """Sliding-window character chunks. Brute-force fallback for messy text."""

    def __init__(self, window: int = 500, overlap: int = 50):
        self.window = window
        self.overlap = overlap

    def chunk(self, text: str, source_meta: dict) -> Iterator[Chunk]:
        if not text:
            return
        step = max(1, self.window - self.overlap)
        for i in range(0, len(text), step):
            chunk_text = text[i : i + self.window]
            if len(chunk_text) < 50:  # skip tiny tail
                continue
            yield Chunk(text=chunk_text, metadata=dict(source_meta))
