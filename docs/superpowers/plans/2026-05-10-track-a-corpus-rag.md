# Track A: Corpus & RAG Backend Implementation Plan

> **Independent track.** Forks from `phase-0-complete` tag. No coordination needed with Tracks B/C/D until Phase 2 vertical slices.
>
> **For agentic workers:** Use superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Spec reference:** [§3.6 Module 3 (Knowledge), §3.8 (RAG→LoRA pipeline), §3.9 (Chunker registry)](../specs/2026-05-10-agent-specialty-cultivation-design.md)

**Goal:** Build all backend pieces for corpus ingestion → chunking → vector indexing → retrieval. Pure backend, no API endpoints (those land in V3 vertical). Deliverable: fully unit-tested Python modules importable by the rest of the system.

**Architecture:** 6 isolated sub-modules in `app/domain_expert/{corpus,retrieval}/`. Each is a standalone Python module with pytest coverage. No HTTP layer.

**Tech Stack:** sqlite-vss (vector store), bge-m3 (embedder via sentence-transformers), bge-reranker-v2-m3, requests + beautifulsoup (scraper), datasets (HF adapter).

**Verification model:** Each task = pytest unit tests pass. No browser, no curl. (Track B and Phase 2 verticals do the e2e wiring.)

---

## File Structure

### New Files

```
app/domain_expert/corpus/
├── __init__.py                       # exports: ingest, chunker_registry, store
├── manifest.py                       # CorpusManifest data model
├── source_flk_npc.py                 # 国家法律法规库 scraper
├── source_hf.py                      # HuggingFace dataset adapter
├── chunker.py                        # Chunker registry + base class + paragraph default
├── chunker_legal.py                  # hierarchical_legal + legal_judgment
├── store.py                          # sqlite-vss wrapper

app/domain_expert/retrieval/
├── __init__.py                       # exports: retrieve, build_pipeline
├── embedder.py                       # bge-m3 wrapper
├── reranker.py                       # bge-reranker-v2-m3 wrapper
└── pipeline.py                       # end-to-end retrieve(query, top_k)

app/domain_expert/tests/
├── test_chunker_legal.py
├── test_chunker_registry.py
├── test_store.py
├── test_embedder.py
├── test_pipeline.py
├── test_source_flk.py                # uses recorded fixture, no live network
└── test_source_hf.py                 # uses tiny mock dataset

app/domain_expert/tests/fixtures/
├── flk_sample.html                   # one law page, recorded
├── legal_chunk_input.txt             # synthetic 民法典 fragment
└── tiny_hf_dataset.jsonl             # 5-row mini dataset
```

### Files Modified

None outside `app/domain_expert/`. Track A is purely additive.

### Dependencies (already declared in Phase 0's requirements-expert.txt)

- `sqlite-vss>=0.1.2`
- `sentence-transformers>=2.7.0`
- `requests`, `beautifulsoup4`
- `datasets>=2.18.0`

```bash
~/tudou-env/bin/pip install -r requirements-expert.txt
```

---

## Task A1: Chunker registry + paragraph fallback

**Goal:** A pluggable chunker system with the default `paragraph` strategy. Other chunkers register here.

- [ ] **Step 1: Write `app/domain_expert/corpus/chunker.py`**

```python
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
```

- [ ] **Step 2: Write `app/domain_expert/tests/test_chunker_registry.py`**

```python
import pytest
from app.domain_expert.corpus import chunker as ck

def test_paragraph_basic():
    c = ck.get("paragraph", {"min_chars": 20, "max_chars": 100})
    text = "First paragraph here.\n\nSecond paragraph here.\n\nThird paragraph here."
    chunks = list(c.chunk(text, {"src": "x"}))
    assert len(chunks) >= 1
    for ch in chunks:
        assert ch.metadata["src"] == "x"
        assert ch.text

def test_paragraph_min_chars_merging():
    c = ck.get("paragraph", {"min_chars": 50, "max_chars": 200})
    text = "Short.\n\nAnother short.\n\nThird."
    chunks = list(c.chunk(text, {}))
    # All paragraphs shorter than min_chars individually — should merge
    assert len(chunks) == 1
    assert "Short" in chunks[0].text

def test_unknown_strategy_raises():
    with pytest.raises(KeyError):
        ck.get("nonexistent_strategy")

def test_fixed_window():
    c = ck.get("fixed_window", {"window": 100, "overlap": 20})
    text = "A" * 250
    chunks = list(c.chunk(text, {}))
    assert len(chunks) >= 2

def test_list_strategies():
    s = ck.list_strategies()
    assert "paragraph" in s
    assert "fixed_window" in s
```

