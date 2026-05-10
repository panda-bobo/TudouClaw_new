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
