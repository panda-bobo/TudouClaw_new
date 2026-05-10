"""R3 — kb_seed_loader.ingest_seeds_into_agent_kb writes typed chunks
to the per-agent KB."""
from __future__ import annotations

import json
import os

import pytest

from app.domain_expert import _config
from app.domain_expert.corpus.manifest import CorpusManifest
from app.domain_expert.kb_seed_loader import (
    SEED_SOURCE_PREFIX,
    _seed_source_id,
    ingest_seeds_into_agent_kb,
    resolve_seed_path,
)
from app.domain_expert.template import KBSeed, SpecialtyTemplate


# ── Fixtures ──

@pytest.fixture
def expert_root(tmp_path, monkeypatch):
    """Redirect ~/.tudou_claw/expert/<id>/ to a tmp dir for isolation."""
    root = tmp_path / "expert"
    root.mkdir()
    monkeypatch.setattr(_config, "expert_root", lambda: str(root))
    yield root


@pytest.fixture
def seeds_dir(tmp_path):
    """A `seeds/` dir we'll point the loader at via template_dir override."""
    base = tmp_path / "tpls"
    seeds = base / "seeds"
    seeds.mkdir(parents=True)
    return base, seeds


def _make_template(kb_seeds: list[dict] | None = None) -> SpecialtyTemplate:
    return SpecialtyTemplate.from_dict({
        "id": "civil-law-expert",
        "version": "1.0",
        "name": "民法专家",
        "specialty": "civil_law",
        "kb_seeds": kb_seeds or [],
    })


# ── _seed_source_id + resolve_seed_path ──

def test_seed_source_id_strips_dirs_and_extension():
    assert _seed_source_id(KBSeed(file="legal/civil_code.md")) == "seed_civil_code"


def test_seed_source_id_replaces_unsafe_chars():
    assert _seed_source_id(KBSeed(file="weird name (v2).txt")) == "seed_weird_name__v2_"


def test_seed_source_id_always_starts_with_prefix():
    assert _seed_source_id(KBSeed(file="x.md")).startswith(SEED_SOURCE_PREFIX)


def test_resolve_seed_path_absolute_passthrough(tmp_path):
    abspath = str(tmp_path / "anywhere.md")
    assert resolve_seed_path(abspath) == abspath


def test_resolve_seed_path_relative_uses_seeds_subdir(seeds_dir):
    base, _ = seeds_dir
    p = resolve_seed_path("legal/civil_code.md", template_dir=str(base))
    assert p == os.path.join(str(base), "seeds", "legal/civil_code.md")


def test_resolve_seed_path_empty_raises():
    with pytest.raises(ValueError):
        resolve_seed_path("")


# ── ingest_seeds_into_agent_kb happy path ──

def test_ingest_writes_chunks_jsonl_with_type_metadata(expert_root, seeds_dir):
    base, seeds = seeds_dir
    # Write a seed file with two paragraphs
    (seeds / "civil_code.md").write_text(
        "第1条 公民享有民事权利。" * 10
        + "\n\n"
        + "第2条 法人享有民事权利。" * 10,
        encoding="utf-8",
    )
    tpl = _make_template([
        {"file": "civil_code.md", "type": "law", "title": "民法典节选"},
    ])
    entries = ingest_seeds_into_agent_kb("ag1", tpl, template_dir=str(base))

    # Manifest entry returned + persisted
    assert len(entries) == 1
    assert entries[0].source_id == "seed_civil_code"
    assert entries[0].chunk_count >= 1
    assert entries[0].chunker_strategy == "paragraph"
    assert entries[0].version == "1.0"
    # Source ID stored on disk
    chunks_jsonl = (
        expert_root / "ag1" / "corpus" / "seed_civil_code" / "chunks.jsonl"
    )
    assert chunks_jsonl.is_file()
    lines = chunks_jsonl.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == entries[0].chunk_count
    # Each chunk carries metadata.type so R5 typed RAG can group
    rec0 = json.loads(lines[0])
    assert rec0["text"]
    assert rec0["metadata"]["type"] == "law"
    assert rec0["metadata"]["title"] == "民法典节选"
    assert rec0["metadata"]["source_id"] == "seed_civil_code"
    assert rec0["metadata"]["from_template"] == "civil-law-expert"
    assert rec0["metadata"]["template_version"] == "1.0"


def test_ingest_persists_manifest(expert_root, seeds_dir):
    base, seeds = seeds_dir
    (seeds / "sop.md").write_text("步骤一: 收集证据。\n\n步骤二: 整理时间线。",
                                  encoding="utf-8")
    tpl = _make_template([
        {"file": "sop.md", "type": "sop", "title": "诉讼准备 SOP"},
    ])
    ingest_seeds_into_agent_kb("ag2", tpl, template_dir=str(base))

    manifest = CorpusManifest.load("ag2")
    sources = {s.source_id: s for s in manifest.sources}
    assert "seed_sop" in sources
    assert sources["seed_sop"].notes.startswith("seeded from civil-law-expert")


