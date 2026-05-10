"""bge-reranker-v2-m3 cross-encoder reranker wrapper."""
from __future__ import annotations
import logging

logger = logging.getLogger("tudouclaw.expert.retrieval.reranker")
_model = None
_tokenizer = None
_model_id = "BAAI/bge-reranker-v2-m3"


def _load():
    global _model, _tokenizer
    if _model is not None:
        return _model, _tokenizer
    try:
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        import torch  # noqa: F401  (verify availability)
    except ImportError as e:
        raise RuntimeError("transformers + torch required for reranker") from e
    logger.info("loading reranker %s", _model_id)
    _tokenizer = AutoTokenizer.from_pretrained(_model_id)
    _model = AutoModelForSequenceClassification.from_pretrained(_model_id)
    _model.eval()
    return _model, _tokenizer


def rerank(query: str, candidates: list[str], top_k: int | None = None) -> list[tuple[int, float]]:
    """Score (query, candidate) pairs. Returns [(orig_index, score), ...] sorted desc.

    `top_k` truncates the result. None returns all.
    """
    if not candidates:
        return []
    model, tokenizer = _load()
    import torch
    pairs = [[query, c] for c in candidates]
    with torch.no_grad():
        inputs = tokenizer(pairs, padding=True, truncation=True,
                           return_tensors="pt", max_length=512)
        scores = model(**inputs, return_dict=True).logits.view(-1).float()
    indexed = list(enumerate(scores.tolist()))
    indexed.sort(key=lambda x: x[1], reverse=True)
    if top_k is not None:
        indexed = indexed[:top_k]
    return indexed


def is_loaded() -> bool:
    return _model is not None


def unload():
    global _model, _tokenizer
    _model = None
    _tokenizer = None