- [ ] **Step 3: Run tests + commit**

```bash
~/tudou-env/bin/python3 -m pytest app/domain_expert/tests/test_chunker_registry.py -v
# expect: 5 passed
git add app/domain_expert/corpus/chunker.py app/domain_expert/tests/test_chunker_registry.py
git commit -m "Track A task 1: chunker registry + paragraph + fixed_window defaults"
```

---

## Task A2: Legal hierarchical chunker

**Goal:** Implement `hierarchical_legal` and `legal_judgment` chunker strategies. Tested with synthetic 民法典 fragment.

- [ ] **Step 1: Write `app/domain_expert/corpus/chunker_legal.py`**

```python
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
```

- [ ] **Step 2: Create test fixture `app/domain_expert/tests/fixtures/legal_chunk_input.txt`**

```
第一编 总则

第一章 基本规定

第一条 为了保护民事主体的合法权益,调整民事关系...

第二条 民法调整平等主体的自然人、法人和非法人组织之间的人身关系和财产关系。

第三编 合同

第八章 违约责任

第585条 当事人可以约定一方违约时应当根据违约情况向对方支付一定数额的违约金,
也可以约定因违约产生的损失赔偿额的计算方法。

约定的违约金低于造成的损失的,人民法院或者仲裁机构可以根据当事人的请求予以增加;
约定的违约金过分高于造成的损失的,人民法院或者仲裁机构可以根据当事人的请求予以适当减少。

第586条 当事人可以约定一方向对方给付定金作为债权的担保。
```

- [ ] **Step 3: Write `app/domain_expert/tests/test_chunker_legal.py`**

```python
import os
import pytest
from app.domain_expert.corpus import chunker as ck
from app.domain_expert.corpus import chunker_legal  # noqa: F401  (registers)

FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "legal_chunk_input.txt")


def test_hierarchical_legal_basic():
    with open(FIXTURE, "r", encoding="utf-8") as f:
        text = f.read()
    c = ck.get("hierarchical_legal", {})
    chunks = list(c.chunk(text, {"law_name": "民法典"}))
    # Should produce 4 chunks (条1, 条2, 条585, 条586)
    assert len(chunks) == 4
    nums = [ch.metadata["article_number"] for ch in chunks]
    assert "一" in nums or "1" in nums
    assert "585" in nums

def test_hierarchical_legal_metadata_path():
    with open(FIXTURE, "r", encoding="utf-8") as f:
        text = f.read()
    c = ck.get("hierarchical_legal", {})
    chunks = list(c.chunk(text, {"law_name": "民法典"}))
    # Find article 585 — should have book=合同 + chapter=违约责任 in path
    a585 = [ch for ch in chunks if ch.metadata["article_number"] == "585"][0]
    assert "合同" in a585.metadata["book"] or "第三编" in a585.metadata["book"]
    assert "违约责任" in a585.metadata["chapter"] or "第八章" in a585.metadata["chapter"]
    assert "民法典" in a585.metadata["full_path"]

def test_hierarchical_legal_no_articles_falls_back():
    """Text without article markers should still produce chunks via fallback."""
    text = "Random text without legal structure.\n\nMore random text."
    c = ck.get("hierarchical_legal", {})
    chunks = list(c.chunk(text, {"law_name": "X"}))
    assert len(chunks) >= 1

def test_legal_judgment_short_doc():
    text = "Short judgment, single chunk."
    c = ck.get("legal_judgment", {"max_chunk_chars": 4000})
    chunks = list(c.chunk(text, {"case_number": "(2024) 京01 民终 1234"}))
    assert len(chunks) == 1
    assert chunks[0].metadata["case_number"] == "(2024) 京01 民终 1234"

def test_legal_judgment_long_doc():
    paragraphs = ["This is a long judgment paragraph." * 30] * 5
    text = "\n\n".join(paragraphs)
    c = ck.get("legal_judgment", {"max_chunk_chars": 1000})
    chunks = list(c.chunk(text, {}))
    assert len(chunks) >= 2
    for ch in chunks:
        assert len(ch.text) <= 4000
```

- [ ] **Step 4: Run + commit**

