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
