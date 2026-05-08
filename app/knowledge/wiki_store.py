"""Markdown-backed wiki store (Karpathy LLM-wiki pattern).

On-disk layout (under ``$DATA/wiki/``):

    wiki/
      global/                          # cross-role / shared pages
        index.md                       # auto-maintained title catalog
        log.md                         # append-only change record
        reference/<slug>.md            # raw refs / tech specs
        methodology/<slug>.md          # how-to / workflows
        template/<slug>.md             # writing / structure templates
        pattern/<slug>.md              # recurring-logic patterns
      role/<role_name>/                # role-scoped pages (experiences)
        index.md
        log.md
        experience/<slug>.md
        methodology/<slug>.md
        ...

Page format: YAML front-matter + markdown body.

    ---
    title: <human-readable title>
    kind: experience | methodology | template | pattern | reference
    scope: global | role:<role>
    tags: [list, of, tags]
    created_at: <unix>
    updated_at: <unix>
    success_count: 0
    fail_count: 0
    sources: [optional: links to raw documents that were ingested]
    related: [optional: other page slugs this links to]
    ---

    # <title>

    <body markdown>

This module owns:

  - ``read_page(scope, kind, slug)``       → ``WikiPage | None``
  - ``write_page(...)``                    → write file + update index + log
  - ``list_pages(scope, kind=None)``       → metadata index for prompt injection
  - ``search(query, scope=None, kind=None, limit=5)``  → top matches by simple
    substring + tag overlap. Replace with embedding search later if needed.
  - ``rebuild_index(scope)``               → regenerate ``index.md`` from disk

Search is intentionally **simple** (no embeddings yet) — operators can
upgrade later by plugging in an embedding store; the page format is
embedding-friendly (front-matter + body).
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("tudouclaw.v2.wiki")


VALID_KINDS: tuple[str, ...] = (
    "experience",
    "methodology",
    "template",
    "pattern",
    "reference",
)


# ─────────────────────────────────────────────────────────────────────
# Page model
# ─────────────────────────────────────────────────────────────────────


@dataclass
class WikiPage:
    scope: str               # "global" | "role:<role>"
    kind: str                # ∈ VALID_KINDS
    slug: str                # filename without .md
    title: str = ""
    body: str = ""
    tags: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    success_count: int = 0
    fail_count: int = 0
    sources: list[str] = field(default_factory=list)
    related: list[str] = field(default_factory=list)
    # ── Self-evolution Phase 1 (2026-05-08) ──
    # When ``knowledge_lookup`` returns this page and the calling
    # agent then closes a step via ``finalize_step`` (success) or marks
    # a step failed (fail), ``update_outcome()`` increments these
    # counters. Lets repeat-applied experiences accumulate authority
    # over time WITHOUT needing to fabricate new wiki entries (which
    # would be expensive in tokens and noise).
    #
    # applied_count    — total times the page was lookup-hit AND a
    #                    downstream step closed with this page in the
    #                    agent's ``lookup_trace``. == success + fail.
    # last_applied_at  — unix ts of the most recent application.
    # consecutive_fails — current streak of failures since last success.
    #                    Reset to 0 on any success. Used by the lazy
    #                    decay rule (≥3 → mark is_valid=False).
    # is_valid         — when False, ``search()`` skips this page so
    #                    repeatedly-failing lessons stop being injected.
    #                    Set automatically by update_outcome() OR by
    #                    admin via the wiki UI.
    applied_count: int = 0
    last_applied_at: float = 0.0
    consecutive_fails: int = 0
    is_valid: bool = True
    # ── Optional structured fields (Gene-like, borrowed from
    # @evomap/evolver's gene schema). Filled when ``kind`` is
    # "experience" / "methodology" / "pattern" — gives downstream
    # tools (selector / matcher / verifier) a normalised contract
    # vs. free-form prose. All fields default empty so legacy pages
    # unaffected.
    #
    # signals_match  — keywords that should trigger this page's recall
    #                  (used by future "auto-suggest experience for
    #                  current task" feature).
    # preconditions  — natural-language statements that must be true
    #                  before applying the page's strategy.
    # strategy       — ordered list of steps the page recommends.
    # constraints    — dict of bounds (max_files, forbidden_paths,
    #                  estimated_cost, ...). Free-shape JSON.
    # validation     — list of verifications that confirm the strategy
    #                  worked (commands / regex matches / acceptance).
    signals_match: list[str] = field(default_factory=list)
    preconditions: list[str] = field(default_factory=list)
    strategy: list[str] = field(default_factory=list)
    constraints: dict = field(default_factory=dict)
    validation: list[str] = field(default_factory=list)

    def to_metadata_line(self) -> str:
        """Single-line summary for index.md / prompt injection."""
        stats = f"✓{self.success_count}/✗{self.fail_count}"
        tags = (" #" + " #".join(self.tags)) if self.tags else ""
        return f"- [{self.kind}/{self.slug}] {self.title} ({stats}){tags}"

    def to_file_text(self) -> str:
        """Render full markdown file (front-matter + body)."""
        fm_lines = ["---"]
        fm_lines.append(f"title: {_yaml_str(self.title)}")
        fm_lines.append(f"kind: {self.kind}")
        fm_lines.append(f"scope: {self.scope}")
        if self.tags:
            fm_lines.append(f"tags: [{', '.join(_yaml_str(t) for t in self.tags)}]")
        fm_lines.append(f"created_at: {self.created_at:.0f}")
        fm_lines.append(f"updated_at: {self.updated_at:.0f}")
        fm_lines.append(f"success_count: {self.success_count}")
        fm_lines.append(f"fail_count: {self.fail_count}")
        # Phase-1 evolution counters — only written when non-default to
        # keep legacy wiki files unchanged on first read.
        if self.applied_count:
            fm_lines.append(f"applied_count: {self.applied_count}")
        if self.last_applied_at:
            fm_lines.append(f"last_applied_at: {self.last_applied_at:.0f}")
        if self.consecutive_fails:
            fm_lines.append(f"consecutive_fails: {self.consecutive_fails}")
        if not self.is_valid:
            fm_lines.append(f"is_valid: false")
        if self.sources:
            fm_lines.append(f"sources: [{', '.join(_yaml_str(s) for s in self.sources)}]")
        if self.related:
            fm_lines.append(f"related: [{', '.join(_yaml_str(r) for r in self.related)}]")
        # Gene-like structured fields (only emitted when non-empty)
        if self.signals_match:
            fm_lines.append(
                f"signals_match: [{', '.join(_yaml_str(s) for s in self.signals_match)}]"
            )
        if self.preconditions:
            fm_lines.append(
                f"preconditions: [{', '.join(_yaml_str(s) for s in self.preconditions)}]"
            )
        if self.strategy:
            fm_lines.append(
                f"strategy: [{', '.join(_yaml_str(s) for s in self.strategy)}]"
            )
        if self.constraints:
            import json as _json
            fm_lines.append(f"constraints: {_json.dumps(self.constraints, ensure_ascii=False)}")
        if self.validation:
            fm_lines.append(
                f"validation: [{', '.join(_yaml_str(s) for s in self.validation)}]"
            )
        fm_lines.append("---")
        body = self.body.rstrip() + "\n"
        return "\n".join(fm_lines) + "\n\n" + body


def _yaml_str(s: str) -> str:
    """Conservative YAML scalar — quote if it has any special char."""
    if not s:
        return '""'
    if re.search(r'[\n:#"\']|^\s|\s$', s):
        # double-escape internal double quotes
        return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return s


# ─────────────────────────────────────────────────────────────────────
# Front-matter parser (no PyYAML dep — these files are simple)
# ─────────────────────────────────────────────────────────────────────


_FM_DELIMITER = "---"
_LIST_RE = re.compile(r'^\s*\[(.*)\]\s*$')


def _parse_front_matter(text: str) -> tuple[dict, str]:
    """Split a markdown file into (front_matter_dict, body).

    Returns ({}, text) if no valid front-matter is found.
    """
    if not text.startswith(_FM_DELIMITER):
        return {}, text
    lines = text.splitlines()
    if len(lines) < 3 or lines[0] != _FM_DELIMITER:
        return {}, text
    end_idx = -1
    for i, line in enumerate(lines[1:], start=1):
        if line == _FM_DELIMITER:
            end_idx = i
            break
    if end_idx < 0:
        return {}, text
    fm_body = "\n".join(lines[1:end_idx])
    body = "\n".join(lines[end_idx + 1:]).lstrip("\n")
    fm: dict = {}
    for raw in fm_body.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if ":" not in raw:
            continue
        k, _, v = raw.partition(":")
        k = k.strip()
        v = v.strip()
        if not k:
            continue
        # list?
        m = _LIST_RE.match(v)
        if m:
            inner = m.group(1).strip()
            items: list[str] = []
            if inner:
                # split on commas at top level (no nesting expected)
                for piece in _split_yaml_list(inner):
                    items.append(_unquote(piece))
            fm[k] = items
            continue
        # JSON dict (used by Gene-like ``constraints`` field)?
        if v.startswith("{") and v.endswith("}"):
            try:
                import json as _json
                fm[k] = _json.loads(v)
                continue
            except (ValueError, TypeError):
                pass  # fall through to scalar
        # scalar
        if v.startswith('"') and v.endswith('"') and len(v) >= 2:
            fm[k] = _unquote(v)
        else:
            # numeric?
            if re.match(r'^-?\d+(\.\d+)?$', v):
                try:
                    fm[k] = float(v) if "." in v else int(v)
                except ValueError:
                    fm[k] = v
            else:
                fm[k] = v
    return fm, body


def _split_yaml_list(inner: str) -> list[str]:
    """Split a one-line YAML list body, respecting double-quoted commas."""
    out: list[str] = []
    cur = []
    in_q = False
    esc = False
    for ch in inner:
        if esc:
            cur.append(ch)
            esc = False
            continue
        if ch == "\\" and in_q:
            esc = True
            continue
        if ch == '"':
            in_q = not in_q
            cur.append(ch)
            continue
        if ch == "," and not in_q:
            out.append("".join(cur).strip())
            cur = []
            continue
        cur.append(ch)
    tail = "".join(cur).strip()
    if tail:
        out.append(tail)
    return out


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    return s


# ─────────────────────────────────────────────────────────────────────
# Slug helpers
# ─────────────────────────────────────────────────────────────────────


_SLUG_DROP_RE = re.compile(r'[^\w\u4e00-\u9fff-]+', re.UNICODE)


def slugify(text: str, max_len: int = 60) -> str:
    """Convert a title to a filesystem-safe slug. Keeps CJK as-is."""
    s = (text or "").strip().lower()
    s = re.sub(r'\s+', '-', s)
    s = _SLUG_DROP_RE.sub('', s)
    s = re.sub(r'-+', '-', s).strip('-')
    if not s:
        s = f"page-{int(time.time())}"
    return s[:max_len]


# ─────────────────────────────────────────────────────────────────────
# Store
# ─────────────────────────────────────────────────────────────────────


class WikiStore:
    """File-based wiki store with simple substring search.

    Thread-safe writes via a single mutex (low contention expected —
    writes only happen on agent ingest events).
    """

    def __init__(self, root_dir: str = "") -> None:
        if not root_dir:
            from app import DEFAULT_DATA_DIR
            root_dir = os.path.join(DEFAULT_DATA_DIR, "wiki")
        self._root = root_dir
        self._lock = threading.Lock()
        os.makedirs(self._root, exist_ok=True)

    # ── path helpers ──────────────────────────────────────────────
    def _scope_dir(self, scope: str) -> str:
        if scope == "global":
            return os.path.join(self._root, "global")
        if scope.startswith("role:"):
            role = scope.split(":", 1)[1].strip() or "default"
            # filesystem-safe role name
            role = re.sub(r'[^\w-]+', '_', role)
            return os.path.join(self._root, "role", role)
        # Unknown scope → treat as "global" with logged warning
        logger.warning("Unknown scope %r — falling back to 'global'", scope)
        return os.path.join(self._root, "global")

    def _page_path(self, scope: str, kind: str, slug: str) -> str:
        if kind not in VALID_KINDS:
            raise ValueError(f"Unknown wiki kind: {kind}")
        return os.path.join(self._scope_dir(scope), kind, f"{slug}.md")

    # ── read ──────────────────────────────────────────────────────
    def read_page(self, scope: str, kind: str, slug: str) -> Optional[WikiPage]:
        path = self._page_path(scope, kind, slug)
        if not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            return None
        fm, body = _parse_front_matter(text)
        _constraints = fm.get("constraints") or {}
        if not isinstance(_constraints, dict):
            _constraints = {}
        # is_valid: legacy pages have no field → True; explicit "false"
        # / 0 / "no" / "False" all coerce to False.
        _iv_raw = fm.get("is_valid", True)
        if isinstance(_iv_raw, bool):
            _is_valid = _iv_raw
        else:
            _is_valid = str(_iv_raw).strip().lower() not in ("false", "0", "no")
        return WikiPage(
            scope=str(fm.get("scope", scope)),
            kind=str(fm.get("kind", kind)),
            slug=slug,
            title=str(fm.get("title", "")),
            body=body,
            tags=list(fm.get("tags") or []),
            created_at=float(fm.get("created_at") or 0.0),
            updated_at=float(fm.get("updated_at") or 0.0),
            success_count=int(fm.get("success_count") or 0),
            fail_count=int(fm.get("fail_count") or 0),
            sources=list(fm.get("sources") or []),
            related=list(fm.get("related") or []),
            signals_match=list(fm.get("signals_match") or []),
            preconditions=list(fm.get("preconditions") or []),
            strategy=list(fm.get("strategy") or []),
            constraints=_constraints,
            validation=list(fm.get("validation") or []),
            applied_count=int(fm.get("applied_count") or 0),
            last_applied_at=float(fm.get("last_applied_at") or 0.0),
            consecutive_fails=int(fm.get("consecutive_fails") or 0),
            is_valid=_is_valid,
        )

    # ── write ─────────────────────────────────────────────────────
    def write_page(self, page: WikiPage, *, log_action: str = "ingest") -> WikiPage:
        """Persist a page; update index.md and log.md for the scope.

        ``log_action`` is appended to log.md as the verb (ingest/update/lint/...).
        Sets ``created_at`` / ``updated_at`` timestamps.
        """
        if page.kind not in VALID_KINDS:
            raise ValueError(f"Unknown wiki kind: {page.kind}")
        if not page.slug:
            page.slug = slugify(page.title)
        now = time.time()
        with self._lock:
            path = self._page_path(page.scope, page.kind, page.slug)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            existed = os.path.exists(path)
            if not existed and page.created_at <= 0:
                page.created_at = now
            page.updated_at = now
            with open(path, "w", encoding="utf-8") as f:
                f.write(page.to_file_text())
            self._append_log(page.scope, log_action, f"{page.kind}/{page.slug}",
                             page.title)
            self._rebuild_index_locked(page.scope)
        return page

    # ── list / search ─────────────────────────────────────────────
    def list_pages(self, scope: str, kind: str = "") -> list[WikiPage]:
        """Return all pages for a scope (optionally filtered by kind)."""
        out: list[WikiPage] = []
        scope_dir = self._scope_dir(scope)
        if not os.path.isdir(scope_dir):
            return out
        kinds = (kind,) if kind else VALID_KINDS
        for k in kinds:
            kdir = os.path.join(scope_dir, k)
            if not os.path.isdir(kdir):
                continue
            for fn in sorted(os.listdir(kdir)):
                if not fn.endswith(".md"):
                    continue
                slug = fn[:-3]
                page = self.read_page(scope, k, slug)
                if page is not None:
                    out.append(page)
        return out

    def search(self, query: str, *, scope: str = "",
               kind: str = "", limit: int = 5) -> list[WikiPage]:
        """Simple substring + tag search.

        Matches in title get a 3x weight, tag exact-match 2x, body 1x.
        Replace with embedding-based search if/when the wiki grows large.
        """
        q = (query or "").strip().lower()
        if not q:
            return []
        scopes_to_search: list[str] = []
        if scope:
            scopes_to_search.append(scope)
        else:
            # Walk all scope dirs we know of
            scopes_to_search.append("global")
            role_root = os.path.join(self._root, "role")
            if os.path.isdir(role_root):
                for r in os.listdir(role_root):
                    if os.path.isdir(os.path.join(role_root, r)):
                        scopes_to_search.append(f"role:{r}")
        scored: list[tuple[float, WikiPage]] = []
        terms = [t for t in q.split() if t]
        for sc in scopes_to_search:
            for p in self.list_pages(sc, kind=kind):
                # Phase-1 evolution: lazy-skip pages that the outcome
                # tracker has marked invalid (≥3 consecutive fails or
                # admin-disabled). Keeps repeatedly-bad lessons from
                # leaking back into agent prompts.
                if not p.is_valid:
                    continue
                score = 0.0
                title_l = (p.title or "").lower()
                body_l = (p.body or "").lower()
                tags_l = [t.lower() for t in p.tags]
                for t in terms:
                    if t in title_l:
                        score += 3.0
                    if t in tags_l:
                        score += 2.0
                    if t in body_l:
                        score += 1.0
                if score > 0:
                    # Tie-break by success_rate so pages that have
                    # repeatedly worked appear higher than equally-
                    # matching pages with no track record.
                    total_uses = p.success_count + p.fail_count
                    success_rate = (p.success_count / total_uses
                                    if total_uses > 0 else 0.5)
                    scored.append((score + success_rate * 0.5, p))
        scored.sort(key=lambda x: (-x[0], x[1].title))
        return [p for _, p in scored[:limit]]

    # ── Phase-1 evolution: outcome write-back ─────────────────────
    def update_outcome(self, scope: str, kind: str, slug: str,
                       *, success: bool) -> Optional[WikiPage]:
        """Record that this page was applied and the downstream task
        succeeded (or failed). Called from finalize_step / fail_step.

        Cheap and idempotent within a single application: the caller
        is expected to dedupe by (entry_id, turn_id) before invoking
        this. Zero LLM cost — pure file write.

        Returns the updated WikiPage on success, None if the page
        doesn't exist or write failed.
        """
        with self._lock:
            page = self.read_page(scope, kind, slug)
            if page is None:
                logger.debug("update_outcome: %s/%s/%s not found",
                             scope, kind, slug)
                return None
            if success:
                page.success_count += 1
                page.consecutive_fails = 0
            else:
                page.fail_count += 1
                page.consecutive_fails += 1
                # Lazy decay — 3 consecutive fails marks the page so
                # it stops being returned by ``search()``. Admin can
                # flip is_valid back to True via the wiki UI after
                # editing the page content.
                if page.consecutive_fails >= 3 and page.is_valid:
                    page.is_valid = False
                    logger.info(
                        "wiki page %s/%s/%s marked invalid "
                        "(3 consecutive fails)",
                        scope, kind, slug,
                    )
            page.applied_count += 1
            page.last_applied_at = time.time()
            # Write-through; reuse the regular write path so log.md /
            # index.md stay coherent. ``log_action="outcome"`` keeps
            # these writes distinguishable from genuine ingest events.
            return self.write_page(page, log_action="outcome")

    # ── index / log ───────────────────────────────────────────────
    def rebuild_index(self, scope: str) -> None:
        with self._lock:
            self._rebuild_index_locked(scope)

    def _rebuild_index_locked(self, scope: str) -> None:
        """Regenerate index.md by scanning the scope dir."""
        scope_dir = self._scope_dir(scope)
        os.makedirs(scope_dir, exist_ok=True)
        lines = [f"# {scope} wiki index", ""]
        total = 0
        for kind in VALID_KINDS:
            pages = self.list_pages(scope, kind=kind)
            if not pages:
                continue
            lines.append(f"## {kind} ({len(pages)})")
            for p in pages:
                lines.append(p.to_metadata_line())
            lines.append("")
            total += len(pages)
        lines.insert(2, f"_{total} pages • updated {time.strftime('%Y-%m-%d %H:%M:%S')}_")
        lines.insert(3, "")
        path = os.path.join(scope_dir, "index.md")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except OSError as e:
            logger.warning("write index.md failed (%s): %s", path, e)

    def _append_log(self, scope: str, action: str, ref: str, title: str) -> None:
        scope_dir = self._scope_dir(scope)
        os.makedirs(scope_dir, exist_ok=True)
        path = os.path.join(scope_dir, "log.md")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {action} {ref} — {title}\n"
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            logger.warning("append log.md failed (%s): %s", path, e)

    # ── index for prompt injection ─────────────────────────────────
    def render_index_for_prompt(self, scope: str, *, max_pages: int = 50) -> str:
        """Compact index suitable for system prompt injection.

        Returns "" if the scope has no pages — saves the wasted bytes.
        """
        pages: list[WikiPage] = []
        for kind in VALID_KINDS:
            pages.extend(self.list_pages(scope, kind=kind))
        if not pages:
            return ""
        # Sort: success_count desc, then most-recently-updated, capped
        pages.sort(key=lambda p: (-p.success_count, -p.updated_at))
        pages = pages[:max_pages]
        lines = [
            f"# Wiki 索引 (scope={scope}; "
            f"{len(pages)} 条; 用 knowledge_lookup 拉详情)"
        ]
        for p in pages:
            lines.append(p.to_metadata_line())
        return "\n".join(lines)


# Process-wide singleton
_STORE: Optional[WikiStore] = None
_STORE_LOCK = threading.Lock()


def get_wiki_store() -> WikiStore:
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                _STORE = WikiStore()
    return _STORE