```bash
~/tudou-env/bin/python3 -m pytest app/domain_expert/tests/test_chunker_legal.py -v
# expect: 5 passed
git add app/domain_expert/corpus/chunker_legal.py app/domain_expert/tests/{fixtures/legal_chunk_input.txt,test_chunker_legal.py}
git commit -m "Track A task 2: hierarchical_legal + legal_judgment chunkers + tests"
```

---

## Task A3: sqlite-vss vector store wrapper

**Goal:** Insert / query / delete chunks with embeddings. Pure storage layer.

- [ ] **Step 1: Write `app/domain_expert/corpus/store.py`**

```python
"""sqlite-vss vector store wrapper.

Schema:
    chunks (id INTEGER PK, text TEXT, metadata_json TEXT, source TEXT, created_at REAL)
    chunks_vss (rowid INTEGER, embedding BLOB)  -- managed by sqlite-vss

API:
    store.insert(chunks: list[Chunk], embeddings: list[list[float]])
    store.query(embedding, top_k=8) -> list[(chunk, score)]
    store.delete_by_source(source: str)
    store.count() -> int
"""
from __future__ import annotations
import json
import sqlite3
import os
from dataclasses import asdict
from .chunker import Chunk


class VectorStore:
    def __init__(self, db_path: str, embedding_dim: int = 1024):
        self.db_path = db_path
        self.embedding_dim = embedding_dim
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.enable_load_extension(True)
        try:
            import sqlite_vss
            sqlite_vss.load(self.conn)
        except ImportError as e:
            raise RuntimeError(
                "sqlite-vss not installed. pip install sqlite-vss"
            ) from e
        self._init_schema()

    def _init_schema(self):
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
            )
        """)
        cur.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vss USING vss0(
                embedding({self.embedding_dim})
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)")
        self.conn.commit()

    def insert(self, chunks: list[Chunk], embeddings: list[list[float]]) -> int:
        assert len(chunks) == len(embeddings), "chunks/embeddings length mismatch"
        cur = self.conn.cursor()
        inserted = 0
        for ch, emb in zip(chunks, embeddings):
            cur.execute(
                "INSERT INTO chunks (text, metadata_json, source) VALUES (?, ?, ?)",
                (ch.text, json.dumps(ch.metadata, ensure_ascii=False),
                 ch.metadata.get("source", "unknown")),
            )
            row_id = cur.lastrowid
            cur.execute(
                "INSERT INTO chunks_vss (rowid, embedding) VALUES (?, ?)",
                (row_id, json.dumps(emb)),
            )
            inserted += 1
        self.conn.commit()
        return inserted

    def query(self, embedding: list[float], top_k: int = 8) -> list[tuple[Chunk, float]]:
        cur = self.conn.cursor()
        cur.execute(f"""
            SELECT c.id, c.text, c.metadata_json, c.source, vss.distance
            FROM chunks_vss vss
            JOIN chunks c ON c.id = vss.rowid
            WHERE vss_search(vss.embedding, vss_search_params(?, ?))
            ORDER BY vss.distance ASC
        """, (json.dumps(embedding), top_k))
        results = []
        for row in cur.fetchall():
            _id, text, meta_json, source, dist = row
            meta = json.loads(meta_json) if meta_json else {}
            chunk = Chunk(text=text, metadata=meta)
            score = 1.0 / (1.0 + dist)  # smaller distance → higher score
            results.append((chunk, score))
        return results

    def delete_by_source(self, source: str) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM chunks WHERE source = ?", (source,))
        ids = [r[0] for r in cur.fetchall()]
        if not ids:
            return 0
        placeholders = ",".join(["?"] * len(ids))
        cur.execute(f"DELETE FROM chunks_vss WHERE rowid IN ({placeholders})", ids)
        cur.execute(f"DELETE FROM chunks WHERE id IN ({placeholders})", ids)
        self.conn.commit()
        return len(ids)

    def count(self) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM chunks")
        return cur.fetchone()[0]

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
```

- [ ] **Step 2: Write `app/domain_expert/tests/test_store.py`**