def test_ingest_handles_multiple_seeds(expert_root, seeds_dir):
    base, seeds = seeds_dir
    (seeds / "law.md").write_text("第1条 ... " * 30, encoding="utf-8")
    (seeds / "sop.md").write_text("步骤 ... " * 30, encoding="utf-8")
    (seeds / "rules.md").write_text("禁止 ... " * 30, encoding="utf-8")
    tpl = _make_template([
        {"file": "law.md", "type": "law"},
        {"file": "sop.md", "type": "sop"},
        {"file": "rules.md", "type": "red_line"},
    ])
    entries = ingest_seeds_into_agent_kb("ag3", tpl, template_dir=str(base))
    assert {e.source_id for e in entries} == {"seed_law", "seed_sop", "seed_rules"}


# ── ingest_seeds_into_agent_kb edge cases ──

def test_ingest_no_seeds_returns_empty(expert_root):
    entries = ingest_seeds_into_agent_kb("ag1", _make_template([]))
    assert entries == []


def test_ingest_none_template_returns_empty(expert_root):
    assert ingest_seeds_into_agent_kb("ag1", None) == []


def test_ingest_missing_seed_file_skipped_silently(expert_root, seeds_dir):
    """A template referencing a non-existent file should not crash —
    other seeds (if any) should still ingest."""
    base, seeds = seeds_dir
    (seeds / "exists.md").write_text("有内容。" * 30, encoding="utf-8")
    tpl = _make_template([
        {"file": "exists.md", "type": "law"},
        {"file": "ghost.md", "type": "sop"},  # not on disk
    ])
    entries = ingest_seeds_into_agent_kb("ag1", tpl, template_dir=str(base))
    sids = [e.source_id for e in entries]
    assert "seed_exists" in sids
    assert "seed_ghost" not in sids


def test_ingest_empty_seed_file_skipped(expert_root, seeds_dir):
    base, seeds = seeds_dir
    (seeds / "blank.md").write_text("   \n\n  \n", encoding="utf-8")
    tpl = _make_template([{"file": "blank.md", "type": "reference"}])
    assert ingest_seeds_into_agent_kb("ag1", tpl, template_dir=str(base)) == []


def test_ingest_re_run_overwrites_seed_source(expert_root, seeds_dir):
    """Re-running the loader after a seed file is updated should
    re-chunk and update the manifest in-place (idempotent)."""
    base, seeds = seeds_dir
    seed_path = seeds / "code.md"
    # One paragraph → one chunk
    seed_path.write_text("old content " * 30, encoding="utf-8")
    tpl = _make_template([{"file": "code.md", "type": "law"}])
    e1 = ingest_seeds_into_agent_kb("ag1", tpl, template_dir=str(base))
    old_count = e1[0].chunk_count
    old_bytes = e1[0].bytes

    # Update the seed with many paragraphs → multiple chunks
    long_paragraphs = "\n\n".join(
        f"段落{i}: " + "新内容。" * 100 for i in range(20)
    )
    seed_path.write_text(long_paragraphs, encoding="utf-8")
    e2 = ingest_seeds_into_agent_kb("ag1", tpl, template_dir=str(base))
    assert e2[0].source_id == e1[0].source_id
    assert e2[0].chunk_count > old_count
    assert e2[0].bytes != old_bytes

    # Manifest still has only one entry for this source
    manifest = CorpusManifest.load("ag1")
    seed_entries = [s for s in manifest.sources if s.source_id == "seed_code"]
    assert len(seed_entries) == 1


def test_ingest_uses_basename_as_default_title(expert_root, seeds_dir):
    base, seeds = seeds_dir
    (seeds / "untitled.md").write_text("内容。" * 30, encoding="utf-8")
    tpl = _make_template([{"file": "untitled.md", "type": "law"}])
    ingest_seeds_into_agent_kb("ag1", tpl, template_dir=str(base))

    chunks_jsonl = (
        _config.expert_dir_for("ag1")
    )
    line = open(os.path.join(chunks_jsonl, "corpus", "seed_untitled",
                             "chunks.jsonl"), encoding="utf-8").readline()
    rec = json.loads(line)
    assert rec["metadata"]["title"] == "untitled.md"


def test_ingest_requires_agent_id(expert_root):
    with pytest.raises(ValueError):
        ingest_seeds_into_agent_kb("", _make_template([
            {"file": "x.md", "type": "law"},
        ]))
