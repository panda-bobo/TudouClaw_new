"""R5 — build_typed_rag_block groups retrieved chunks by metadata.type."""
from __future__ import annotations

from app.domain_expert.inference.pipeline import (
    TYPE_ORDER,
    TYPE_TITLES,
    build_typed_rag_block,
)


def _chunk(source_id: str, text: str, type: str | None = None) -> dict:
    """Shape that matches what _retrieve returns."""
    meta: dict = {"source_id": source_id}
    if type is not None:
        meta["type"] = type
    return {
        "source_id": source_id,
        "text": text,
        "score": 1.0,
        "metadata": meta,
    }


# ── Empty / trivial ──

def test_empty_chunks_returns_empty_string():
    assert build_typed_rag_block([]) == ""


def test_single_chunk_no_type_uses_reference_section():
    out = build_typed_rag_block([_chunk("s1", "some text")])
    assert TYPE_TITLES["reference"] in out
    assert "[reference · s1]" in out
    assert "some text" in out


# ── Header / footer ──

def test_header_includes_specialty_when_set():
    out = build_typed_rag_block([_chunk("s1", "x")], specialty="legal")
    assert "legal 专家知识库检索" in out
    assert "(共 1 段)" in out


def test_header_drops_specialty_when_blank():
    out = build_typed_rag_block([_chunk("s1", "x")])
    assert "专家知识库检索" not in out
    assert "检索到 1 段资料" in out


def test_footer_reminds_to_cite():
    out = build_typed_rag_block([_chunk("s1", "x")])
    assert "[来源: source_id]" in out


# ── Grouping ──

def test_groups_by_type_one_section_per_type():
    chunks = [
        _chunk("law1", "L1", type="law"),
        _chunk("law2", "L2", type="law"),
        _chunk("sop1", "S1", type="sop"),
    ]
    out = build_typed_rag_block(chunks, specialty="legal")
    # Exactly two section headers, with the right counts
    assert "📋 参考工作流程 (1)" in out
    assert "📖 法条参考 (2)" in out
    # Each chunk's source_id appears once with the right type prefix
    assert "[law · law1]" in out
    assert "[law · law2]" in out
    assert "[sop · sop1]" in out


def test_canonical_section_order_red_line_first_reference_last():
    """When multiple types are present, red_line section renders
    before sop, sop before law, ..., reference last."""
    chunks = [
        _chunk("ref1", "ref", type="reference"),
        _chunk("law1", "law", type="law"),
        _chunk("rl1", "redline", type="red_line"),
        _chunk("sop1", "sop", type="sop"),
    ]
    out = build_typed_rag_block(chunks, specialty="legal")
    pos_red = out.index(TYPE_TITLES["red_line"])
    pos_sop = out.index(TYPE_TITLES["sop"])
    pos_law = out.index(TYPE_TITLES["law"])
    pos_ref = out.index(TYPE_TITLES["reference"])
    assert pos_red < pos_sop < pos_law < pos_ref


def test_unknown_type_appended_at_end_uses_uppercase_label():
    chunks = [
        _chunk("rl1", "rl", type="red_line"),
        _chunk("custom1", "custom", type="risk"),
    ]
    out = build_typed_rag_block(chunks, specialty="legal")
    pos_red = out.index(TYPE_TITLES["red_line"])
    pos_custom = out.index("RISK")
    assert pos_red < pos_custom
    assert "[risk · custom1]" in out


def test_chunks_without_metadata_treated_as_reference():
    """Defensive: if a chunk lacks `metadata` entirely, default type."""
    chunks = [{"source_id": "s1", "text": "t", "score": 1.0}]
    out = build_typed_rag_block(chunks)
    assert TYPE_TITLES["reference"] in out
    assert "[reference · s1]" in out


def test_total_count_matches_input_count():
    chunks = [_chunk(f"s{i}", "x", type="law") for i in range(7)]
    out = build_typed_rag_block(chunks, specialty="legal")
    assert "(共 7 段)" in out
    assert "📖 法条参考 (7)" in out


def test_type_order_constant_covers_every_known_type_title():
    """Sanity: TYPE_ORDER and TYPE_TITLES stay in lockstep so unknown
    types fall through to the "append at end" path consistently."""
    assert set(TYPE_ORDER) == set(TYPE_TITLES.keys())