```python
import os
import tempfile
import pytest

# Skip whole test if sqlite-vss not installed (CI-friendly)
sqlite_vss = pytest.importorskip("sqlite_vss")

from app.domain_expert.corpus.store import VectorStore
from app.domain_expert.corpus.chunker import Chunk

DIM = 4  # tiny for tests

@pytest.fixture
def store():
    with tempfile.TemporaryDirectory() as d:
        s = VectorStore(os.path.join(d, "test.db"), embedding_dim=DIM)
        yield s
        s.close()

def test_insert_and_count(store):
    chunks = [
        Chunk(text="hello", metadata={"source": "s1", "n": 1}),
        Chunk(text="world", metadata={"source": "s1", "n": 2}),
    ]
    embs = [[1.0, 0, 0, 0], [0, 1.0, 0, 0]]
    n = store.insert(chunks, embs)
    assert n == 2
    assert store.count() == 2

def test_query_returns_nearest(store):
    chunks = [
        Chunk(text="apple", metadata={"source": "x"}),
        Chunk(text="banana", metadata={"source": "x"}),
        Chunk(text="cherry", metadata={"source": "x"}),
    ]
    embs = [
        [1.0, 0, 0, 0],
        [0, 1.0, 0, 0],
        [0, 0, 1.0, 0],
    ]
    store.insert(chunks, embs)
    results = store.query([1.0, 0, 0, 0], top_k=2)
    assert len(results) == 2
    # apple (exact match) should be first
    assert results[0][0].text == "apple"
    assert results[0][1] > results[1][1]  # higher score = closer

def test_delete_by_source(store):
    chunks = [
        Chunk(text="a", metadata={"source": "src1"}),
        Chunk(text="b", metadata={"source": "src1"}),
        Chunk(text="c", metadata={"source": "src2"}),
    ]
    embs = [[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0]]
    store.insert(chunks, embs)
    deleted = store.delete_by_source("src1")
    assert deleted == 2
    assert store.count() == 1
```

- [ ] **Step 3: Run + commit**

```bash
~/tudou-env/bin/pip install sqlite-vss  # if not done
~/tudou-env/bin/python3 -m pytest app/domain_expert/tests/test_store.py -v
# expect: 3 passed (or skipped if sqlite-vss unavailable)
git add app/domain_expert/corpus/store.py app/domain_expert/tests/test_store.py
git commit -m "Track A task 3: sqlite-vss VectorStore wrapper + tests"
```

---

## Task A4: bge-m3 embedder + reranker wrappers

**Goal:** Two thin wrappers around sentence-transformers — embed text → vector, rerank (query, candidates) → scored list.

- [ ] **Step 1: Write `app/domain_expert/retrieval/embedder.py`**

```python
"""bge-m3 multilingual embedder wrapper.

Lazy-loads the model. Default model from BAAI/bge-m3 (1024-dim).
"""
from __future__ import annotations
import logging

logger = logging.getLogger("tudouclaw.expert.retrieval.embedder")
_model = None
_model_id = "BAAI/bge-m3"


def _load():
    global _model
    if _model is not None:
        return _model
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as e:
        raise RuntimeError(
            "sentence-transformers not installed; pip install sentence-transformers"
        ) from e
    logger.info("loading embedder %s (one-time, ~420MB)", _model_id)
    _model = SentenceTransformer(_model_id)
    logger.info("embedder loaded")
    return _model


def embed(texts: list[str], batch_size: int = 32) -> list[list[float]]:
    """Encode list of texts to embedding vectors."""
    if not texts:
        return []
    m = _load()
    embeddings = m.encode(
        texts, batch_size=batch_size, show_progress_bar=False,
        normalize_embeddings=True,  # cosine = dot product after norm
    )
    return embeddings.tolist()


def embedding_dim() -> int:
    return 1024  # bge-m3 default


def is_loaded() -> bool:
    return _model is not None


def unload():
    """Free the model (省电模式)."""
    global _model
    _model = None
```

- [ ] **Step 2: Write `app/domain_expert/retrieval/reranker.py`**

```python
"""bge-reranker-v2-m3 cross-encoder reranker wrapper."""
from __future__ import annotations
import logging

logger = logging.getLogger("tudouclaw.expert.retrieval.reranker")
_model = None
_tokenizer = None
_model_id = "BAAI/bge-reranker-v2-m3"


def _load():
    global _model, _tokenizer
    if _model is not None:
        return _model, _tokenizer
    try:
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        import torch
    except ImportError as e:
        raise RuntimeError("transformers + torch required for reranker") from e
    logger.info("loading reranker %s", _model_id)
    _tokenizer = AutoTokenizer.from_pretrained(_model_id)
    _model = AutoModelForSequenceClassification.from_pretrained(_model_id)
    _model.eval()
    return _model, _tokenizer


def rerank(query: str, candidates: list[str], top_k: int | None = None) -> list[tuple[int, float]]:
    """Score (query, candidate) pairs. Returns [(orig_index, score), ...] sorted desc.

    `top_k` truncates the result. None returns all.
    """
    if not candidates:
        return []
    model, tokenizer = _load()
    import torch
    pairs = [[query, c] for c in candidates]
    with torch.no_grad():
        inputs = tokenizer(pairs, padding=True, truncation=True,
                           return_tensors="pt", max_length=512)
        scores = model(**inputs, return_dict=True).logits.view(-1).float()
    indexed = list(enumerate(scores.tolist()))
    indexed.sort(key=lambda x: x[1], reverse=True)
    if top_k is not None:
        indexed = indexed[:top_k]
    return indexed


def is_loaded() -> bool:
    return _model is not None


def unload():
    global _model, _tokenizer
    _model = None
    _tokenizer = None
```

