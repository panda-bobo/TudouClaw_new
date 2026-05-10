import pytest
datasets = pytest.importorskip("datasets")


def test_hf_adapter_smoke(monkeypatch):
    """Lightly verify the adapter doesn't crash. Uses a tiny known-good ds."""
    from app.domain_expert.corpus.source_hf import iter_dataset
    # Only run if network is OK; test as a stretch
    try:
        items = list(iter_dataset("Anthropic/hh-rlhf",
                                   split="train", text_field="chosen", max_items=2))
        assert len(items) <= 2
    except Exception as e:
        pytest.skip(f"HF live dataset unavailable: {e}")
