"""国家法律法规库 (flk.npc.gov.cn) scraper.

Public, freely-available legal text. Polite rate limiting (1 req/sec).
For Phase A test fidelity, can be run with --fixture-only flag using
recorded HTML.
"""
from __future__ import annotations
import logging
import time
from typing import Iterator
from dataclasses import dataclass

logger = logging.getLogger("tudouclaw.expert.corpus.flk")

INDEX_URL = "https://flk.npc.gov.cn/api/?type=xfwx"  # 现行有效法律列表


@dataclass
class FlkDocument:
    title: str
    url: str
    text: str
    metadata: dict


def fetch_index(max_items: int = 100) -> list[dict]:
    """Get list of currently-valid laws from the index API."""
    import requests
    resp = requests.get(INDEX_URL, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return (data.get("result", {}).get("data", []) or [])[:max_items]


def fetch_document(item: dict) -> FlkDocument | None:
    """Fetch and parse one law document. `item` is from fetch_index()."""
    import requests
    from bs4 import BeautifulSoup
    title = item.get("title", "")
    doc_url = item.get("link") or item.get("url") or ""
    if not doc_url:
        return None
    try:
        resp = requests.get(doc_url, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("fetch failed for %s: %s", title, e)
        return None
    soup = BeautifulSoup(resp.text, "html.parser")
    body = soup.find("div", class_="content") or soup.body
    text = body.get_text("\n").strip() if body else ""
    if not text:
        return None
    return FlkDocument(
        title=title, url=doc_url, text=text,
        metadata={
            "law_name": title,
            "source": "flk_npc",
            "source_url": doc_url,
            "publish_date": item.get("publish") or "",
        },
    )


def iter_all(max_items: int = 100, rate_limit_seconds: float = 1.0) -> Iterator[FlkDocument]:
    """Lazy iterate all current laws. Caller handles indexing."""
    items = fetch_index(max_items=max_items)
    logger.info("flk_npc: %d laws to fetch", len(items))
    for i, item in enumerate(items):
        doc = fetch_document(item)
        if doc:
            yield doc
        if rate_limit_seconds > 0:
            time.sleep(rate_limit_seconds)


def iter_from_fixture(fixture_path: str) -> Iterator[FlkDocument]:
    """Replay a recorded fixture (used in tests / offline mode)."""
    import json
    with open(fixture_path, "r", encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            yield FlkDocument(**d)