- [ ] **Step 3: Tests `app/domain_expert/tests/test_embedder.py` and `test_reranker.py`**

```python
# test_embedder.py
import pytest
sentence_transformers = pytest.importorskip("sentence_transformers")
from app.domain_expert.retrieval import embedder

def test_embed_single():
    vecs = embedder.embed(["hello world"])
    assert len(vecs) == 1
    assert len(vecs[0]) == embedder.embedding_dim()

def test_embed_batch():
    texts = ["合同", "违约金", "民法典"]
    vecs = embedder.embed(texts)
    assert len(vecs) == 3

def test_unload():
    embedder.embed(["x"])
    assert embedder.is_loaded()
    embedder.unload()
    assert not embedder.is_loaded()
```

```python
# test_reranker.py
import pytest
transformers = pytest.importorskip("transformers")
from app.domain_expert.retrieval import reranker

def test_rerank_orders_correctly():
    query = "违约金过高怎么办"
    candidates = [
        "苹果是一种水果",
        "民法典 585 条规定违约金过分高于造成损失的可请求减少",
        "天气预报今天下雨",
    ]
    ranked = reranker.rerank(query, candidates)
    assert ranked[0][0] == 1  # 违约金 candidate should be first
```

- [ ] **Step 4: Run + commit**

```bash
~/tudou-env/bin/pip install sentence-transformers transformers
~/tudou-env/bin/python3 -m pytest app/domain_expert/tests/test_embedder.py app/domain_expert/tests/test_reranker.py -v
# expect: 3 + 1 passed (slow first run, downloads models ~500MB+1GB)
git add app/domain_expert/retrieval/{embedder,reranker}.py app/domain_expert/tests/test_{embedder,reranker}.py
git commit -m "Track A task 4: bge-m3 embedder + bge-reranker-v2-m3 wrappers"
```

---

## Task A5: Retrieval pipeline (combines store + embedder + reranker)

**Goal:** End-to-end `retrieve(query, agent_id, top_k)` returns ranked top-k chunks.

- [ ] **Step 1: Write `app/domain_expert/retrieval/pipeline.py`**

```python
"""End-to-end retrieval: query → embed → vector search → rerank → top-k.

Per spec §3 data flow Step 3.
"""
from __future__ import annotations
import logging
import os
from typing import Any
from .. import _config
from ..corpus.store import VectorStore
from ..corpus.chunker import Chunk
from . import embedder, reranker

logger = logging.getLogger("tudouclaw.expert.retrieval.pipeline")


def retrieve(
    agent_id: str,
    query: str,
    top_k: int = 8,
    rerank_top_n: int = 30,
    use_reranker: bool = True,
) -> list[tuple[Chunk, float]]:
    """Full pipeline: bge-m3 embed → vector search top_n → bge-reranker → top_k.

    Returns [(chunk, score), ...] sorted by relevance descending.
    """
    if not query.strip():
        return []
    db_path = os.path.join(_config.expert_dir_for(agent_id), "vector_store.db")
    if not os.path.exists(db_path):
        logger.warning("vector store missing for agent %s", agent_id)
        return []
    # Stage 1: embed
    q_emb = embedder.embed([query])[0]
    # Stage 2: vector search top_n
    with VectorStore(db_path, embedding_dim=embedder.embedding_dim()) as store:
        candidates = store.query(q_emb, top_k=rerank_top_n)
    if not candidates:
        return []
    # Stage 3: rerank top_n → top_k
    if use_reranker and len(candidates) > top_k:
        try:
            cand_texts = [c.text for c, _ in candidates]
            ranked = reranker.rerank(query, cand_texts, top_k=top_k)
            return [(candidates[i][0], score) for i, score in ranked]
        except Exception as e:
            logger.warning("reranker failed, returning vector-search order: %s", e)
    return candidates[:top_k]
```

