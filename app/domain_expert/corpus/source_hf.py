"""HuggingFace dataset adapter for HF-hosted legal Q/A corpora.

Targets:
  ShengbinYue/DISC-Law-SFT
  YeungNLP/CAIL2018-2019
  ...
"""
from __future__ import annotations
import logging
from typing import Iterator
from dataclasses import dataclass

logger = logging.getLogger("tudouclaw.expert.corpus.hf")


@dataclass
class HfRecord:
    text: str
    metadata: dict


def iter_dataset(
    dataset_id: str,
    split: str = "train",
    text_field: str = "text",
    max_items: int | None = None,
    metadata_fields: list[str] | None = None,
) -> Iterator[HfRecord]:
    """Stream a HuggingFace dataset. Requires `datasets` package."""
    try:
        from datasets import load_dataset
    except ImportError as e:
        raise RuntimeError("`datasets` package required. pip install datasets") from e
    logger.info("loading HF dataset %s split=%s", dataset_id, split)
    ds = load_dataset(dataset_id, split=split, streaming=True)
    metadata_fields = metadata_fields or []
    count = 0
    for row in ds:
        text = row.get(text_field, "")
        if not text:
            continue
        meta = {f: row.get(f, "") for f in metadata_fields}
        meta["source"] = f"hf:{dataset_id}"
        yield HfRecord(text=text, metadata=meta)
        count += 1
        if max_items and count >= max_items:
            break
