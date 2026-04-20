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
    """Add a new knowledge entry."""
    try:
        title = body.get("title", "").strip()
        content = body.get("content", "").strip()
        tags = body.get("tags", [])
        if not title or not content:
            raise HTTPException(400, "title and content are required")

        try:
            from ...core.memory import get_knowledge_manager
            knowledge = get_knowledge_manager()
            entry = knowledge.add_entry(title, content, tags)
        except (ImportError, Exception):
            entry = hub.add_knowledge_entry(body) if hasattr(hub, "add_knowledge_entry") else {"title": title}

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
    """Update a knowledge entry by id."""
    try:
        title = body.get("title")
        content = body.get("content")
        tags = body.get("tags")

        try:
            from ...core.memory import get_knowledge_manager
            knowledge = get_knowledge_manager()
            entry = knowledge.update_entry(entry_id, title=title, content=content, tags=tags)
        except (ImportError, Exception):
            entry = hub.update_knowledge_entry(entry_id, body) if hasattr(hub, "update_knowledge_entry") else None

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
    """Delete a knowledge entry by id."""
    try:
        try:
            from ...core.memory import get_knowledge_manager
            knowledge = get_knowledge_manager()
            ok = knowledge.delete_entry(entry_id)
        except (ImportError, Exception):
            hub.delete_knowledge_entry(entry_id) if hasattr(hub, "delete_knowledge_entry") else None
            ok = True

        return {"ok": ok}
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
    """Search using RAG."""
    try:
        query = body.get("query", "")
        if not query:
            raise HTTPException(400, "Missing query")

        provider = body.get("provider", "")
        limit = body.get("limit", 10)

        if hasattr(hub, "search_rag"):
            results = hub.search_rag(query, provider=provider, limit=limit)
            return {"results": results}

        return {"results": []}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/rag/ingest")
async def ingest_rag_documents(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Ingest documents into RAG system."""
    try:
        provider = body.get("provider", "")
        documents = body.get("documents", [])
        if not documents:
            raise HTTPException(400, "Missing documents")

        if hasattr(hub, "ingest_rag_documents"):
            result = hub.ingest_rag_documents(provider, documents)
            return {"ok": True, "result": result}

        return {"ok": True}
    except HTTPException:
        raise
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
        kbs = hub.list_domain_knowledge_bases() if hasattr(hub, "list_domain_knowledge_bases") else []
        kbs_list = [k.to_dict() if hasattr(k, "to_dict") else k for k in kbs]
        return {"knowledge_bases": kbs_list}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/domain-kb/create")
async def create_domain_knowledge_base(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Create a domain knowledge base."""
    try:
        name = body.get("name", "")
        if not name:
            raise HTTPException(400, "Missing name")

        if hasattr(hub, "create_domain_knowledge_base"):
            kb = hub.create_domain_knowledge_base(body)
            return {"ok": True, "knowledge_base": kb}

        return {"ok": True}
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
        results = get_rag_registry().search(kb.provider_id, kb.collection, query, top_k)
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
        text = raw_content.strip()
        chunks = []
        paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
        current_chunk = ""
        chunk_idx = 0
        for para in paragraphs:
            if len(current_chunk) + len(para) > chunk_size and current_chunk:
                chunk_idx += 1
                chunks.append({
                    "id": f"dkb_{kb.id}_{chunk_idx:04d}",
                    "title": f"{title} (Part {chunk_idx})",
                    "content": current_chunk.strip(),
                    "tags": tags,
                    "source": "domain_import",
                })
                current_chunk = para
            else:
                current_chunk += ("\n\n" + para) if current_chunk else para
        if current_chunk.strip():
            chunk_idx += 1
            chunks.append({
                "id": f"dkb_{kb.id}_{chunk_idx:04d}",
                "title": f"{title} (Part {chunk_idx})" if chunk_idx > 1 else title,
                "content": current_chunk.strip(),
                "tags": tags,
                "source": "domain_import",
            })
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