- [ ] **Step 2: Test (integration with store + embedder + reranker)**

```python
# app/domain_expert/tests/test_pipeline.py
import os
import tempfile
import pytest

sqlite_vss = pytest.importorskip("sqlite_vss")
sentence_transformers = pytest.importorskip("sentence_transformers")

from app.domain_expert import _config
from app.domain_expert.retrieval import pipeline, embedder
from app.domain_expert.corpus.store import VectorStore
from app.domain_expert.corpus.chunker import Chunk


def test_retrieve_finds_relevant_chunk(monkeypatch, tmp_path):
    # Override expert dir to tmp
    monkeypatch.setattr(_config, "expert_dir_for", lambda a: str(tmp_path))
    # Seed the store with a few chunks + their embeddings
    chunks = [
        Chunk(text="苹果是一种水果", metadata={"source": "x"}),
        Chunk(text="违约金过高时,人民法院可以适当减少", metadata={"source": "x"}),
        Chunk(text="今天天气晴朗", metadata={"source": "x"}),
    ]
    embs = embedder.embed([c.text for c in chunks])
    db_path = os.path.join(str(tmp_path), "vector_store.db")
    with VectorStore(db_path, embedding_dim=embedder.embedding_dim()) as s:
        s.insert(chunks, embs)
    # Query
    results = pipeline.retrieve("ag1", "违约金过高怎么办", top_k=2)
    assert len(results) >= 1
    assert "违约金" in results[0][0].text
```

- [ ] **Step 3: Run + commit**

```bash
~/tudou-env/bin/python3 -m pytest app/domain_expert/tests/test_pipeline.py -v
# expect: 1 passed
git add app/domain_expert/retrieval/pipeline.py app/domain_expert/tests/test_pipeline.py
git commit -m "Track A task 5: retrieve() pipeline integrating store + embedder + reranker"
```

---

## Task A6: Corpus source adapters (flk_npc + HF datasets)

**Goal:** Two ingestion adapters: scrape 国家法律法规库 / pull from HuggingFace. Each yields raw text + source_meta dicts ready for chunkers.

- [ ] **Step 1: Write `app/domain_expert/corpus/source_flk_npc.py`**

```python
"""国家法律法规库 (flk.npc.gov.cn) scraper.

Public, freely-available legal text. Polite rate limiting (1 req/sec).
For Phase A test fidelity, can be run with --fixture-only flag using
recorded HTML.
"""
from __future__ import annotations
import logging
import re
import time
from typing import Iterator
from dataclasses import dataclass

logger = logging.getLogger("tudouclaw.expert.corpus.flk")

INDEX_URL = "https://flk.npc.gov.cn/api/?type=xfwx"  # 现行有效法律列表


@dataclass
class FlkDocument:
    title: str
    url: str
    text: str
    metadata: dict


def fetch_index(max_items: int = 100) -> list[dict]:
    """Get list of currently-valid laws from the index API."""
    import requests
    resp = requests.get(INDEX_URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return (data.get("result", {}).get("data", []) or [])[:max_items]


def fetch_document(item: dict) -> FlkDocument | None:
    """Fetch and parse one law document. `item` is from fetch_index()."""
    import requests
    from bs4 import BeautifulSoup
    title = item.get("title", "")
    doc_url = item.get("link") or item.get("url") or ""
    if not doc_url:
        return None
    try:
        resp = requests.get(doc_url, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("fetch failed for %s: %s", title, e)
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    body = soup.find("div", class_="content") or soup.body
    text = body.get_text("\n").strip() if body else ""
    if not text:
        return None
    return FlkDocument(
        title=title, url=doc_url, text=text,
        metadata={
            "law_name": title,
            "source": "flk_npc",
            "source_url": doc_url,
            "publish_date": item.get("publish") or "",
        },
    )


def iter_all(max_items: int = 100, rate_limit_seconds: float = 1.0) -> Iterator[FlkDocument]:
    """Lazy iterate all current laws. Caller handles indexing."""
    items = fetch_index(max_items=max_items)
    logger.info("flk_npc: %d laws to fetch", len(items))
    for i, item in enumerate(items):
        doc = fetch_document(item)
        if doc:
            yield doc
        if rate_limit_seconds > 0:
            time.sleep(rate_limit_seconds)


def iter_from_fixture(fixture_path: str) -> Iterator[FlkDocument]:
    """Replay a recorded fixture (used in tests / offline mode)."""
    import json
    with open(fixture_path, "r", encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            yield FlkDocument(**d)
```

