"""Wiki admin router — list, view, create, edit, delete pages.

The wiki layer (``app/knowledge/wiki_store.py``) holds rich-structured
markdown pages that agents author via ``wiki_ingest`` AND that the
admin can curate via the Portal UI.

Until this router landed (2026-05-08) the only way to inspect what
agents had written was to ``ls`` the filesystem; admins couldn't
list, edit, delete or toggle ``is_valid`` from the UI. That's what
this module fixes — a small CRUD surface on top of WikiStore.

URL design
----------
Wiki pages are addressed by the triple ``(scope, kind, slug)``. To
avoid encoding-colon weirdness in path params (``role:coder`` would
need ``%3A``), all mutation endpoints take the triple in the JSON
body. Listing and reading use query strings. Same convention as the
admin endpoints in ``knowledge.py``.

Endpoints
---------
GET  /api/portal/wiki                  list pages (filters: scope, kind, domain, q)
GET  /api/portal/wiki/page             get full body for one page (scope+kind+slug)
GET  /api/portal/wiki/scopes           enumerate known scopes (global + role:*)
GET  /api/portal/wiki/stats            counts by scope / kind / domain
POST /api/portal/wiki/create           create a new page
POST /api/portal/wiki/edit             update an existing page
POST /api/portal/wiki/delete           remove a page from disk
POST /api/portal/wiki/toggle-valid     flip is_valid (admin override of decay)
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..deps.auth import CurrentUser, get_current_user
from ..deps.hub import get_hub

logger = logging.getLogger("tudouclaw.api.wiki")

router = APIRouter(prefix="/api/portal/wiki", tags=["wiki"])


# ── Helpers ───────────────────────────────────────────────────────────

def _get_store():
    """Lazy import to avoid circular deps at module load."""
    from ...knowledge.wiki_store import get_wiki_store
    return get_wiki_store()


def _page_to_summary(p) -> dict:
    """Render a WikiPage as the list-view DTO (no full body — only a
    preview, so a wiki of thousands of pages doesn't blow up the
    response). Used by GET /wiki and GET /wiki/stats.
    """
    body = p.body or ""
    preview = body[:300].replace("\n", " ")
    if len(body) > 300:
        preview += "…"
    total_uses = p.success_count + p.fail_count
    success_rate = (
        round(p.success_count / total_uses, 3) if total_uses else None
    )
    return {
        "scope": p.scope,
        "kind": p.kind,
        "slug": p.slug,
        "title": p.title,
        "preview": preview,
        "body_chars": len(body),
        "tags": list(p.tags or []),
        "sources": list(p.sources or []),
        "related": list(p.related or []),
        "created_at": p.created_at,
        "updated_at": p.updated_at,
        # Outcome counters (Phase-1 evolution)
        "success_count": p.success_count,
        "fail_count": p.fail_count,
        "applied_count": getattr(p, "applied_count", 0),
        "consecutive_fails": getattr(p, "consecutive_fails", 0),
        "last_applied_at": getattr(p, "last_applied_at", 0.0),
        "is_valid": getattr(p, "is_valid", True),
        "success_rate": success_rate,
    }


def _page_to_full(p) -> dict:
    """Like _page_to_summary but includes the full body + structured
    Gene-like fields (signals_match / preconditions / strategy /
    constraints / validation). Used by GET /wiki/page and admin edit."""
    base = _page_to_summary(p)
    base["body"] = p.body or ""
    # Gene-like structured fields (may be empty for legacy pages)
    base["signals_match"] = list(getattr(p, "signals_match", []) or [])
    base["preconditions"] = list(getattr(p, "preconditions", []) or [])
    base["strategy"] = list(getattr(p, "strategy", []) or [])
    base["constraints"] = dict(getattr(p, "constraints", {}) or {})
    base["validation"] = list(getattr(p, "validation", []) or [])
    return base


def _list_all_scopes(store) -> list[str]:
    """Enumerate scopes by scanning disk (one global + every role:*).

    Cheap O(N_scopes) — typically ≤ a dozen. Falls back to ['global']
    if the role/ dir doesn't exist yet.
    """
    out = ["global"]
    role_root = os.path.join(store._root, "role")
    if os.path.isdir(role_root):
        for r in sorted(os.listdir(role_root)):
            if os.path.isdir(os.path.join(role_root, r)):
                out.append(f"role:{r}")
    return out


def _list_all_pages(store) -> list:
    """Walk every known scope and return a flat list of WikiPage."""
    pages: list = []
    for sc in _list_all_scopes(store):
        try:
            pages.extend(store.list_pages(sc))
        except Exception as e:
            logger.warning("wiki list scope=%s failed: %s", sc, e)
    return pages


# ── List + filter ─────────────────────────────────────────────────────

@router.get("")
async def list_wiki_pages(
    scope: Optional[str] = Query(None,
        description="Filter by exact scope (e.g. 'global', 'role:pm'). "
                    "Empty = all scopes."),
    kind: Optional[str] = Query(None,
        description="Filter by kind: experience / methodology / "
                    "template / pattern / reference."),
    domain: Optional[str] = Query(None,
        description="Filter by domain tag (matches any tag substring)."),
    q: Optional[str] = Query(None,
        description="Free-text query, matched against title / tags / "
                    "body via WikiStore.search()."),
    include_invalid: bool = Query(False,
        description="If True, include pages with is_valid=False "
                    "(pages auto-decayed by 3 consecutive fails)."),
    limit: int = Query(500, ge=1, le=5000),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """List wiki pages. ``q`` runs through the same scoring path as
    agent ``knowledge_lookup`` so admin sees what agents see."""
    store = _get_store()
    if q:
        # Use the proper scored search path — but expand to all scopes
        # if no scope filter; respect kind filter.
        pages = store.search(q, scope=scope or "", kind=kind or "", limit=limit)
        # search() always honours is_valid; flip back when include_invalid
        if include_invalid:
            extra_pages: list = []
            for sc in ([scope] if scope else _list_all_scopes(store)):
                for p in store.list_pages(sc, kind=kind or ""):
                    if not p.is_valid and not any(
                        x.scope == p.scope and x.kind == p.kind and x.slug == p.slug
                        for x in pages
                    ):
                        # Only add if matches q in title or body
                        ql = q.lower()
                        if ql in (p.title or "").lower() \
                           or ql in (p.body or "").lower():
                            extra_pages.append(p)
            pages = list(pages) + extra_pages
    else:
        if scope:
            pages = store.list_pages(scope, kind=kind or "")
        else:
            pages = _list_all_pages(store)
            if kind:
                pages = [p for p in pages if p.kind == kind]
        if not include_invalid:
            pages = [p for p in pages if p.is_valid]

    if domain:
        # Domain filter: page tag list contains domain substring.
        # Once Step 3 lands a real `domains` field separate from tags,
        # update this to match `p.domains` directly.
        d_lower = domain.lower()
        pages = [
            p for p in pages
            if any(d_lower in t.lower() for t in (p.tags or []))
        ]

    out = [_page_to_summary(p) for p in pages[:limit]]
    return {"pages": out, "count": len(out), "total_returned": len(out)}


# ── Read single page ──────────────────────────────────────────────────

@router.get("/page")
async def get_wiki_page(
    scope: str = Query(..., description="'global' or 'role:<role>'"),
    kind: str = Query(..., description="experience / methodology / template / pattern / reference"),
    slug: str = Query(..., description="filename without .md"),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    store = _get_store()
    page = store.read_page(scope, kind, slug)
    if page is None:
        raise HTTPException(404, f"wiki page not found: {scope}/{kind}/{slug}")
    return _page_to_full(page)


# ── Scopes enumeration (UI dropdown) ──────────────────────────────────

@router.get("/scopes")
async def list_wiki_scopes(
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """Return all scopes that have at least one wiki page on disk —
    plus 'global' even if currently empty (so the UI dropdown always
    has it as an option)."""
    store = _get_store()
    return {"scopes": _list_all_scopes(store)}


# ── Stats / dashboard ─────────────────────────────────────────────────

@router.get("/stats")
async def wiki_stats(
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """Count breakdowns for the dashboard tile.

    Returns:
      total                — all pages on disk
      valid                — pages with is_valid=True
      invalid              — pages auto-decayed (consecutive_fails ≥ 3)
      by_scope             — {scope: count}
      by_kind              — {kind: count}
      top_applied          — top 10 pages by applied_count
      top_failing          — top 10 pages by consecutive_fails ≥ 1
    """
    store = _get_store()
    pages = _list_all_pages(store)
    by_scope: dict[str, int] = {}
    by_kind: dict[str, int] = {}
    invalid = 0
    for p in pages:
        by_scope[p.scope] = by_scope.get(p.scope, 0) + 1
        by_kind[p.kind] = by_kind.get(p.kind, 0) + 1
        if not p.is_valid:
            invalid += 1
    top_applied = sorted(
        pages,
        key=lambda p: -getattr(p, "applied_count", 0),
    )[:10]
    top_failing = sorted(
        [p for p in pages if getattr(p, "consecutive_fails", 0) >= 1],
        key=lambda p: -getattr(p, "consecutive_fails", 0),
    )[:10]
    return {
        "total": len(pages),
        "valid": len(pages) - invalid,
        "invalid": invalid,
        "by_scope": by_scope,
        "by_kind": by_kind,
        "top_applied": [_page_to_summary(p) for p in top_applied],
        "top_failing": [_page_to_summary(p) for p in top_failing],
    }


# ── Mutations ─────────────────────────────────────────────────────────

@router.post("/create")
async def create_wiki_page(
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """Create a new wiki page (admin entry point — same kinds as
    ``wiki_ingest`` allows)."""
    from ...knowledge.wiki_store import WikiPage, VALID_KINDS, slugify

    scope = (body.get("scope") or "global").strip()
    kind = (body.get("kind") or "").strip().lower()
    if kind not in VALID_KINDS:
        raise HTTPException(
            400,
            f"kind must be one of {sorted(VALID_KINDS)}, got {kind!r}",
        )
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title is required")
    body_text = (body.get("body") or "").strip()
    if not body_text:
        raise HTTPException(400, "body is required (markdown content)")

    slug = (body.get("slug") or "").strip() or slugify(title)
    tags = body.get("tags") or []
    if not isinstance(tags, list):
        raise HTTPException(400, "tags must be a list of strings")

    store = _get_store()
    if store.read_page(scope, kind, slug) is not None:
        raise HTTPException(
            409,
            f"page already exists: {scope}/{kind}/{slug}. "
            f"Use POST /wiki/edit to update.",
        )
    page = WikiPage(
        scope=scope, kind=kind, slug=slug,
        title=title, body=body_text,
        tags=[str(t).strip() for t in tags if str(t).strip()],
        sources=list(body.get("sources") or []),
        related=list(body.get("related") or []),
    )
    store.write_page(page, log_action="ingest")
    logger.info("wiki page created: %s/%s/%s by %s",
                scope, kind, slug, user.username)
    return {"ok": True, "page": _page_to_full(page)}


@router.post("/edit")
async def edit_wiki_page(
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """Edit an existing wiki page. The (scope, kind, slug) triple
    identifies the page; remaining fields are optional and only
    overwrite when present in the request."""
    scope = (body.get("scope") or "").strip()
    kind = (body.get("kind") or "").strip().lower()
    slug = (body.get("slug") or "").strip()
    if not (scope and kind and slug):
        raise HTTPException(400, "scope, kind, slug all required")

    store = _get_store()
    page = store.read_page(scope, kind, slug)
    if page is None:
        raise HTTPException(404, f"page not found: {scope}/{kind}/{slug}")

    # Field-by-field overlay — None means "don't touch".
    if "title" in body and body["title"] is not None:
        page.title = str(body["title"]).strip() or page.title
    if "body" in body and body["body"] is not None:
        page.body = str(body["body"])
    if "tags" in body and isinstance(body["tags"], list):
        page.tags = [str(t).strip() for t in body["tags"] if str(t).strip()]
    if "sources" in body and isinstance(body["sources"], list):
        page.sources = list(body["sources"])
    if "related" in body and isinstance(body["related"], list):
        page.related = list(body["related"])
    # Gene-like structured fields (admin can still author by hand)
    for fld in ("signals_match", "preconditions", "strategy",
                "validation"):
        if fld in body and isinstance(body[fld], list):
            setattr(page, fld, list(body[fld]))
    if "constraints" in body and isinstance(body["constraints"], dict):
        page.constraints = dict(body["constraints"])

    store.write_page(page, log_action="edit")
    logger.info("wiki page edited: %s/%s/%s by %s",
                scope, kind, slug, user.username)
    return {"ok": True, "page": _page_to_full(page)}


@router.post("/delete")
async def delete_wiki_page(
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """Delete a wiki page from disk. Not recoverable — caller is
    expected to confirm in the UI."""
    scope = (body.get("scope") or "").strip()
    kind = (body.get("kind") or "").strip().lower()
    slug = (body.get("slug") or "").strip()
    if not (scope and kind and slug):
        raise HTTPException(400, "scope, kind, slug all required")
    store = _get_store()
    path = store._page_path(scope, kind, slug)
    if not os.path.exists(path):
        raise HTTPException(404, f"page not found: {scope}/{kind}/{slug}")
    try:
        os.remove(path)
    except OSError as e:
        raise HTTPException(500, f"delete failed: {e}")
    # Refresh the scope index so the deleted entry vanishes from it.
    try:
        store.rebuild_index(scope)
    except Exception as e:
        logger.warning("rebuild_index after delete skipped: %s", e)
    logger.info("wiki page deleted: %s/%s/%s by %s",
                scope, kind, slug, user.username)
    return {"ok": True}


@router.post("/import")
async def import_wiki_page(
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """Admin entry-point for ingesting external content into the wiki.

    Mirrors ``wiki_ingest`` (the agent-facing tool) but is intended for
    admin-driven imports — PDF / Word / Markdown / HTML / TXT files
    that the admin uploads via the Portal. The frontend parses the file
    client-side via ``/api/portal/rag/parse-file`` and posts the
    extracted text here as ``body``.

    Differences from POST /create:
      - Tolerates very large bodies (no size cap; wiki pages can be
        full reference documents).
      - Stamps ``source=admin`` (or whatever caller passes; default
        admin) into the wiki page tags so search/filter can later
        distinguish admin-curated vs agent-authored entries.
      - Auto-generates slug from title if omitted.
      - 200 on success even when overwriting an existing slug (caller
        is admin and presumably knows; otherwise use POST /edit).

    Step B of the wiki / shared-knowledge merge plan. Step D will
    hook the RAG indexer so imported pages auto-index for vector
    retrieval — until then, imported pages are searchable via the
    wiki layer's built-in keyword scorer (good enough for ≤thousands
    of pages).
    """
    from ...knowledge.wiki_store import WikiPage, VALID_KINDS, slugify

    scope = (body.get("scope") or "global").strip()
    kind = (body.get("kind") or "reference").strip().lower()
    if kind not in VALID_KINDS:
        raise HTTPException(
            400,
            f"kind must be one of {sorted(VALID_KINDS)}, got {kind!r}",
        )
    title = (body.get("title") or "").strip()
    if not title:
        raise HTTPException(400, "title is required")
    body_text = (body.get("body") or body.get("content") or "").strip()
    if not body_text:
        raise HTTPException(400, "body is required (markdown content)")

    slug = (body.get("slug") or "").strip() or slugify(title)
    tags = list(body.get("tags") or [])
    # Stamp the source — used by future filters ("show me only
    # admin-curated reference pages, hide agent-authored experiences").
    source_tag = (body.get("source") or "admin").strip().lower()
    if source_tag and source_tag not in tags:
        tags.append(f"source:{source_tag}")

    store = _get_store()
    existing = store.read_page(scope, kind, slug)
    overwrote = existing is not None
    page = WikiPage(
        scope=scope, kind=kind, slug=slug,
        title=title, body=body_text,
        tags=[str(t).strip() for t in tags if str(t).strip()],
        sources=list(body.get("sources") or []),
        related=list(body.get("related") or []),
    )
    store.write_page(page, log_action="import")
    logger.info(
        "wiki page imported: %s/%s/%s (%d chars, overwrote=%s) by %s",
        scope, kind, slug, len(body_text), overwrote, user.username,
    )
    return {
        "ok": True,
        "page": _page_to_full(page),
        "overwrote": overwrote,
        "chars": len(body_text),
    }


@router.post("/toggle-valid")
async def toggle_wiki_valid(
    body: dict = Body(...),
    user: CurrentUser = Depends(get_current_user),
    hub=Depends(get_hub),
):
    """Flip a page's is_valid flag. Used to:
      - resurrect a page that auto-decayed after 3 consecutive fails
        (admin reviewed and decided the lessons are still relevant)
      - manually retire a noisy page without deleting its history
    """
    scope = (body.get("scope") or "").strip()
    kind = (body.get("kind") or "").strip().lower()
    slug = (body.get("slug") or "").strip()
    if not (scope and kind and slug):
        raise HTTPException(400, "scope, kind, slug all required")
    store = _get_store()
    page = store.read_page(scope, kind, slug)
    if page is None:
        raise HTTPException(404, f"page not found: {scope}/{kind}/{slug}")
    page.is_valid = not page.is_valid
    # Reset the failure streak when re-validating; otherwise the very
    # next failure would auto-decay it again.
    if page.is_valid:
        page.consecutive_fails = 0
    store.write_page(page, log_action="toggle_valid")
    logger.info("wiki page %s/%s/%s is_valid=%s (set by %s)",
                scope, kind, slug, page.is_valid, user.username)
    return {"ok": True, "is_valid": page.is_valid,
            "consecutive_fails": page.consecutive_fails}
