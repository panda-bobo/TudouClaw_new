import pytest
transformers = pytest.importorskip("transformers")
torch = pytest.importorskip("torch")
from app.domain_expert.retrieval import reranker


def test_rerank_orders_correctly():
    query = "违约金过高怎么办"
    candidates = [
        "苹果是一种水果",
        "民法典 585 条规定违约金过分高于造成损失的可请求减少",
        "天气预报今天下雨",
    ]
    ranked = reranker.rerank(query, candidates)
    assert ranked[0][0] == 1  # 违约金 candidate should be first