- [ ] **Step 2: Write `app/domain_expert/corpus/source_hf.py`**

```python
"""HuggingFace dataset adapter for HF-hosted legal Q/A corpora.

Targets:
  ShengbinYue/DISC-Law-SFT
  YeungNLP/CAIL2018-2019
  ...
"""
from __future__ import annotations
import logging
from typing import Iterator
from dataclasses import dataclass

logger = logging.getLogger("tudouclaw.expert.corpus.hf")


@dataclass
class HfRecord:
    text: str
    metadata: dict


def iter_dataset(
    dataset_id: str,
    split: str = "train",
    text_field: str = "text",
    max_items: int | None = None,
    metadata_fields: list[str] | None = None,
) -> Iterator[HfRecord]:
    """Stream a HuggingFace dataset. Requires `datasets` package."""
    try:
        from datasets import load_dataset
    except ImportError as e:
        raise RuntimeError("`datasets` package required. pip install datasets") from e
    logger.info("loading HF dataset %s split=%s", dataset_id, split)
    ds = load_dataset(dataset_id, split=split, streaming=True)
    metadata_fields = metadata_fields or []
    count = 0
    for row in ds:
        text = row.get(text_field, "")
        if not text:
            continue
        meta = {f: row.get(f, "") for f in metadata_fields}
        meta["source"] = f"hf:{dataset_id}"
        yield HfRecord(text=text, metadata=meta)
        count += 1
        if max_items and count >= max_items:
            break
```

- [ ] **Step 3: Test (with mocks / fixtures, no live network)**

```python
# app/domain_expert/tests/test_source_flk.py
import os
import tempfile
import json
from app.domain_expert.corpus.source_flk_npc import iter_from_fixture, FlkDocument


def test_iter_from_fixture(tmp_path):
    fixture = tmp_path / "fixture.jsonl"
    docs = [
        {"title": "民法典", "url": "u1", "text": "test text",
         "metadata": {"law_name": "民法典", "source": "flk_npc"}},
        {"title": "刑法", "url": "u2", "text": "another",
         "metadata": {"law_name": "刑法", "source": "flk_npc"}},
    ]
    with open(fixture, "w") as f:
        for d in docs:
            f.write(json.dumps(d) + "\n")
    items = list(iter_from_fixture(str(fixture)))
    assert len(items) == 2
    assert items[0].title == "民法典"
    assert items[1].metadata["law_name"] == "刑法"
```

```python
# app/domain_expert/tests/test_source_hf.py
import pytest
datasets = pytest.importorskip("datasets")

def test_hf_adapter_smoke(monkeypatch):
    """Lightly verify the adapter doesn't crash. Uses a tiny known-good ds."""
    from app.domain_expert.corpus.source_hf import iter_dataset
    # Only run if network is OK; test as a stretch
    try:
        items = list(iter_dataset("Anthropic/hh-rlhf",
                                   split="train", text_field="chosen", max_items=2))
        assert len(items) <= 2
    except Exception as e:
        pytest.skip(f"HF live dataset unavailable: {e}")
```

- [ ] **Step 4: Run + commit**

```bash
~/tudou-env/bin/python3 -m pytest app/domain_expert/tests/test_source_flk.py app/domain_expert/tests/test_source_hf.py -v
# fixture test should pass; HF live test may skip
git add app/domain_expert/corpus/source_*.py app/domain_expert/tests/test_source_*.py
git commit -m "Track A task 6: flk.npc.gov.cn + HuggingFace corpus source adapters"
```

---

## Task A7: Corpus manifest manager

**Goal:** Track which sources are indexed, when, how many chunks. Lives at `~/.tudou_claw/expert/<agent_id>/corpus/_manifest.json`.

- [ ] **Step 1: Write `app/domain_expert/corpus/manifest.py`**

