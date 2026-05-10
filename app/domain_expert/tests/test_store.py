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
