import os
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
