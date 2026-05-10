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
