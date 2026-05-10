"""Corpus manifest — tracks which sources are indexed for a given agent."""
from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass, field, asdict


@dataclass
class CorpusSourceEntry:
    source_id: str                     # e.g. "flk_npc" / "hf:disc-law-sft"
    version: str = ""                  # snapshot version
    chunk_count: int = 0
    bytes: int = 0
    indexed_at: float = 0.0
    chunker_strategy: str = ""
    notes: str = ""


@dataclass
class CorpusManifest:
    agent_id: str
    sources: list[CorpusSourceEntry] = field(default_factory=list)
    last_updated: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "CorpusManifest":
        sources = [CorpusSourceEntry(**s) for s in d.get("sources", [])]
        return CorpusManifest(
            agent_id=d.get("agent_id", ""),
            sources=sources,
            last_updated=d.get("last_updated", time.time()),
        )

    def add_source(self, entry: CorpusSourceEntry) -> None:
        # replace if exists
        self.sources = [s for s in self.sources if s.source_id != entry.source_id]
        self.sources.append(entry)
        self.last_updated = time.time()

    def get_source(self, source_id: str) -> CorpusSourceEntry | None:
        for s in self.sources:
            if s.source_id == source_id:
                return s
        return None

    def remove_source(self, source_id: str) -> bool:
        before = len(self.sources)
        self.sources = [s for s in self.sources if s.source_id != source_id]
        if len(self.sources) < before:
            self.last_updated = time.time()
            return True
        return False

    def total_chunks(self) -> int:
        return sum(s.chunk_count for s in self.sources)

    def total_bytes(self) -> int:
        return sum(s.bytes for s in self.sources)

    @staticmethod
    def path_for(agent_id: str) -> str:
        from .._config import expert_dir_for
        return os.path.join(expert_dir_for(agent_id), "corpus", "_manifest.json")

    @staticmethod
    def load(agent_id: str) -> "CorpusManifest":
        p = CorpusManifest.path_for(agent_id)
        if not os.path.exists(p):
            return CorpusManifest(agent_id=agent_id)
        with open(p, "r", encoding="utf-8") as f:
            return CorpusManifest.from_dict(json.load(f))

    def save(self) -> None:
        p = CorpusManifest.path_for(self.agent_id)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
