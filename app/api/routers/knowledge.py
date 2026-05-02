"""Knowledge management and RAG router — knowledge base, search, RAG providers."""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Body

from ..deps.hub import get_hub
from ..deps.auth import CurrentUser, get_current_user

logger = logging.getLogger("tudouclaw.api.knowledge")

router = APIRouter(prefix="/api/portal", tags=["knowledge"])


# ---------------------------------------------------------------------------
# Knowledge base listing and management
# ---------------------------------------------------------------------------

@router.get("/knowledge")
async def list_knowledge_entries(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List knowledge base entries — matches legacy portal_routes_get."""
    try:
        from ... import knowledge
        entries = knowledge.list_entries()
        return {"entries": entries}
    except (ImportError, Exception) as e:
        return {"entries": []}


@router.get("/knowledge/search")
async def search_knowledge(
    q: str = Query("", description="Search query"),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Search knowledge base — matches legacy portal_routes_get."""
    try:
        if not q:
            return {"entries": []}
        from ... import knowledge
        entries = knowledge.search(q)
        return {"entries": entries}
    except (ImportError, Exception) as e:
        return {"entries": []}


@router.post("/knowledge")
async def add_knowledge_entry(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Add a knowledge entry to the shared Knowledge Wiki.

    Bug fix (Nov 2026): the old implementation imported a non-existent
    ``core.memory.get_knowledge_manager``, caught the ImportError with
    a too-broad ``except (ImportError, Exception)``, then fell back to
    a non-existent ``hub.add_knowledge_entry`` method, and returned a
    stub ``{"title": title}`` — UI reported success but nothing was
    persisted. Now routed directly through the real module-level API
    in ``app.knowledge``.
    """
    try:
        title = body.get("title", "").strip()
        content = body.get("content", "").strip()
        tags = body.get("tags", []) or []
        if not title or not content:
            raise HTTPException(400, "title and content are required")
        if not isinstance(tags, list):
            tags = []
        from ... import knowledge as _kb
        entry = _kb.add_entry(title, content, tags)
        return {"ok": True, "entry": entry}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/knowledge/{entry_id}")
async def update_knowledge_entry(
    entry_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update a knowledge entry by id. Same bug fix as add_knowledge_entry."""
    try:
        title = body.get("title")
        content = body.get("content")
        tags = body.get("tags")
        if tags is not None and not isinstance(tags, list):
            tags = None
        from ... import knowledge as _kb
        entry = _kb.update_entry(entry_id, title=title,
                                  content=content, tags=tags)
        if entry:
            return {"ok": True, "entry": entry}
        raise HTTPException(404, "Entry not found")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/knowledge/{entry_id}/delete")
async def delete_knowledge_entry(
    entry_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Delete a knowledge entry by id. Returns 404 if entry not found."""
    try:
        from ... import knowledge as _kb
        ok = _kb.delete_entry(entry_id)
        if not ok:
            raise HTTPException(404, f"Entry '{entry_id}' not found")
        return {"ok": True, "deleted_id": entry_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# RAG provider management
# ---------------------------------------------------------------------------

@router.get("/rag/providers")
async def list_rag_providers(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List available RAG providers — matches legacy portal_routes_get."""
    try:
        from ...rag_provider import get_rag_registry
        reg = get_rag_registry()
        return {"providers": [p.to_dict() for p in reg.list_providers()]}
    except (ImportError, Exception):
        return {"providers": []}


# ---------------------------------------------------------------------------
# RAG provider management (register, update, delete, rebuild)
# ---------------------------------------------------------------------------

@router.post("/rag/providers")
async def register_rag_provider(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Register a new RAG provider."""
    try:
        from ...rag_provider import get_rag_registry
        reg = get_rag_registry()
        entry = reg.register(
            name=body.get("name", ""),
            kind=body.get("kind", "remote"),
            base_url=body.get("base_url", ""),
            api_key=body.get("api_key", ""),
            config=body.get("config", {}),
        )
        return entry.to_dict() if hasattr(entry, "to_dict") else {"ok": True, "entry": entry}
    except ImportError:
        raise HTTPException(501, "RAG provider module not available")
    except ValueError as e:
        # register() rejects ghost entries (empty remote name+url). That
        # is a client error, not a server error — surface as 400 so the
        # Portal form can show the validation message.
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rag/providers/{provider_id}/update")
async def update_rag_provider(
    provider_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update an existing RAG provider."""
    try:
        from ...rag_provider import get_rag_registry
        reg = get_rag_registry()
        kwargs = {k: v for k, v in body.items() if k != "provider_id"}
        entry = reg.update(provider_id, **kwargs)
        if entry:
            return entry.to_dict() if hasattr(entry, "to_dict") else {"ok": True}
        raise HTTPException(404, "Provider not found")
    except HTTPException:
        raise
    except ImportError:
        raise HTTPException(501, "RAG provider module not available")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rag/providers/{provider_id}/delete")
async def delete_rag_provider(
    provider_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Delete a RAG provider."""
    try:
        from ...rag_provider import get_rag_registry
        reg = get_rag_registry()
        ok = reg.remove(provider_id)
        return {"ok": ok}
    except ImportError:
        raise HTTPException(501, "RAG provider module not available")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rag/providers/{provider_id}/rebuild")
async def rebuild_rag_provider(
    provider_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Rebuild/reindex a RAG provider's data."""
    try:
        from ...rag_provider import get_rag_registry
        reg = get_rag_registry()
        provider = reg.get(provider_id) if hasattr(reg, "get") else None
        if not provider:
            raise HTTPException(404, "Provider not found")
        if hasattr(reg, "rebuild"):
            result = reg.rebuild(provider_id)
            return {"ok": True, "result": result}
        if hasattr(reg, "reindex"):
            result = reg.reindex(provider_id)
            return {"ok": True, "result": result}
        # Fallback: re-list collections to confirm provider is reachable
        colls = reg.list_collections(provider_id) if hasattr(reg, "list_collections") else []
        return {
            "ok": True,
            "message": "provider reachable, no explicit rebuild needed",
            "collections": len(colls),
        }
    except HTTPException:
        raise
    except ImportError:
        raise HTTPException(501, "RAG provider module not available")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rag/index")
async def rag_index(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Index documents into RAG (batch indexing)."""
    try:
        provider_id = body.get("provider_id", "")
        collection = body.get("collection", "knowledge")
        documents = body.get("documents", [])
        if not documents:
            raise HTTPException(400, "Missing documents")

        from ...rag_provider import get_rag_registry
        reg = get_rag_registry()
        count = reg.ingest(
            provider_id=provider_id,
            collection=collection,
            documents=documents,
        )
        return {"ok": True, "count": count}
    except HTTPException:
        raise
    except ImportError:
        raise HTTPException(501, "RAG provider module not available")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# RAG search and ingestion
# ---------------------------------------------------------------------------

@router.post("/rag/search")
async def search_rag(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Search using RAG.

    Bug fix (Nov 2026): old code checked ``hasattr(hub, 'search_rag')``
    — hub has no such method, so the endpoint silently returned
    ``{"results": []}`` for every query. Now routes through the real
    RAG provider registry.
    """
    try:
        query = (body.get("query") or "").strip()
        if not query:
            raise HTTPException(400, "query is required")
        provider = body.get("provider", "") or ""
        collection = body.get("collection", "") or ""
        if not collection:
            raise HTTPException(400, "collection is required")
        limit = int(body.get("limit", 10) or 10)

        from ...rag_provider import get_rag_registry
        results = get_rag_registry().search(
            provider, collection, query, top_k=limit,
        )
        return {
            "ok": True,
            "results": [
                r.to_dict() if hasattr(r, "to_dict") else r
                for r in (results or [])
            ],
            "count": len(results or []),
        }
    except HTTPException:
        raise
    except ImportError:
        raise HTTPException(501, "RAG provider module not available")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rag/ingest")
async def ingest_rag_documents(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Ingest documents into RAG system.

    Bug fix (Nov 2026): same silent-success pattern as /rag/search.
    Now routes through the real registry.
    """
    try:
        provider = body.get("provider", "") or ""
        collection = body.get("collection", "") or ""
        documents = body.get("documents", []) or []
        if not documents or not isinstance(documents, list):
            raise HTTPException(400, "documents must be a non-empty list")
        if not collection:
            raise HTTPException(400, "collection is required")

        from ...rag_provider import get_rag_registry
        count = get_rag_registry().ingest(provider, collection, documents)
        return {
            "ok": True,
            "ingested": count,
            "requested": len(documents),
        }
    except HTTPException:
        raise
    except ImportError:
        raise HTTPException(501, "RAG provider module not available")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Domain knowledge base management
# ---------------------------------------------------------------------------

@router.post("/domain-kb/list")
async def list_domain_knowledge_bases(
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List domain knowledge bases."""
    try:
        # Prefer the hub convenience if present; otherwise go direct to
        # the store. The hub method was never fully wired up, and silent
        # `return []` here matched the silent `return {"ok": True}` in
        # create() below → users thought the feature was broken.
        if hasattr(hub, "list_domain_knowledge_bases"):
            kbs = hub.list_domain_knowledge_bases()
        else:
            from ...rag_provider import get_domain_kb_store
            kbs = get_domain_kb_store().list_all()
        kbs_list = [k.to_dict() if hasattr(k, "to_dict") else k for k in kbs]
        return {"knowledge_bases": kbs_list}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# Allow-list of embedding models the user can pick at KB-creation time.
# Each entry: (model_id, dim, ~download_size, one-line tag for the UI).
# Adding a new option requires verifying SentenceTransformer can load it
# offline / via HF Hub. Empty model_id = use server default.
ALLOWED_EMBEDDING_MODELS = [
    {"id": "", "label": "默认 (服务器配置)", "dim": None, "size_mb": None,
     "note": "fallback to DEFAULT_EMBEDDING_MODEL"},
    {"id": "all-MiniLM-L6-v2", "label": "all-MiniLM-L6-v2", "dim": 384, "size_mb": 80,
     "note": "英文为主, 体积小, 速度快"},
    {"id": "BAAI/bge-m3", "label": "BAAI/bge-m3", "dim": 1024, "size_mb": 2300,
     "note": "多语言, 召回最强 (推荐), 首次下载较慢"},
    {"id": "BAAI/bge-large-zh-v1.5", "label": "BAAI/bge-large-zh-v1.5", "dim": 1024, "size_mb": 1300,
     "note": "中文优化"},
    {"id": "paraphrase-multilingual-MiniLM-L12-v2",
     "label": "paraphrase-multilingual-MiniLM-L12-v2", "dim": 384, "size_mb": 470,
     "note": "多语言 + 体积小"},
]
_ALLOWED_MODEL_IDS = {m["id"] for m in ALLOWED_EMBEDDING_MODELS}


@router.get("/domain-kb/embedding-models")
async def list_embedding_models(user: CurrentUser = Depends(get_current_user)):
    """Catalog of selectable embedding models for KB creation."""
    return {"models": ALLOWED_EMBEDDING_MODELS}


@router.post("/domain-kb/create")
async def create_domain_knowledge_base(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Create a domain knowledge base.

    Bug fix: the old implementation only created a KB when the hub
    exposed a ``create_domain_knowledge_base`` method, which it never
    did. The endpoint then silently returned ``{"ok": True}`` — UI
    reported success but nothing was persisted. Now we call the store
    directly.
    """
    try:
        name = body.get("name", "")
        if not name:
            raise HTTPException(400, "Missing name")

        description = body.get("description", "") or ""
        provider_id = body.get("provider_id", "") or ""
        embedding_model = (body.get("embedding_model") or "").strip()
        # Empty string = use server default (existing behavior). Otherwise
        # must be in the allow-list — random HF model names could be huge,
        # untrusted, or fail to load and leave the KB in a broken state.
        if embedding_model and embedding_model not in _ALLOWED_MODEL_IDS:
            raise HTTPException(
                400,
                f"embedding_model not allowed: {embedding_model!r}. "
                f"GET /domain-kb/embedding-models for the catalog.",
            )
        tags = body.get("tags") or []
        if not isinstance(tags, list):
            tags = []

        # Prefer hub method if a future version exposes it.
        if hasattr(hub, "create_domain_knowledge_base"):
            kb = hub.create_domain_knowledge_base(body)
            if hasattr(kb, "to_dict"):
                kb = kb.to_dict()
            return {"ok": True, "knowledge_base": kb}

        # Direct-to-store path — what the endpoint should have been doing
        # from day one.
        from ...rag_provider import get_domain_kb_store
        store = get_domain_kb_store()
        kb = store.create(
            name=name,
            description=description,
            provider_id=provider_id,
            tags=[str(t).strip() for t in tags if str(t).strip()],
            embedding_model=embedding_model,
        )
        return {"ok": True, "knowledge_base": kb.to_dict()}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/domain-kb/search")
async def search_domain_knowledge_base(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Search a domain knowledge base."""
    try:
        from ...rag_provider import get_domain_kb_store, get_rag_registry
        store = get_domain_kb_store()
        kb_id = body.get("kb_id", "")
        query = body.get("query", "")
        top_k = int(body.get("top_k", 5))
        if not kb_id or not query:
            raise HTTPException(400, "Missing kb_id or query")
        kb = store.get(kb_id)
        if not kb:
            raise HTTPException(404, "knowledge base not found")
        # RAG v1-B: prefer hybrid (BM25 + vector + RRF) when available.
        reg = get_rag_registry()
        if hasattr(reg, "hybrid_search"):
            try:
                results = reg.hybrid_search(
                    kb.provider_id, kb.collection, query, top_k=top_k
                )
            except Exception:
                results = reg.search(kb.provider_id, kb.collection, query, top_k)
        else:
            results = reg.search(kb.provider_id, kb.collection, query, top_k)
        return {"results": [r.to_dict() for r in results]}
    except HTTPException:
        raise
    except ImportError:
        return {"results": []}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/domain-kb/update")
async def update_domain_knowledge_base(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update a domain knowledge base metadata."""
    try:
        from ...rag_provider import get_domain_kb_store
        store = get_domain_kb_store()
        kb_id = body.get("id", "")
        kb = store.update(
            kb_id,
            name=body.get("name"),
            description=body.get("description"),
            tags=body.get("tags"),
        )
        if kb:
            return kb.to_dict()
        raise HTTPException(404, "knowledge base not found")
    except HTTPException:
        raise
    except ImportError:
        raise HTTPException(501, "RAG provider module not available")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/domain-kb/delete")
async def delete_domain_knowledge_base(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Delete a domain knowledge base."""
    try:
        from ...rag_provider import get_domain_kb_store
        store = get_domain_kb_store()
        kb_id = body.get("id", "")
        ok = store.delete(kb_id)
        return {"ok": ok}
    except ImportError:
        raise HTTPException(501, "RAG provider module not available")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── file parsing helper (shared by single-file & folder import) ──────
# Extensions we know how to parse. Unknown extensions are skipped with
# a warning in folder-import; for single-file they fall through to
# best-effort text decode.
_KNOWN_KB_EXTENSIONS: tuple[str, ...] = (
    "txt", "md", "markdown", "rst", "log",
    "csv", "tsv",
    "json", "yaml", "yml",
    "html", "htm",
    "pdf", "docx",
    "py", "js", "ts", "go", "rs", "java", "c", "cpp", "h",
)


def _parse_file_bytes_to_text(raw: bytes, file_name: str) -> tuple[str, str]:
    """Shared file-to-text logic.

    Returns (text, method). Best-effort — always returns SOMETHING
    (may be raw-decoded) rather than raising, so folder walks don't
    abort on one bad file.
    """
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "txt"
    text = ""
    method = "raw"
    if ext == "pdf":
        try:
            import pdfplumber  # type: ignore
            import io as _io
            with pdfplumber.open(_io.BytesIO(raw)) as pdf:
                text = "\n\n".join(p.extract_text() or "" for p in pdf.pages)
            method = "pdfplumber"
        except Exception:
            try:
                import fitz  # type: ignore
                doc = fitz.open(stream=raw, filetype="pdf")
                text = "\n\n".join(page.get_text() for page in doc)
                method = "pymupdf"
            except Exception:
                text = raw.decode("utf-8", errors="replace")
                method = "raw_decode"
    elif ext == "docx":
        try:
            import docx  # type: ignore
            import io as _io
            doc = docx.Document(_io.BytesIO(raw))
            parts = []
            for para in doc.paragraphs:
                if para.text.strip():
                    parts.append(para.text)
            for table in doc.tables:
                for row in table.rows:
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        parts.append(" | ".join(cells))
            text = "\n\n".join(parts)
            method = "python-docx"
        except Exception:
            text = raw.decode("utf-8", errors="replace")
            method = "raw_decode"
    elif ext in ("html", "htm"):
        try:
            from bs4 import BeautifulSoup  # type: ignore
            soup = BeautifulSoup(raw, "html.parser")
            text = soup.get_text(separator="\n\n", strip=True)
            method = "beautifulsoup"
        except Exception:
            import re as _re
            text = _re.sub(r"<[^>]+>",
                           "",
                           raw.decode("utf-8", errors="replace"))
            method = "regex_strip"
    else:
        for enc in ("utf-8", "gbk", "gb2312", "latin-1"):
            try:
                text = raw.decode(enc)
                method = enc
                break
            except (UnicodeDecodeError, LookupError):
                continue
        if not text:
            text = raw.decode("utf-8", errors="replace")
            method = "utf8_replace"
    return text, method


# ── RAG v1-A: heading-aware recursive chunker ────────────────────────
# Splits in priority order:
#   1. Markdown headings (# / ## / ### / ...)  — strongest boundary
#   2. Double-newline paragraphs
#   3. Sentence terminators (. / 。 / ! / ？ etc.)
#   4. Raw character slice (last resort, should rarely fire)
# Adds 15% overlap between adjacent chunks to keep cross-boundary context.
# Preserves the heading path ("第一章 / 第二节") on each chunk's metadata
# so retrieval can show WHERE the chunk lives in the source.

import re as _re_rag
import hashlib as _hashlib_rag
import time

_HEADING_RE = _re_rag.compile(r"^(#{1,6})\s+(.+?)\s*$", _re_rag.MULTILINE)
# Sentence terminators — handles both English (.!?) and CJK (。！？).
# We split on the terminator but keep it attached to the left side so
# sentences round-trip correctly.
_SENTENCE_RE = _re_rag.compile(r"(?<=[.!?。！？])\s+")

_CHUNK_OVERLAP_RATIO = 0.15


def _split_by_headings(text: str) -> list[tuple[str, str]]:
    """Split text at Markdown heading boundaries.

    Returns a list of (heading_path, section_body) pairs. Text before
    the first heading gets heading_path="" (preamble). Nested headings
    build a breadcrumb path like "第一章 / 第二节".
    """
    lines = text.splitlines()
    sections: list[tuple[str, str]] = []
    stack: list[tuple[int, str]] = []        # [(level, title), ...]
    current_body: list[str] = []
    current_path = ""

    def _path_of_stack() -> str:
        return " / ".join(t for _l, t in stack)

    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            # Flush current section.
            body = "\n".join(current_body).strip()
            if body:
                sections.append((current_path, body))
            current_body = []
            level = len(m.group(1))
            title = m.group(2).strip()
            # Pop to level - 1, then push this.
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            current_path = _path_of_stack()
        else:
            current_body.append(line)
    # Flush tail.
    body = "\n".join(current_body).strip()
    if body:
        sections.append((current_path, body))
    # If no headings at all, return single section with empty path.
    if not sections:
        sections = [("", text.strip())] if text.strip() else []
    return sections


def _split_by_paragraphs(text: str) -> list[str]:
    return [p.strip() for p in text.split("\n\n") if p.strip()]


def _split_by_sentences(text: str) -> list[str]:
    parts = _SENTENCE_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def _force_char_slice(text: str, size: int) -> list[str]:
    """Last-resort slicer for a single unit that's too big."""
    return [text[i:i + size] for i in range(0, len(text), size) if text[i:i + size].strip()]


def _recursive_split(body: str, chunk_size: int) -> list[str]:
    """Hierarchical split: paragraphs → sentences → char. Returns
    chunk-body strings sized at or below ``chunk_size`` (overlap added
    later by the caller)."""
    body = (body or "").strip()
    if not body:
        return []
    if len(body) <= chunk_size:
        return [body]

    out: list[str] = []
    # Level 1: paragraphs.
    paragraphs = _split_by_paragraphs(body)
    buf = ""
    for para in paragraphs:
        if len(para) > chunk_size:
            # Flush pending buffer first.
            if buf.strip():
                out.append(buf.strip())
                buf = ""
            # Level 2: sentences inside the oversized paragraph.
            sentences = _split_by_sentences(para)
            sbuf = ""
            for sent in sentences:
                if len(sent) > chunk_size:
                    # Flush sentence buffer.
                    if sbuf.strip():
                        out.append(sbuf.strip())
                        sbuf = ""
                    # Level 3: force-slice the giant sentence.
                    for piece in _force_char_slice(sent, chunk_size):
                        out.append(piece.strip())
                elif len(sbuf) + len(sent) + 1 > chunk_size and sbuf:
                    out.append(sbuf.strip())
                    sbuf = sent
                else:
                    sbuf += (" " + sent) if sbuf else sent
            if sbuf.strip():
                out.append(sbuf.strip())
        elif len(buf) + len(para) + 2 > chunk_size and buf:
            out.append(buf.strip())
            buf = para
        else:
            buf += ("\n\n" + para) if buf else para
    if buf.strip():
        out.append(buf.strip())
    return out


def _apply_overlap(pieces: list[str], chunk_size: int,
                   ratio: float = _CHUNK_OVERLAP_RATIO) -> list[str]:
    """Prepend a tail slice of the previous chunk onto each chunk after
    the first, to preserve cross-boundary context during retrieval.
    Overlap size = ratio * chunk_size, capped by the previous chunk's
    actual length."""
    if not pieces or len(pieces) == 1:
        return list(pieces)
    overlap_chars = max(0, int(chunk_size * ratio))
    if overlap_chars == 0:
        return list(pieces)
    out: list[str] = [pieces[0]]
    for i in range(1, len(pieces)):
        prev = pieces[i - 1]
        tail = prev[-overlap_chars:] if len(prev) > overlap_chars else prev
        out.append(f"…{tail}\n\n{pieces[i]}")
    return out


def _chunk_text_for_rag(text: str, base_id: str, base_title: str,
                        tags: list, chunk_size: int,
                        source: str = "domain_import",
                        source_file: str = "") -> list[dict]:
    """Heading-aware recursive chunker with overlap.

    Emits chunk dicts ready for RAG ingest. Each chunk's metadata carries
    (v1-C):
      * content_hash  — SHA-256 of content (enables dedup)
      * heading_path  — breadcrumb of Markdown headings leading into it
      * source_file   — relative path, if caller passed it
      * chunk_index   — ordinal within this document
      * imported_at   — unix timestamp
    """
    text = (text or "").strip()
    if not text:
        return []

    now = time.time()
    sections = _split_by_headings(text)

    chunks: list[dict] = []
    chunk_idx = 0
    for heading_path, body in sections:
        if not body.strip():
            continue
        pieces = _recursive_split(body, chunk_size)
        pieces = _apply_overlap(pieces, chunk_size)
        for piece in pieces:
            if not piece.strip():
                continue
            chunk_idx += 1
            content = piece.strip()
            title_suffix = f" · {heading_path}" if heading_path else ""
            display_title = (
                base_title + title_suffix
                + (f" (Part {chunk_idx})" if chunk_idx > 1 else "")
            ) if chunk_idx > 1 else (base_title + title_suffix)
            content_hash = _hashlib_rag.sha256(
                content.encode("utf-8", errors="replace"),
            ).hexdigest()
            chunks.append({
                "id": f"{base_id}_{chunk_idx:04d}",
                "title": display_title,
                "content": content,
                "tags": list(tags or []),
                "source": source,
                # v1-C metadata fields; _ingest_local flattens these
                # into ChromaDB metadata via the `metadata` subdict.
                "content_hash": content_hash,
                "heading_path": heading_path or "",
                "source_file": source_file or "",
                "chunk_index": chunk_idx,
                "imported_at": now,
            })
    return chunks


@router.post("/domain-kb/import")
async def import_domain_knowledge(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Import content into a domain knowledge base."""
    try:
        from ...rag_provider import get_domain_kb_store, get_rag_registry
        store = get_domain_kb_store()
        kb_id = body.get("kb_id", "")
        kb = store.get(kb_id)
        if not kb:
            raise HTTPException(404, "knowledge base not found")
        raw_content = body.get("content", "")
        title = body.get("title", "Imported")
        tags = body.get("tags", [])
        chunk_size = int(body.get("chunk_size", 1000))
        if not raw_content.strip():
            raise HTTPException(400, "content is required")
        chunks = _chunk_text_for_rag(
            raw_content, base_id=f"dkb_{kb.id}",
            base_title=title, tags=tags, chunk_size=chunk_size,
        )
        if not chunks:
            return {"ok": True, "count": 0, "chunks": 0}
        count = get_rag_registry().ingest(kb.provider_id, kb.collection, chunks)
        store.increment_doc_count(kb_id, len(chunks))
        return {"ok": True, "count": count, "chunks": len(chunks)}
    except HTTPException:
        raise
    except ImportError:
        raise HTTPException(501, "RAG provider module not available")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/domain-kb/import-files")
async def import_files_into_domain_kb(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Browser-side folder import: accepts a LIST of base64-encoded files.

    The portal's new folder picker enumerates local files via
    ``<input webkitdirectory>``, reads each as base64, and batches them
    into this endpoint. No server-side path is required — the folder
    can live on the user's laptop.

    Body::

        {
          "kb_id":      "dkb_abc",
          "files": [
              {"name": "docs/a.md",   "data_base64": "..."},
              {"name": "docs/b.pdf",  "data_base64": "..."}
          ],
          "tags":       ["legal"],
          "chunk_size": 1000,
          "max_file_size_mb": 20
        }

    Returns the same shape as /domain-kb/import-folder so the UI can
    share a single result-renderer.
    """
    import base64 as _base64
    try:
        from ...rag_provider import get_domain_kb_store, get_rag_registry
        store = get_domain_kb_store()
        kb_id = (body.get("kb_id") or "").strip()
        if not kb_id:
            raise HTTPException(400, "kb_id is required")
        kb = store.get(kb_id)
        if not kb:
            raise HTTPException(404, "knowledge base not found")

        files = body.get("files") or []
        if not isinstance(files, list) or not files:
            raise HTTPException(400, "files must be a non-empty list")

        tags = body.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        chunk_size = int(body.get("chunk_size", 1000) or 1000)
        max_file_size_mb = float(body.get("max_file_size_mb", 20) or 20)
        max_bytes = int(max_file_size_mb * 1024 * 1024)

        by_file: list[dict] = []
        skipped: list[dict] = []
        all_chunks: list[dict] = []
        files_imported = 0
        files_scanned = 0

        for f in files:
            if not isinstance(f, dict):
                skipped.append({"name": "", "reason": "not_a_dict"})
                continue
            name = (f.get("name") or "").strip()
            data_b64 = f.get("data_base64") or ""
            if not name:
                skipped.append({"name": "", "reason": "missing_name"})
                continue
            if not data_b64:
                skipped.append({"name": name, "reason": "empty_data"})
                continue
            files_scanned += 1
            try:
                raw = _base64.b64decode(data_b64)
            except Exception as e:
                skipped.append({"name": name,
                                "reason": f"base64_decode_failed: {e}"})
                continue
            if not raw:
                skipped.append({"name": name, "reason": "empty"})
                continue
            if len(raw) > max_bytes:
                skipped.append({
                    "name": name,
                    "reason": f"too_large ({len(raw) // (1024*1024)}MB > "
                              f"{max_file_size_mb}MB)",
                })
                continue
            # Parse to text (reuses folder-import helper).
            text, method = _parse_file_bytes_to_text(raw, name)
            if not text or not text.strip():
                skipped.append({"name": name, "reason": "parse_empty",
                                "method": method})
                continue
            # Title/base_id derived from the client-supplied relative name.
            import hashlib as _hl
            base_id = (f"dkb_{kb.id}_u"
                       + _hl.md5(name.encode()).hexdigest()[:10])
            chunks = _chunk_text_for_rag(
                text, base_id=base_id, base_title=name,
                tags=tags, chunk_size=chunk_size,
                source=f"domain_import_upload:{name}",
                source_file=name,
            )
            if not chunks:
                skipped.append({"name": name, "reason": "no_chunks"})
                continue
            all_chunks.extend(chunks)
            files_imported += 1
            by_file.append({
                "name": name,
                "chunks": len(chunks),
                "method": method,
                "size_bytes": len(raw),
                "ok": True,
            })

        if not all_chunks:
            return {
                "ok": True,
                "kb_id": kb_id,
                "files_scanned": files_scanned,
                "files_imported": 0,
                "files_skipped": len(skipped),
                "chunks_total": 0,
                "ingest_count": 0,
                "by_file": by_file,
                "skipped": skipped,
                "message": "No files ingested — see skipped for reasons.",
            }

        # Dedup by content_hash within THIS request (same file uploaded
        # twice gets one copy). Cross-request dedup kicks in at the
        # ingest layer when we wire v1-C up; for now it's upsert-by-id
        # in ChromaDB.
        seen_hashes: set[str] = set()
        deduped: list[dict] = []
        for c in all_chunks:
            h = c.get("content_hash", "")
            if h and h in seen_hashes:
                continue
            if h:
                seen_hashes.add(h)
            deduped.append(c)

        ingest_count = get_rag_registry().ingest(
            kb.provider_id, kb.collection, deduped,
        )
        store.increment_doc_count(kb_id, len(deduped))

        return {
            "ok": True,
            "kb_id": kb_id,
            "files_scanned": files_scanned,
            "files_imported": files_imported,
            "files_skipped": len(skipped),
            "chunks_total": len(deduped),
            "chunks_deduped": len(all_chunks) - len(deduped),
            "ingest_count": ingest_count,
            "by_file": by_file,
            "skipped": skipped,
        }
    except HTTPException:
        raise
    except ImportError:
        raise HTTPException(501, "RAG provider module not available")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/domain-kb/import-folder")
async def import_folder_into_domain_kb(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Bulk-import every file in a server-local folder into a domain KB.

    Body::

        {
          "kb_id":      "dkb_abc",
          "folder":     "/absolute/path/to/docs",
          "recursive":  true,                    # walk subfolders (default true)
          "extensions": ["pdf","docx","md","txt","html"],  # default = _KNOWN_KB_EXTENSIONS
          "tags":       ["legal", "2026"],       # per-chunk tag list
          "chunk_size": 1000,                    # chars per chunk
          "max_files":  500,                     # safety cap (default 500)
          "max_file_size_mb": 20,                # skip anything bigger (default 20MB)
        }

    Returns::

        {
          "ok": true,
          "kb_id": "dkb_abc",
          "files_scanned":  17,
          "files_imported": 15,
          "files_skipped":  2,     // details in `skipped`
          "chunks_total":   312,
          "ingest_count":   312,
          "by_file": [{"path": ..., "chunks": N, "method": "...", "ok": true}, ...],
          "skipped": [{"path": ..., "reason": "..."}, ...],
        }
    """
    import os as _os
    try:
        from ...rag_provider import get_domain_kb_store, get_rag_registry
        store = get_domain_kb_store()

        kb_id = (body.get("kb_id") or "").strip()
        folder = (body.get("folder") or "").strip()
        if not kb_id:
            raise HTTPException(400, "kb_id is required")
        if not folder:
            raise HTTPException(400, "folder is required")
        kb = store.get(kb_id)
        if not kb:
            raise HTTPException(404, "knowledge base not found")
        if not _os.path.isdir(folder):
            raise HTTPException(400, f"folder does not exist or is not a directory: {folder}")

        recursive = bool(body.get("recursive", True))
        # Normalize extensions list → lowercase, strip leading dots.
        exts_raw = body.get("extensions")
        if exts_raw is None:
            exts_set = set(_KNOWN_KB_EXTENSIONS)
        elif isinstance(exts_raw, list) and exts_raw:
            exts_set = {
                str(x).strip().lstrip(".").lower()
                for x in exts_raw if str(x).strip()
            }
        else:
            exts_set = set(_KNOWN_KB_EXTENSIONS)

        tags = body.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        chunk_size = int(body.get("chunk_size", 1000) or 1000)
        max_files = int(body.get("max_files", 500) or 500)
        max_file_size_mb = float(body.get("max_file_size_mb", 20) or 20)
        max_bytes = int(max_file_size_mb * 1024 * 1024)

        # Walk folder.
        targets: list[str] = []
        if recursive:
            for dirpath, _dirs, filenames in _os.walk(folder):
                for fn in filenames:
                    targets.append(_os.path.join(dirpath, fn))
        else:
            for fn in _os.listdir(folder):
                full = _os.path.join(folder, fn)
                if _os.path.isfile(full):
                    targets.append(full)
        targets.sort()

        by_file: list[dict] = []
        skipped: list[dict] = []
        all_chunks: list[dict] = []
        files_imported = 0
        files_scanned = 0

        for path in targets:
            if len(by_file) + len(skipped) >= max_files:
                skipped.append({"path": path, "reason": "max_files_cap_reached"})
                continue
            files_scanned += 1
            ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
            if exts_set and ext not in exts_set:
                skipped.append({"path": path, "reason": f"extension_{ext}_not_in_allowlist"})
                continue
            try:
                size = _os.path.getsize(path)
            except OSError as e:
                skipped.append({"path": path, "reason": f"stat_failed: {e}"})
                continue
            if size > max_bytes:
                skipped.append({
                    "path": path,
                    "reason": f"too_large ({size // (1024*1024)}MB > {max_file_size_mb}MB)",
                })
                continue
            if size == 0:
                skipped.append({"path": path, "reason": "empty"})
                continue
            try:
                with open(path, "rb") as fh:
                    raw = fh.read()
            except OSError as e:
                skipped.append({"path": path, "reason": f"read_failed: {e}"})
                continue
            # Parse to text.
            file_name = _os.path.basename(path)
            text, method = _parse_file_bytes_to_text(raw, file_name)
            if not text or not text.strip():
                skipped.append({"path": path, "reason": "parse_empty",
                                "method": method})
                continue
            # Title: relative to folder root for readability; file_name fallback.
            try:
                rel = _os.path.relpath(path, folder)
            except ValueError:
                rel = file_name
            base_title = rel
            # base_id: path-safe, derived from the relpath hash so chunks
            # are globally unique within this KB.
            import hashlib as _hashlib
            base_id = f"dkb_{kb.id}_f{_hashlib.md5(rel.encode()).hexdigest()[:10]}"
            chunks = _chunk_text_for_rag(
                text, base_id=base_id, base_title=base_title,
                tags=tags, chunk_size=chunk_size,
                source=f"domain_import_folder:{rel}",
            )
            if not chunks:
                skipped.append({"path": path, "reason": "no_chunks_after_split"})
                continue
            all_chunks.extend(chunks)
            files_imported += 1
            by_file.append({
                "path": path,
                "relative_path": rel,
                "chunks": len(chunks),
                "method": method,
                "size_bytes": size,
                "ok": True,
            })

        if not all_chunks:
            return {
                "ok": True,
                "kb_id": kb_id,
                "files_scanned": files_scanned,
                "files_imported": 0,
                "files_skipped": len(skipped),
                "chunks_total": 0,
                "ingest_count": 0,
                "by_file": by_file,
                "skipped": skipped,
                "message": "No files ingested — see skipped for reasons.",
            }

        # Bulk ingest — one call for all chunks keeps RAG provider happy.
        ingest_count = get_rag_registry().ingest(
            kb.provider_id, kb.collection, all_chunks,
        )
        store.increment_doc_count(kb_id, len(all_chunks))

        return {
            "ok": True,
            "kb_id": kb_id,
            "files_scanned": files_scanned,
            "files_imported": files_imported,
            "files_skipped": len(skipped),
            "chunks_total": len(all_chunks),
            "ingest_count": ingest_count,
            "by_file": by_file,
            "skipped": skipped,
        }
    except HTTPException:
        raise
    except ImportError:
        raise HTTPException(501, "RAG provider module not available")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# RAG collections
# ---------------------------------------------------------------------------

@router.post("/rag/collections")
async def list_rag_collections(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List collections on a RAG provider."""
    try:
        from ...rag_provider import get_rag_registry
        provider_id = body.get("provider_id", "")
        colls = get_rag_registry().list_collections(provider_id)
        return {"collections": [c.to_dict() for c in colls]}
    except ImportError:
        raise HTTPException(501, "RAG provider module not available")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rag/collection/create")
async def create_rag_collection(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Create a new RAG collection."""
    try:
        from ...rag_provider import get_rag_registry
        coll = get_rag_registry().create_collection(
            provider_id=body.get("provider_id", ""),
            collection_name=body.get("name", ""),
            description=body.get("description", ""),
        )
        return coll.to_dict()
    except ImportError:
        raise HTTPException(501, "RAG provider module not available")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rag/parse-file")
async def parse_file_for_rag(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Parse an uploaded file (base64) into text for RAG ingestion."""
    import base64
    try:
        file_data = body.get("file_data", "")
        file_name = body.get("file_name", "unknown.txt")
        if not file_data:
            raise HTTPException(400, "file_data is required")
        raw = base64.b64decode(file_data)
        ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "txt"
        text = ""
        method = "raw"
        if ext == "pdf":
            try:
                import pdfplumber
                import io
                with pdfplumber.open(io.BytesIO(raw)) as pdf:
                    text = "\n\n".join(p.extract_text() or "" for p in pdf.pages)
                method = "pdfplumber"
            except Exception:
                try:
                    import fitz
                    doc = fitz.open(stream=raw, filetype="pdf")
                    text = "\n\n".join(page.get_text() for page in doc)
                    method = "pymupdf"
                except Exception:
                    text = raw.decode("utf-8", errors="replace")
                    method = "raw_decode"
        elif ext == "docx":
            try:
                import docx
                import io
                doc = docx.Document(io.BytesIO(raw))
                parts = []
                for para in doc.paragraphs:
                    if para.text.strip():
                        parts.append(para.text)
                for table in doc.tables:
                    for row in table.rows:
                        cells = [c.text.strip() for c in row.cells if c.text.strip()]
                        if cells:
                            parts.append(" | ".join(cells))
                text = "\n\n".join(parts)
                method = "python-docx"
            except Exception:
                text = raw.decode("utf-8", errors="replace")
                method = "raw_decode"
        elif ext in ("html", "htm"):
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(raw, "html.parser")
                text = soup.get_text(separator="\n\n", strip=True)
                method = "beautifulsoup"
            except Exception:
                import re
                text = re.sub(r"<[^>]+>", "", raw.decode("utf-8", errors="replace"))
                method = "regex_strip"
        else:
            for enc in ("utf-8", "gbk", "gb2312", "latin-1"):
                try:
                    text = raw.decode(enc)
                    method = enc
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            if not text:
                text = raw.decode("utf-8", errors="replace")
                method = "utf8_replace"
        return {"text": text, "length": len(text), "method": method, "file_name": file_name}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rag/import")
async def import_rag_content(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Import knowledge from raw text content (split into chunks)."""
    try:
        from ...rag_provider import get_rag_registry
        raw_content = body.get("content", "")
        title = body.get("title", "Imported Document")
        collection = body.get("collection", "knowledge")
        provider_id = body.get("provider_id", "")
        tags = body.get("tags", [])
        chunk_size = int(body.get("chunk_size", 1000))
        if not raw_content.strip():
            raise HTTPException(400, "content is required")
        text = raw_content.strip()
        chunks = []
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        current_chunk = ""
        chunk_idx = 0
        for para in paragraphs:
            if len(current_chunk) + len(para) > chunk_size and current_chunk:
                chunk_idx += 1
                chunks.append({
                    "id": f"import_{hash(title) % 100000:05d}_{chunk_idx:03d}",
                    "title": f"{title} (Part {chunk_idx})",
                    "content": current_chunk.strip(),
                    "tags": tags,
                    "source": "import",
                })
                current_chunk = para
            else:
                current_chunk += ("\n\n" + para) if current_chunk else para
        if current_chunk.strip():
            chunk_idx += 1
            chunks.append({
                "id": f"import_{hash(title) % 100000:05d}_{chunk_idx:03d}",
                "title": f"{title} (Part {chunk_idx})" if chunk_idx > 1 else title,
                "content": current_chunk.strip(),
                "tags": tags,
                "source": "import",
            })
        count = get_rag_registry().ingest(provider_id, collection, chunks)
        if collection == "knowledge" and not provider_id:
            try:
                from ...core.memory import get_knowledge_manager
                knowledge = get_knowledge_manager()
                for chunk in chunks:
                    knowledge.add_entry(chunk["title"], chunk["content"], tags)
            except (ImportError, Exception):
                pass
        return {"ok": True, "count": count, "chunks": len(chunks)}
    except HTTPException:
        raise
    except ImportError:
        raise HTTPException(501, "RAG provider module not available")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Vector memory management
# ---------------------------------------------------------------------------

@router.post("/vector/manage")
async def manage_vector_memory(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Vector memory management (migrate, stats)."""
    try:
        action = body.get("action", "")
        if action == "migrate":
            from ...core.memory import get_memory_manager
            mm = get_memory_manager()
            agent_id = body.get("agent_id", None)
            stats = mm.migrate_to_vector(agent_id)
            return stats
        elif action == "stats":
            from ...core.memory import get_memory_manager
            mm = get_memory_manager()
            stats = mm.get_vector_stats()
            return stats
        else:
            raise HTTPException(400, f"Unknown vector action: {action}")
    except HTTPException:
        raise
    except ImportError:
        raise HTTPException(501, "Memory manager not available")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
