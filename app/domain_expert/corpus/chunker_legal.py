"""Legal-specialty chunkers.

hierarchical_legal: Split legal codes by 编/章/节/条/款/项 hierarchy.
legal_judgment: Split judgment documents by paragraph + case metadata.

Per spec §3.9.
"""
from __future__ import annotations
import re
from typing import Iterator
from .chunker import Chunker, Chunk, register


# Regex patterns for legal hierarchy markers.
# Matches Chinese-style numbering: 第一编 / 第二章 / 第三节 / 第585条 / etc.
_BOOK_RE     = re.compile(r"^\s*第[一二三四五六七八九十百千]+编\s+(\S.*)$", re.MULTILINE)
_CHAPTER_RE  = re.compile(r"^\s*第[一二三四五六七八九十百千]+章\s+(\S.*)$", re.MULTILINE)
_SECTION_RE  = re.compile(r"^\s*第[一二三四五六七八九十百千]+节\s+(\S.*)$", re.MULTILINE)
_ARTICLE_RE  = re.compile(r"^\s*第([一二三四五六七八九十百千零\d]+)条\s*", re.MULTILINE)


@register("hierarchical_legal")
class HierarchicalLegalChunker(Chunker):
    """Article-level chunks with full 编/章/节 path in metadata.

    Each output Chunk = one 法条 (article). Metadata carries
    book / chapter / section / article_number for retrieval reranking.
    """

    def __init__(
        self,
        min_chunk_chars: int = 80,
        max_chunk_chars: int = 800,
        primary_unit: str = "article",
        keep_metadata: list[str] | None = None,
    ):
        self.min_chunk_chars = min_chunk_chars
        self.max_chunk_chars = max_chunk_chars
        self.primary_unit = primary_unit
        self.keep_metadata = keep_metadata or [
            "law_name", "book", "chapter", "section", "article_number",
        ]

    def chunk(self, text: str, source_meta: dict) -> Iterator[Chunk]:
        if not text.strip():
            return
        # Pass 1: walk text linearly, tracking current book/chapter/section.
        current = {
            "law_name": source_meta.get("law_name", ""),
            "book": "",
            "chapter": "",
            "section": "",
        }
        # Find article boundaries
        article_starts = [(m.start(), m.group(1)) for m in _ARTICLE_RE.finditer(text)]
        if not article_starts:
            # No article markers — fall back to paragraph chunking
            from .chunker import ParagraphChunker
            yield from ParagraphChunker(
                min_chars=self.min_chunk_chars,
                max_chars=self.max_chunk_chars * 2,
            ).chunk(text, source_meta)
            return
        # Sentinel
        article_starts.append((len(text), ""))
        # For each article, determine its preceding book/chapter/section
        for i in range(len(article_starts) - 1):
            start, article_num = article_starts[i]
            end = article_starts[i + 1][0]
            article_text = text[start:end].strip()
            # Look back from `start` to find the most recent book/chapter/section
            preamble = text[:start]
            book_m = list(_BOOK_RE.finditer(preamble))
            chapter_m = list(_CHAPTER_RE.finditer(preamble))
            section_m = list(_SECTION_RE.finditer(preamble))
            if book_m:    current["book"] = book_m[-1].group(0).strip()
            if chapter_m: current["chapter"] = chapter_m[-1].group(0).strip()
            if section_m: current["section"] = section_m[-1].group(0).strip()
            metadata = dict(source_meta)
            metadata.update({
                "law_name": current["law_name"],
                "book": current["book"],
                "chapter": current["chapter"],
                "section": current["section"],
                "article_number": article_num,
                "full_path": " · ".join(filter(None, [
                    current["law_name"],
                    current["book"],
                    current["chapter"],
                    current["section"],
                    f"第{article_num}条",
                ])),
            })
            yield Chunk(text=article_text, metadata=metadata)


@register("legal_judgment")
class LegalJudgmentChunker(Chunker):
    """Judgment documents — DON'T split mid-document. One judgment ≈ one chunk.

    For very long judgments (> max_chunk_chars), split at paragraph boundaries
    but keep ≥ min_chunk_chars.
    """

    def __init__(
        self,
        min_chunk_chars: int = 800,
        max_chunk_chars: int = 4000,
        keep_metadata: list[str] | None = None,
    ):
        self.min_chunk_chars = min_chunk_chars
        self.max_chunk_chars = max_chunk_chars
        self.keep_metadata = keep_metadata or [
            "court_level", "case_number", "case_type", "decision_year", "parties",
        ]

    def chunk(self, text: str, source_meta: dict) -> Iterator[Chunk]:
        if not text.strip():
            return
        if len(text) <= self.max_chunk_chars:
            yield Chunk(text=text.strip(), metadata=dict(source_meta))
            return
        # Long judgment — split on paragraphs but accumulate aggressively
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        buf = ""
        for p in paragraphs:
            if len(buf) + len(p) + 2 <= self.max_chunk_chars:
                buf = (buf + "\n\n" + p) if buf else p
            else:
                if len(buf) >= self.min_chunk_chars:
                    yield Chunk(text=buf, metadata=dict(source_meta))
                    buf = p
                else:
                    buf = (buf + "\n\n" + p) if buf else p
        if buf:
            yield Chunk(text=buf, metadata=dict(source_meta))