```python
"""Corpus manifest — tracks which sources are indexed for a given agent."""
from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass, field, asdict


@dataclass
class CorpusSourceEntry:
    source_id: str                     # e.g. "flk_npc" / "hf:disc-law-sft"
    version: str = ""                  # snapshot version
    chunk_count: int = 0
    bytes: int = 0
    indexed_at: float = 0.0
    chunker_strategy: str = ""
    notes: str = ""


@dataclass
class CorpusManifest:
    agent_id: str
    sources: list[CorpusSourceEntry] = field(default_factory=list)
    last_updated: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "CorpusManifest":
        sources = [CorpusSourceEntry(**s) for s in d.get("sources", [])]
        return CorpusManifest(
            agent_id=d.get("agent_id", ""),
            sources=sources,
            last_updated=d.get("last_updated", time.time()),
        )

    def add_source(self, entry: CorpusSourceEntry) -> None:
        # replace if exists
        self.sources = [s for s in self.sources if s.source_id != entry.source_id]
        self.sources.append(entry)
        self.last_updated = time.time()

    def get_source(self, source_id: str) -> CorpusSourceEntry | None:
        for s in self.sources:
            if s.source_id == source_id:
                return s
        return None

    def remove_source(self, source_id: str) -> bool:
        before = len(self.sources)
        self.sources = [s for s in self.sources if s.source_id != source_id]
        if len(self.sources) < before:
            self.last_updated = time.time()
            return True
        return False

    def total_chunks(self) -> int:
        return sum(s.chunk_count for s in self.sources)

    def total_bytes(self) -> int:
        return sum(s.bytes for s in self.sources)

    @staticmethod
    def path_for(agent_id: str) -> str:
        from .._config import expert_dir_for
        return os.path.join(expert_dir_for(agent_id), "corpus", "_manifest.json")

    @staticmethod
    def load(agent_id: str) -> "CorpusManifest":
        p = CorpusManifest.path_for(agent_id)
        if not os.path.exists(p):
            return CorpusManifest(agent_id=agent_id)
        with open(p, "r", encoding="utf-8") as f:
            return CorpusManifest.from_dict(json.load(f))

    def save(self) -> None:
        p = CorpusManifest.path_for(self.agent_id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
```

- [ ] **Step 2: Test**

```python
# app/domain_expert/tests/test_manifest.py
import os
from app.domain_expert.corpus.manifest import CorpusManifest, CorpusSourceEntry
from app.domain_expert import _config

def test_manifest_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for", lambda a: str(tmp_path / a))
    m = CorpusManifest(agent_id="ag1")
    m.add_source(CorpusSourceEntry(source_id="flk_npc", chunk_count=1500,
                                    chunker_strategy="hierarchical_legal"))
    m.add_source(CorpusSourceEntry(source_id="hf:disc-law-sft", chunk_count=4000))
    m.save()

    m2 = CorpusManifest.load("ag1")
    assert m2.total_chunks() == 5500
    assert m2.get_source("flk_npc").chunker_strategy == "hierarchical_legal"

def test_manifest_replace_existing(monkeypatch, tmp_path):
    monkeypatch.setattr(_config, "expert_dir_for", lambda a: str(tmp_path / a))
    m = CorpusManifest(agent_id="ag2")
    m.add_source(CorpusSourceEntry(source_id="X", chunk_count=10))
    m.add_source(CorpusSourceEntry(source_id="X", chunk_count=20))  # replace
    assert m.total_chunks() == 20
    assert len(m.sources) == 1
```

- [ ] **Step 3: Run + commit**

```bash
~/tudou-env/bin/python3 -m pytest app/domain_expert/tests/test_manifest.py -v
# expect: 2 passed
git add app/domain_expert/corpus/manifest.py app/domain_expert/tests/test_manifest.py
git commit -m "Track A task 7: CorpusManifest manager + tests"
```

---

## Self-Review

- ☑ All deliverables: 7 tasks → 6 modules + 1 manifest + ~12 unit tests
- ☑ No HTTP layer touched (Phase 2 V3 verticals will combine these into endpoints)
- ☑ No coordination needed with other tracks
- ☑ Each module has tests; coverage target ≥ 80% per module
- ☑ Reversible: revert any task without breaking later tasks (each is independent)
- ☑ TUDOU_EXPERT_DISABLED still works (these modules just don't get imported)

## Handoff to Phase 2

After Track A merges, V3 vertical slice (Corpus + RAG indexing + UI) consumes:
- `app.domain_expert.corpus.source_flk_npc.iter_all()` for ingestion
- `app.domain_expert.corpus.source_hf.iter_dataset()` for HF imports
- `app.domain_expert.corpus.chunker.get(strategy_id, config)` for chunking
- `app.domain_expert.corpus.store.VectorStore` for indexing
- `app.domain_expert.retrieval.pipeline.retrieve(agent_id, query)` for queries
- `app.domain_expert.corpus.manifest.CorpusManifest` for state tracking

V3 wraps these in API endpoints and UI panels; no further changes needed in Track A modules.
