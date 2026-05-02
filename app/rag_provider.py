"""
RAG Provider Registry — pluggable vector search backends.

Supports:
  - local:    Built-in ChromaDB (default, zero-config)
  - remote:   HTTP endpoints on other TudouClaw nodes or third-party services
  - custom:   Any provider implementing the RAGProvider protocol

Each provider can host multiple *collections* (knowledge bases).
Agents reference a provider via `rag_provider_id` in their profile.
An empty provider ID means "use local ChromaDB".

Provider config is persisted in ~/.tudou_claw/rag_providers.json.

Remote API contract (POST /api/rag/search):
  Request:  {"query": "...", "collection": "...", "top_k": 5}
  Response: {"results": [{"id": "...", "title": "...", "content": "...",
                           "distance": 0.12, "metadata": {...}}]}

Remote API contract (POST /api/rag/ingest):
  Request:  {"collection": "...", "documents": [{"id": "...", "title": "...",
             "content": "...", "tags": [...]}]}
  Response: {"ok": true, "count": N}
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("tudou.rag")

_DATA_DIR = Path.home() / ".tudou_claw"
_PROVIDERS_FILE = _DATA_DIR / "rag_providers.json"
_DOMAIN_KB_FILE = _DATA_DIR / "domain_knowledge_bases.json"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class RAGResult:
    """Single search result from a RAG provider."""
    id: str = ""
    title: str = ""
    content: str = ""
    distance: float = 0.0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "content": self.content,
            "distance": self.distance, "metadata": self.metadata,
        }


@dataclass
class RAGCollection:
    """Metadata for a knowledge collection."""
    id: str = ""
    name: str = ""
    description: str = ""
    provider_id: str = ""
    doc_count: int = 0
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "provider_id": self.provider_id, "doc_count": self.doc_count,
            "created_at": self.created_at,
        }


@dataclass
class RAGProviderEntry:
    """Registered RAG provider."""
    id: str = ""
    name: str = ""
    kind: str = "local"     # "local" | "remote" | "custom"
    base_url: str = ""      # For remote providers: http://node:port
    api_key: str = ""       # Optional auth
    enabled: bool = True
    config: dict = field(default_factory=dict)   # Provider-specific settings
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "kind": self.kind,
            "base_url": self.base_url, "api_key": self.api_key,
            "enabled": self.enabled, "config": self.config,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(d: dict) -> RAGProviderEntry:
        return RAGProviderEntry(
            id=d.get("id", ""),
            name=d.get("name", ""),
            kind=d.get("kind", "local"),
            base_url=d.get("base_url", ""),
            api_key=d.get("api_key", ""),
            enabled=d.get("enabled", True),
            config=d.get("config", {}),
            created_at=d.get("created_at", time.time()),
        )


@dataclass
class DomainKnowledgeBase:
    """A standalone domain knowledge base, decoupled from any specific agent.

    Domain KBs persist independently — deleting an agent that uses one does NOT
    remove the KB.  Multiple advisor agents can share the same domain KB.
    """
    id: str = ""
    name: str = ""                     # e.g. "法律知识库", "财务知识库"
    description: str = ""
    collection: str = ""               # RAG collection name (e.g. "domain_law")
    provider_id: str = ""              # empty = local ChromaDB
    # Per-KB embedding model. Empty string = use the server default
    # (DEFAULT_EMBEDDING_MODEL). Each KB's ChromaDB collection is bound
    # to whatever model was used at INGEST time — switching this field
    # on an existing KB will cause dim-mismatch errors on next query,
    # so it should be locked at creation. ALLOWED_EMBEDDING_MODELS in
    # the API layer enforces a known-good allow-list.
    embedding_model: str = ""
    tags: list[str] = field(default_factory=list)
    doc_count: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "description": self.description,
            "collection": self.collection, "provider_id": self.provider_id,
            "embedding_model": self.embedding_model,
            "tags": self.tags, "doc_count": self.doc_count,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(d: dict) -> DomainKnowledgeBase:
        return DomainKnowledgeBase(
            id=d.get("id", ""),
            name=d.get("name", ""),
            description=d.get("description", ""),
            collection=d.get("collection", ""),
            provider_id=d.get("provider_id", ""),
            embedding_model=d.get("embedding_model", ""),
            tags=d.get("tags", []),
            doc_count=d.get("doc_count", 0),
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
        )


class DomainKBStore:
    """Persistent store for standalone domain knowledge bases."""

    def __init__(self, path: Path | str | None = None):
        self._path = Path(path) if path else _DOMAIN_KB_FILE
        self._kbs: dict[str, DomainKnowledgeBase] = {}
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                for d in data.get("knowledge_bases", []):
                    kb = DomainKnowledgeBase.from_dict(d)
                    if kb.id:
                        self._kbs[kb.id] = kb
                logger.info("Loaded %d domain knowledge bases", len(self._kbs))
            except Exception as e:
                logger.warning("Failed to load domain KBs: %s", e)

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {"knowledge_bases": [kb.to_dict() for kb in self._kbs.values()]}
        self._path.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                              encoding="utf-8")

    def list_all(self) -> list[DomainKnowledgeBase]:
        return sorted(self._kbs.values(), key=lambda x: x.created_at, reverse=True)

    def get(self, kb_id: str) -> DomainKnowledgeBase | None:
        return self._kbs.get(kb_id)

    def create(self, name: str, description: str = "", provider_id: str = "",
               tags: list[str] | None = None,
               embedding_model: str = "") -> DomainKnowledgeBase:
        kb_id = f"dkb_{uuid.uuid4().hex[:10]}"
        collection = f"domain_{kb_id}"
        kb = DomainKnowledgeBase(
            id=kb_id, name=name, description=description,
            collection=collection, provider_id=provider_id,
            embedding_model=(embedding_model or "").strip(),
            tags=tags or [],
        )
        self._kbs[kb_id] = kb
        # Create the underlying RAG collection
        try:
            get_rag_registry().create_collection(provider_id, collection)
        except Exception as e:
            logger.warning("Failed to create RAG collection for domain KB %s: %s", kb_id, e)
        self._save()
        return kb

    def update(self, kb_id: str, name: str | None = None,
               description: str | None = None,
               tags: list[str] | None = None) -> DomainKnowledgeBase | None:
        kb = self._kbs.get(kb_id)
        if not kb:
            return None
        if name is not None:
            kb.name = name
        if description is not None:
            kb.description = description
        if tags is not None:
            kb.tags = tags
        kb.updated_at = time.time()
        self._save()
        return kb

    def delete(self, kb_id: str) -> bool:
        kb = self._kbs.pop(kb_id, None)
        if kb is None:
            return False
        # Optionally delete the underlying collection
        try:
            get_rag_registry().delete_collection(kb.provider_id, kb.collection)
        except Exception as e:
            logger.warning("Failed to delete RAG collection for domain KB %s: %s", kb_id, e)
        self._save()
        return True

    def increment_doc_count(self, kb_id: str, delta: int = 1):
        kb = self._kbs.get(kb_id)
        if kb:
            kb.doc_count += delta
            kb.updated_at = time.time()
            self._save()


_domain_kb_store: DomainKBStore | None = None

def get_domain_kb_store() -> DomainKBStore:
    global _domain_kb_store
    if _domain_kb_store is None:
        _domain_kb_store = DomainKBStore()
    return _domain_kb_store


# ---------------------------------------------------------------------------
# Provider Registry (singleton)
# ---------------------------------------------------------------------------

class RAGProviderRegistry:
    """Manages registered RAG providers and routes queries."""

    def __init__(self):
        self._providers: dict[str, RAGProviderEntry] = {}
        self._loaded = False

    # A ghost entry has an id but no distinguishing fields. They are
    # produced when the REST handler persists a ``RAGProviderEntry()``
    # empty constructor (typically from a POST with an empty JSON body
    # or a form-submit that cleared every field). Filter them at both
    # load AND save time so we stop the bleeding AND never re-persist
    # a garbage row read from an older file.
    @staticmethod
    def _is_ghost(p: RAGProviderEntry) -> bool:
        return (not p.name.strip()
                and not p.base_url.strip()
                and p.kind == "remote"
                and not p.config)

    def _ensure_loaded(self):
        if self._loaded:
            return
        self._loaded = True
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        if _PROVIDERS_FILE.exists():
            try:
                with open(_PROVIDERS_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                ghosts_skipped = 0
                for d in data:
                    p = RAGProviderEntry.from_dict(d)
                    if not p.id:
                        continue
                    if self._is_ghost(p):
                        ghosts_skipped += 1
                        continue
                    self._providers[p.id] = p
                if ghosts_skipped:
                    logger.warning(
                        "Skipped %d ghost RAG provider entries on load "
                        "(empty name+url+config). Consider running the "
                        "cleanup script.", ghosts_skipped,
                    )
            except Exception as e:
                logger.error("Failed to load RAG providers: %s", e)

    def _save(self):
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        try:
            with open(_PROVIDERS_FILE, "w", encoding="utf-8") as f:
                json.dump([p.to_dict() for p in self._providers.values()],
                          f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error("Failed to save RAG providers: %s", e)

    # ── CRUD ──

    def list_providers(self) -> list[RAGProviderEntry]:
        self._ensure_loaded()
        return list(self._providers.values())

    def get_provider(self, provider_id: str) -> Optional[RAGProviderEntry]:
        self._ensure_loaded()
        return self._providers.get(provider_id)

    def register(self, name: str, kind: str = "remote",
                 base_url: str = "", api_key: str = "",
                 config: dict = None) -> RAGProviderEntry:
        """Register a new RAG provider.

        Rejects ghost registrations (empty name + empty URL on a remote
        provider) — these were the source of the ~59 junk entries that
        accumulated previously. Callers that genuinely want a blank
        local-kind slot must pass kind='local' explicitly.
        """
        self._ensure_loaded()
        # Guard: reject obvious ghosts at the front door.
        if kind == "remote" and not name.strip() and not base_url.strip():
            raise ValueError(
                "RAG provider rejected: kind='remote' requires name or "
                "base_url. Pass kind='local' for a blank local provider."
            )
        entry = RAGProviderEntry(
            id=uuid.uuid4().hex[:10],
            name=name, kind=kind,
            base_url=base_url.rstrip("/") if base_url else "",
            api_key=api_key,
            config=config or {},
        )
        self._providers[entry.id] = entry
        self._save()
        logger.info("RAG provider registered: %s (%s, %s)", name, kind, entry.id)
        return entry

    def update(self, provider_id: str, **kwargs) -> Optional[RAGProviderEntry]:
        """Update provider fields."""
        self._ensure_loaded()
        p = self._providers.get(provider_id)
        if not p:
            return None
        for k, v in kwargs.items():
            if hasattr(p, k):
                setattr(p, k, v)
        self._save()
        return p

    def remove(self, provider_id: str) -> bool:
        self._ensure_loaded()
        if provider_id in self._providers:
            del self._providers[provider_id]
            self._save()
            return True
        return False

    # ── Search routing ──

    def search(self, provider_id: str, collection: str,
               query: str, top_k: int = 5) -> list[RAGResult]:
        """Route a search query to the correct provider backend."""
        self._ensure_loaded()

        if not provider_id or provider_id == "local":
            return self._search_local(collection, query, top_k)

        provider = self._providers.get(provider_id)
        if not provider:
            logger.warning("RAG provider not found: %s, falling back to local", provider_id)
            return self._search_local(collection, query, top_k)

        if not provider.enabled:
            logger.warning("RAG provider disabled: %s", provider_id)
            return []

        if provider.kind == "remote":
            return self._search_remote(provider, collection, query, top_k)
        elif provider.kind == "local":
            return self._search_local(collection, query, top_k)
        else:
            # Custom — extensibility point
            logger.warning("Custom RAG provider not implemented: %s", provider.kind)
            return []

    def ingest(self, provider_id: str, collection: str,
               documents: list[dict]) -> int:
        """Route document ingestion to the correct provider backend.

        Each document: {"id": "...", "title": "...", "content": "...", "tags": [...]}
        Returns: number of documents ingested.
        """
        self._ensure_loaded()

        if not provider_id or provider_id == "local":
            return self._ingest_local(collection, documents)

        provider = self._providers.get(provider_id)
        if not provider or not provider.enabled:
            logger.warning("RAG provider unavailable: %s, falling back to local", provider_id)
            return self._ingest_local(collection, documents)

        if provider.kind == "remote":
            return self._ingest_remote(provider, collection, documents)
        elif provider.kind == "local":
            return self._ingest_local(collection, documents)
        else:
            return 0

    # ── Collection management ──

    def create_collection(self, provider_id: str, collection_name: str,
                          description: str = "") -> RAGCollection:
        """Create a new collection on the specified provider."""
        if not provider_id or provider_id == "local":
            return self._create_local_collection(collection_name, description)
        provider = self._providers.get(provider_id)
        if provider and provider.kind == "remote":
            return self._create_remote_collection(provider, collection_name, description)
        return self._create_local_collection(collection_name, description)

    def list_collections(self, provider_id: str = "") -> list[RAGCollection]:
        """List collections on a provider."""
        if not provider_id or provider_id == "local":
            return self._list_local_collections()
        provider = self._providers.get(provider_id)
        if provider and provider.kind == "remote":
            return self._list_remote_collections(provider)
        return self._list_local_collections()

    def delete_collection(self, provider_id: str, collection_name: str) -> bool:
        if not provider_id or provider_id == "local":
            return self._delete_local_collection(collection_name)
        provider = self._providers.get(provider_id)
        if provider and provider.kind == "remote":
            return self._delete_remote_collection(provider, collection_name)
        return False

    # ── Local ChromaDB backend ──

    def _get_memory_manager(self):
        try:
            from .core.memory import get_memory_manager
            return get_memory_manager()
        except Exception:
            return None

    def _resolve_kb_embed_model(self, collection: str) -> str | None:
        """If ``collection`` belongs to a domain KB with a per-KB
        ``embedding_model`` set, return that model name. Otherwise
        return None so the caller falls back to the server default.

        Conv: domain KB collections are named ``domain_<kb_id>``. We
        avoid the lookup for the (more frequent) memory_facts /
        memory_episodes / knowledge collections so the hot path stays
        cheap.
        """
        if not collection or not collection.startswith("domain_"):
            return None
        try:
            store = get_domain_kb_store()
            kb_id = collection[len("domain_"):]
            kb = store.get(kb_id)
            if kb and kb.embedding_model:
                return kb.embedding_model
        except Exception:
            pass
        return None

    def _search_local(self, collection: str, query: str,
                      top_k: int = 5) -> list[RAGResult]:
        mm = self._get_memory_manager()
        if not mm:
            return []
        try:
            coll = mm._get_chroma_collection(collection, model_name=self._resolve_kb_embed_model(collection))
            if coll.count() == 0:
                return []
            results = coll.query(query_texts=[query], n_results=min(top_k, 20))
            items = []
            if results and results.get("ids") and results["ids"][0]:
                for i, doc_id in enumerate(results["ids"][0]):
                    meta = results["metadatas"][0][i] if results.get("metadatas") else {}
                    doc = results["documents"][0][i] if results.get("documents") else ""
                    # v1-C: surface heading_path / source_file in the
                    # result's metadata (was already there via meta dict,
                    # but be explicit so callers know these are first-class).
                    items.append(RAGResult(
                        id=doc_id,
                        title=meta.get("title", ""),
                        content=doc,
                        distance=results["distances"][0][i] if results.get("distances") else 0,
                        metadata=dict(meta or {}),
                    ))
            return items
        except Exception as e:
            logger.warning("Local RAG search failed (collection=%s): %s", collection, e)
            return []

    def _ingest_local(self, collection: str, documents: list[dict]) -> int:
        """Ingest into the local ChromaDB.

        RAG v1-C: persist the structured metadata the chunker now
        produces (content_hash / heading_path / source_file /
        chunk_index / imported_at) so downstream consumers (UI,
        dedup, hybrid search) can use it. Also does cross-request
        dedup by content_hash: if a doc's content_hash already exists
        in the collection, we skip re-ingest to avoid KB bloat on
        repeat imports of the same file.
        """
        mm = self._get_memory_manager()
        if not mm:
            return 0
        count = 0
        try:
            coll = mm._get_chroma_collection(collection, model_name=self._resolve_kb_embed_model(collection))

            # Collect content_hashes already in the collection so we
            # can skip duplicates. ChromaDB's `where` filter handles
            # metadata but doesn't do "value in set" — we grab the
            # hashes once upfront. Cheap for collections < 100k rows.
            existing_hashes: set[str] = set()
            try:
                probe = coll.get(include=["metadatas"])
                for m in (probe.get("metadatas") or []):
                    h = (m or {}).get("content_hash", "")
                    if h:
                        existing_hashes.add(h)
            except Exception:
                # Older ChromaDB clients may not support include=["metadatas"]
                # on .get(); on failure just proceed without cross-request
                # dedup (upsert-by-id still protects same-id duplicates).
                existing_hashes = set()

            skipped_dupes = 0
            for doc in documents:
                doc_id = doc.get("id") or uuid.uuid4().hex[:10]
                title = (doc.get("title") or "").strip()
                content = (doc.get("content") or "").strip()
                if not content:
                    continue
                # RAG v1-C: dedup by content_hash across requests.
                content_hash = doc.get("content_hash", "")
                if content_hash and content_hash in existing_hashes:
                    skipped_dupes += 1
                    continue

                text = f"{title}. {title}. {content}" if title else content
                # Flatten structured metadata. ChromaDB accepts scalars
                # (str / int / float / bool); list/dict must be
                # stringified. `tags` already goes comma-joined.
                metadata: dict = {
                    "title": title,
                    "tags": ",".join(doc.get("tags", []) or []),
                    "source": doc.get("source", "import"),
                    "created_at": time.time(),
                }
                # v1-C fields — only write non-empty/non-default values
                # to keep ChromaDB payloads small.
                for k in ("content_hash", "heading_path", "source_file"):
                    v = doc.get(k)
                    if v:
                        metadata[k] = str(v)
                for k in ("chunk_index", "imported_at"):
                    v = doc.get(k)
                    if v is not None:
                        try:
                            metadata[k] = (float(v) if "_at" in k
                                           else int(v))
                        except Exception:
                            pass
                coll.upsert(ids=[doc_id], documents=[text],
                            metadatas=[metadata])
                if content_hash:
                    existing_hashes.add(content_hash)
                count += 1
            if skipped_dupes:
                logger.info(
                    "Local RAG ingest: skipped %d duplicates by content_hash "
                    "(collection=%s)", skipped_dupes, collection,
                )
        except Exception as e:
            logger.warning("Local RAG ingest failed (collection=%s): %s",
                           collection, e)
        return count

    # ── RAG v1-B: Hybrid retrieval (BM25 + Vector + RRF) ────────────
    # Design:
    #   1. Vector search → top-N candidates (n = top_k * 4)
    #   2. BM25 over the same corpus (fetched via coll.get(include=...))
    #      → top-N candidates
    #   3. Reciprocal Rank Fusion: score_d = Σ 1 / (K + rank_in_each_list)
    #      — classical RRF with K=60
    #   4. Top-k by fused score
    # No new dependency: BM25 is implemented in ~40 lines with stdlib.

    _RRF_K = 60
    _HYBRID_CANDIDATE_MULTIPLIER = 4

    @staticmethod
    def _tokenize(s: str) -> list[str]:
        """Cheap tokenizer: lowercase + split on non-alphanumeric, plus
        split CJK runs into per-character tokens so 中文 retrieval works."""
        import re as _re
        s = (s or "").lower()
        # Latin tokens of length >= 2.
        latin = _re.findall(r"[a-z0-9][a-z0-9_]+", s)
        # CJK characters (each character becomes its own token).
        cjk = _re.findall(r"[\u4e00-\u9fff]", s)
        return latin + cjk

    def _bm25_search(self, collection: str, query: str,
                     top_k: int = 20) -> list[tuple[str, float]]:
        """Pure-python BM25 over the local ChromaDB collection. Returns
        ``[(doc_id, score), ...]`` sorted by score desc. Falls back to
        empty list on any error."""
        mm = self._get_memory_manager()
        if not mm:
            return []
        try:
            coll = mm._get_chroma_collection(collection, model_name=self._resolve_kb_embed_model(collection))
            count = coll.count()
            if count == 0:
                return []
            # Pull all docs + ids. Cheap for collections < ~50k.
            # Title is prefixed into the text at ingest (`title. title. body`),
            # so BM25 naturally weights title matches higher.
            probe = coll.get(include=["documents"])
            ids = probe.get("ids") or []
            docs = probe.get("documents") or []
            if not ids or not docs:
                return []
            qtoks = self._tokenize(query)
            if not qtoks:
                return []
            # Pre-tokenize corpus once.
            tokenized = [self._tokenize(d) for d in docs]
            doc_lens = [len(t) for t in tokenized]
            avgdl = (sum(doc_lens) / len(doc_lens)) if doc_lens else 1.0
            # Document-frequency per query token.
            import math as _math
            n_docs = len(docs)
            df: dict[str, int] = {}
            # For query tokens only — small dict.
            qset = set(qtoks)
            for t in tokenized:
                seen = set()
                for tok in t:
                    if tok in qset and tok not in seen:
                        df[tok] = df.get(tok, 0) + 1
                        seen.add(tok)
            # BM25 params.
            K1 = 1.5
            B = 0.75
            scores: list[tuple[str, float]] = []
            for idx, toks in enumerate(tokenized):
                if not toks:
                    continue
                # Term frequencies in this doc.
                tf: dict[str, int] = {}
                for tok in toks:
                    if tok in qset:
                        tf[tok] = tf.get(tok, 0) + 1
                if not tf:
                    continue
                s = 0.0
                for qt, qcount in [(q, 1) for q in qset]:
                    if qt not in tf:
                        continue
                    n_qt = df.get(qt, 0)
                    # IDF with + 0.5 smoothing.
                    idf = _math.log(
                        (n_docs - n_qt + 0.5) / (n_qt + 0.5) + 1.0
                    )
                    f = tf[qt]
                    dl = doc_lens[idx] or 1
                    num = f * (K1 + 1)
                    den = f + K1 * (1 - B + B * dl / avgdl)
                    s += idf * num / den
                if s > 0:
                    scores.append((ids[idx], s))
            scores.sort(key=lambda x: -x[1])
            return scores[:top_k]
        except Exception as e:
            logger.debug("BM25 search failed (collection=%s): %s",
                         collection, e)
            return []

    @staticmethod
    def _rrf_fuse(rank_lists: list[list[tuple[str, float]]],
                  K: int = 60) -> list[tuple[str, float]]:
        """Reciprocal Rank Fusion. Each list is already sorted best-first.
        Returns fused [(doc_id, rrf_score), ...] sorted desc."""
        fused: dict[str, float] = {}
        for lst in rank_lists:
            for rank, (doc_id, _) in enumerate(lst):
                fused[doc_id] = fused.get(doc_id, 0.0) + 1.0 / (K + rank + 1)
        return sorted(fused.items(), key=lambda x: -x[1])

    def hybrid_search(self, provider_id: str, collection: str,
                      query: str, top_k: int = 5) -> list[RAGResult]:
        """Vector + BM25 + RRF. Falls back to pure vector when BM25
        produces no results (empty collection, CJK-only query with
        punctuation, etc.). The extra work is bounded — ~ms for
        collections under 10k chunks.
        """
        self._ensure_loaded()

        # Only local / ChromaDB path supports BM25 co-location.
        # Remote providers fall back to their own search.
        if provider_id and provider_id != "local":
            provider = self._providers.get(provider_id)
            if provider and provider.kind == "remote":
                return self._search_remote(provider, collection, query, top_k)

        # Local: gather both candidate lists.
        candidate_k = max(top_k * self._HYBRID_CANDIDATE_MULTIPLIER, 20)

        vector_hits = self._search_local(collection, query, top_k=candidate_k)
        vector_rank = [(r.id, 1.0 / (1 + r.distance)) for r in vector_hits]

        bm25_rank = self._bm25_search(collection, query, top_k=candidate_k)

        # If BM25 found nothing, short-circuit to vector-only.
        if not bm25_rank:
            return vector_hits[:top_k]
        # If vector is empty but BM25 has hits, hydrate from the corpus.
        if not vector_rank and bm25_rank:
            # Can't fuse without vector hits; just return BM25 order.
            id_to_result = {r.id: r for r in vector_hits}
            # Re-fetch missing docs so callers still get full content/metadata.
            mm = self._get_memory_manager()
            try:
                coll = mm._get_chroma_collection(collection, model_name=self._resolve_kb_embed_model(collection))
                probe = coll.get(include=["documents", "metadatas"])
                doc_map = {
                    probe["ids"][i]: (
                        probe["documents"][i] if i < len(probe.get("documents", [])) else "",
                        probe["metadatas"][i] if i < len(probe.get("metadatas", [])) else {},
                    )
                    for i in range(len(probe.get("ids", [])))
                }
            except Exception:
                doc_map = {}
            out: list[RAGResult] = []
            for doc_id, _s in bm25_rank[:top_k]:
                content, meta = doc_map.get(doc_id, ("", {}))
                out.append(RAGResult(
                    id=doc_id,
                    title=(meta or {}).get("title", ""),
                    content=content,
                    distance=0.0,
                    metadata=meta or {},
                ))
            return out

        # Both lists present — RRF fuse.
        fused = self._rrf_fuse([vector_rank, bm25_rank], K=self._RRF_K)
        id_to_result = {r.id: r for r in vector_hits}
        # Hydrate BM25-only ids with fresh fetches.
        missing_ids = [d for d, _ in fused if d not in id_to_result]
        if missing_ids:
            mm = self._get_memory_manager()
            try:
                coll = mm._get_chroma_collection(collection, model_name=self._resolve_kb_embed_model(collection))
                probe = coll.get(ids=missing_ids,
                                 include=["documents", "metadatas"])
                for i, doc_id in enumerate(probe.get("ids", []) or []):
                    content = (probe.get("documents") or [""])[i] if i < len(probe.get("documents", [])) else ""
                    meta = (probe.get("metadatas") or [{}])[i] if i < len(probe.get("metadatas", [])) else {}
                    id_to_result[doc_id] = RAGResult(
                        id=doc_id,
                        title=(meta or {}).get("title", ""),
                        content=content,
                        distance=0.0,
                        metadata=meta or {},
                    )
            except Exception:
                pass

        out = []
        for doc_id, rrf_score in fused[:top_k]:
            r = id_to_result.get(doc_id)
            if r is None:
                continue
            # Attach the fused score so callers can see it.
            r.metadata = dict(r.metadata or {})
            r.metadata["rrf_score"] = round(rrf_score, 6)
            out.append(r)
        return out

    def kb_statistics(self, provider_id: str, collection: str,
                      query: str = "") -> dict:
        """Pack v3 — aggregate/count mode (bypasses top-k RAG).

        Scans the entire collection's metadata, groups chunks by
        source_file, and optionally filters by substring match on
        title / heading_path / source_file.

        Returns a dict shaped for knowledge_lookup(mode="count"):
            {
                "total_chunks": int,
                "unique_source_files": int,
                "by_source_file": [
                    {"source_file": str, "chunk_count": int,
                     "first_heading": str, "titles_sample": [str, ...]},
                    ...
                ],
                "filter": str,        # the query passed in, for LLM context
                "filter_matched": int # chunks that matched, 0 == no filter
            }

        Pure metadata — no embedding / no BM25 / no LLM. Works on any
        collection size since it's a single coll.get(). Scales linearly
        with chunk count; fine up to ~50k chunks.
        """
        self._ensure_loaded()
        if provider_id and provider_id != "local":
            # Remote providers: stats endpoint is provider-specific.
            # Return empty rather than fabricating.
            return {
                "total_chunks": 0, "unique_source_files": 0,
                "by_source_file": [], "filter": query or "",
                "filter_matched": 0,
                "note": "kb_statistics only supported for local provider",
            }
        mm = self._get_memory_manager()
        if not mm:
            return {"total_chunks": 0, "unique_source_files": 0,
                    "by_source_file": [], "filter": query or "",
                    "filter_matched": 0}
        try:
            coll = mm._get_chroma_collection(collection, model_name=self._resolve_kb_embed_model(collection))
            probe = coll.get(include=["metadatas", "documents"])
        except Exception as e:
            logger.debug("kb_statistics get failed: %s", e)
            return {"total_chunks": 0, "unique_source_files": 0,
                    "by_source_file": [], "filter": query or "",
                    "filter_matched": 0}

        ids = probe.get("ids") or []
        metas = probe.get("metadatas") or []
        docs = probe.get("documents") or []
        total = len(ids)
        q = (query or "").strip().lower()

        # Group by source_file (fall back to "unknown" bucket).
        groups: dict[str, dict] = {}
        filter_matched = 0
        for i, _id in enumerate(ids):
            meta = metas[i] if i < len(metas) else {}
            meta = meta or {}
            src = str(meta.get("source_file") or "unknown")
            title = str(meta.get("title") or "")
            heading = str(meta.get("heading_path") or "")
            doc_body = str(docs[i]) if i < len(docs) else ""
            # Filter match: substring in title / heading_path / source_file
            # / doc body (body search catches chunks where source metadata
            # is thin but content has the term).
            matched = True
            if q:
                hay = (title + " " + heading + " " + src + " " +
                       doc_body).lower()
                matched = q in hay
                if matched:
                    filter_matched += 1
            if not matched:
                continue
            g = groups.setdefault(src, {
                "source_file": src, "chunk_count": 0,
                "first_heading": "", "titles_sample": [],
            })
            g["chunk_count"] += 1
            if not g["first_heading"] and heading:
                g["first_heading"] = heading[:120]
            if title and title not in g["titles_sample"] and \
                    len(g["titles_sample"]) < 5:
                g["titles_sample"].append(title[:120])

        by_source = sorted(groups.values(),
                           key=lambda x: -x["chunk_count"])
        return {
            "total_chunks": total,
            "unique_source_files": len(by_source),
            "by_source_file": by_source,
            "filter": query or "",
            "filter_matched": filter_matched if q else 0,
        }

    def kb_list(self, provider_id: str, collection: str,
                query: str = "", limit: int = 50) -> dict:
        """Pack v3 — list mode (per-chunk metadata, no content bodies).

        Like kb_statistics but returns a flat list of chunks ordered by
        (source_file, chunk_index) with their metadata. Useful when the
        user asks "列出所有文档块 / 目录 / 有哪些条目". Capped at
        ``limit`` rows to keep the LLM payload bounded.
        """
        self._ensure_loaded()
        if provider_id and provider_id != "local":
            return {"items": [], "total": 0, "truncated": False,
                    "note": "kb_list only supported for local provider"}
        mm = self._get_memory_manager()
        if not mm:
            return {"items": [], "total": 0, "truncated": False}
        try:
            coll = mm._get_chroma_collection(collection, model_name=self._resolve_kb_embed_model(collection))
            probe = coll.get(include=["metadatas"])
        except Exception as e:
            logger.debug("kb_list get failed: %s", e)
            return {"items": [], "total": 0, "truncated": False}

        ids = probe.get("ids") or []
        metas = probe.get("metadatas") or []
        q = (query or "").strip().lower()

        rows = []
        for i, _id in enumerate(ids):
            m = metas[i] if i < len(metas) else {}
            m = m or {}
            title = str(m.get("title") or "")
            heading = str(m.get("heading_path") or "")
            src = str(m.get("source_file") or "")
            if q:
                hay = (title + " " + heading + " " + src).lower()
                if q not in hay:
                    continue
            rows.append({
                "id": _id,
                "title": title[:120],
                "source_file": src,
                "heading_path": heading[:120],
                "chunk_index": m.get("chunk_index"),
            })
        # Stable order: source_file then chunk_index
        rows.sort(key=lambda r: (r["source_file"],
                                 r["chunk_index"] if isinstance(
                                     r["chunk_index"], (int, float)) else 10 ** 9))
        total_matched = len(rows)
        truncated = total_matched > limit
        return {
            "items": rows[:limit],
            "total": total_matched,
            "truncated": truncated,
            "filter": query or "",
        }

    def _create_local_collection(self, name: str, description: str = "") -> RAGCollection:
        mm = self._get_memory_manager()
        if mm:
            try:
                mm._get_chroma_collection(name, model_name=self._resolve_kb_embed_model(name))  # creates if not exist
            except Exception as e:
                logger.warning("Create local collection failed: %s", e)
        return RAGCollection(id=name, name=name, description=description,
                             provider_id="local", created_at=time.time())

    def _list_local_collections(self) -> list[RAGCollection]:
        mm = self._get_memory_manager()
        if not mm:
            return []
        try:
            client = mm._get_chromadb_client()
            colls = client.list_collections()
            result = []
            for c in colls:
                name = c.name if hasattr(c, 'name') else str(c)
                result.append(RAGCollection(
                    id=name, name=name, provider_id="local",
                    doc_count=0,  # would need count() call per collection
                ))
            return result
        except Exception:
            return []

    def _delete_local_collection(self, name: str) -> bool:
        mm = self._get_memory_manager()
        if not mm:
            return False
        try:
            client = mm._get_chromadb_client()
            client.delete_collection(name)
            return True
        except Exception as e:
            logger.warning("Delete local collection failed: %s", e)
            return False

    # ── Remote HTTP backend ──

    def _remote_headers(self, provider: RAGProviderEntry) -> dict:
        headers = {"Content-Type": "application/json"}
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"
        # Support TudouClaw node secret
        secret = provider.config.get("node_secret")
        if secret:
            headers["X-Claw-Secret"] = secret
        return headers

    def _search_remote(self, provider: RAGProviderEntry, collection: str,
                       query: str, top_k: int = 5) -> list[RAGResult]:
        import requests
        url = f"{provider.base_url}/api/rag/search"
        try:
            resp = requests.post(url, json={
                "query": query, "collection": collection, "top_k": top_k,
            }, headers=self._remote_headers(provider), timeout=15)
            if resp.status_code != 200:
                logger.warning("Remote RAG search failed (%s): %d %s",
                               provider.name, resp.status_code, resp.text[:200])
                return []
            data = resp.json()
            return [
                RAGResult(
                    id=r.get("id", ""),
                    title=r.get("title", ""),
                    content=r.get("content", ""),
                    distance=r.get("distance", 0.0),
                    metadata=r.get("metadata", {}),
                )
                for r in data.get("results", [])
            ]
        except Exception as e:
            logger.warning("Remote RAG search error (%s): %s", provider.name, e)
            return []

    def _ingest_remote(self, provider: RAGProviderEntry, collection: str,
                       documents: list[dict]) -> int:
        # Public entry: never raises (caller expects int). Inner closure
        # is wrapped in @retry so transient ConnectionError / Timeout
        # gets up to 3 attempts before we surface the silent-fail.
        import requests
        from .resilience import retry as _retry
        url = f"{provider.base_url}/api/rag/ingest"

        @_retry(max_attempts=3, base_delay=1.0, max_delay=10.0,
                retryable_exceptions=(requests.RequestException,))
        def _do_post() -> int:
            resp = requests.post(url, json={
                "collection": collection, "documents": documents,
            }, headers=self._remote_headers(provider), timeout=30)
            if resp.status_code >= 500:
                # Retryable upstream error — raise so @retry sees it.
                raise requests.RequestException(
                    f"upstream {resp.status_code} from {url}"
                )
            if resp.status_code != 200:
                # 4xx (auth / bad request) — not worth retrying.
                logger.warning("Remote RAG ingest failed (%s): %d",
                               provider.name, resp.status_code)
                return 0
            return resp.json().get("count", 0)

        try:
            return _do_post()
        except Exception as e:
            logger.warning("Remote RAG ingest error (%s): %s", provider.name, e)
            return 0

    def _create_remote_collection(self, provider: RAGProviderEntry,
                                  name: str, description: str = "") -> RAGCollection:
        import requests
        url = f"{provider.base_url}/api/rag/collection/create"
        try:
            resp = requests.post(url, json={
                "name": name, "description": description,
            }, headers=self._remote_headers(provider), timeout=10)
            if resp.status_code == 200:
                return RAGCollection(id=name, name=name, description=description,
                                     provider_id=provider.id, created_at=time.time())
        except Exception as e:
            logger.warning("Remote create collection failed: %s", e)
        return RAGCollection(id=name, name=name, provider_id=provider.id)

    def _list_remote_collections(self, provider: RAGProviderEntry) -> list[RAGCollection]:
        import requests
        url = f"{provider.base_url}/api/rag/collections"
        try:
            resp = requests.get(url, headers=self._remote_headers(provider), timeout=10)
            if resp.status_code == 200:
                return [
                    RAGCollection(id=c.get("id", ""), name=c.get("name", ""),
                                  provider_id=provider.id,
                                  doc_count=c.get("doc_count", 0))
                    for c in resp.json().get("collections", [])
                ]
        except Exception:
            pass
        return []

    def _delete_remote_collection(self, provider: RAGProviderEntry,
                                  name: str) -> bool:
        import requests
        url = f"{provider.base_url}/api/rag/collection/delete"
        try:
            resp = requests.post(url, json={"name": name},
                                 headers=self._remote_headers(provider), timeout=10)
            return resp.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------
_registry: RAGProviderRegistry | None = None


def get_rag_registry() -> RAGProviderRegistry:
    global _registry
    if _registry is None:
        _registry = RAGProviderRegistry()
    return _registry


# ---------------------------------------------------------------------------
# High-level search for agents (routes by agent profile)
# ---------------------------------------------------------------------------

def search_for_agent(agent_profile, query: str, agent_id: str = "",
                     top_k: int = 5) -> list[dict]:
    """Search RAG knowledge base(s) according to agent's rag_mode.

    Args:
        agent_profile: AgentProfile instance (has rag_mode, rag_provider_id, etc.)
        query: Search query
        agent_id: Agent ID (for private collection naming)
        top_k: Max results

    Returns:
        List of result dicts [{id, title, content, distance, metadata}]
    """
    reg = get_rag_registry()
    rag_mode = getattr(agent_profile, "rag_mode", "shared")
    provider_id = getattr(agent_profile, "rag_provider_id", "") or ""
    extra_collections = getattr(agent_profile, "rag_collection_ids", []) or []

    results: list[RAGResult] = []

    if rag_mode == "none":
        return []

    # Helper: hybrid_search with graceful fallback to pure vector.
    # Used for "专 (domain)" collections where BM25 adds value.
    def _domain_search(p_id: str, coll: str) -> list[RAGResult]:
        if hasattr(reg, "hybrid_search"):
            try:
                return reg.hybrid_search(p_id, coll, query, top_k=top_k)
            except Exception:
                pass
        return reg.search(p_id, coll, query, top_k)

    if rag_mode in ("private", "both"):
        # 专属领域 KB → hybrid (BM25 + vector + RRF)
        dkb_store = get_domain_kb_store()
        for kb_id in extra_collections:
            kb = dkb_store.get(kb_id)
            if kb:
                kb_provider = kb.provider_id or provider_id
                results.extend(_domain_search(kb_provider, kb.collection))
            else:
                # Unknown kb_id — treat as raw collection name (backward compat)
                results.extend(_domain_search(provider_id, kb_id))
        # Legacy advisor_{agent_id} collection — also treat as domain-class.
        if agent_id:
            try:
                results.extend(_domain_search(provider_id, f"advisor_{agent_id}"))
            except Exception:
                pass

    if rag_mode in ("shared", "both"):
        # 共享池 "knowledge" → 保持 pure vector (条目短、碎，BM25 加成有限)
        results.extend(reg.search(provider_id, "knowledge", query, top_k))

    # Deduplicate by ID and sort by distance
    seen = set()
    unique = []
    for r in results:
        if r.id not in seen:
            seen.add(r.id)
            unique.append(r)
    unique.sort(key=lambda r: r.distance)

    return [r.to_dict() for r in unique[:top_k]]
