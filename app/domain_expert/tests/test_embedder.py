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
