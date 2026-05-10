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
