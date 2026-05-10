"""End-to-end retrieval: query → embed → vector search → rerank → top-k.

Per spec §3 data flow Step 3.
"""
from __future__ import annotations
import logging
import os
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
