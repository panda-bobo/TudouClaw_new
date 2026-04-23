"""
Artifact extractors — turn raw tool-result strings into Artifact rows.

Universal file contract (phase-1 generalization):

  * Any URL or absolute/relative path whose extension is in the
    central mime_registry becomes an Artifact of the matching kind.
  * No tool-name-specific logic. No hardcoded field names like
    `video_url` / `image_path` — those are gone. The walker just
    inspects every string in the JSON tree (and every URL in plain
    text) and asks the registry "what kind is this?".
  * The registry decides kind, mime, render_hint, category. Adding
    a new file type means editing mime_registry.py only — extractor,
    store, router, frontend pick it up automatically.

Higher-quality extraction (magic-byte sniffing, image dimensions,
duration probing) is phase-2 work and should live in dedicated
post-extract enrichers, not here.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Dict, Iterable, List, Optional

from .artifact import Artifact, ArtifactKind, ArtifactStore, ProducedBy
from .mime_registry import (
    DEFAULT_INFO,
    FileKindInfo,
    info_for_value,
)


# How deep we'll walk into deliverable_dir during a scan, and how many
# files we'll ingest in one pass. Both are safety rails — a runaway
# tool that drops 100k files into the workspace shouldn't OOM the
# portal on next chat-open.
_SCAN_MAX_DEPTH = 6
_SCAN_MAX_FILES = 2000

# Files larger than this don't get a content hash during scan — the
# IO cost would dominate scan time on workspaces with big binaries.
# Identity for these still works via path-based stable id (scheme A).
_HASH_MAX_BYTES = 100 * 1024 * 1024  # 100 MB
_HASH_CHUNK = 1024 * 1024            # 1 MB read chunks


# ----------------------------------------------------------------------
# Stable identity helpers (scheme A + B)
# ----------------------------------------------------------------------
def stable_artifact_id(abs_path: str) -> str:
    """Deterministic artifact id derived from absolute path.

    Same file path -> same id, across portal restarts. The 12-hex-char
    truncation matches the existing `_new_id()` shape so url routing
    and frontend logic don't need to change. Collision space (16^12 =
    ~2.8e14) is more than enough for a single workspace.
    """
    h = hashlib.sha1(abs_path.encode("utf-8", errors="replace")).hexdigest()
    return f"art_{h[:12]}"


def content_hash_blake2b(abs_path: str, *, max_bytes: int = _HASH_MAX_BYTES) -> Optional[str]:
    """Streaming BLAKE2b-128 of file contents. Returns hex string,
    or None if the file is too large / unreadable. Two paths whose
    bytes match get the same hash; useful for "same file generated
    twice under different names" detection.
    """
    try:
        st = os.stat(abs_path)
    except OSError:
        return None
    if st.st_size > max_bytes:
        return None
    h = hashlib.blake2b(digest_size=16)  # 32-hex-char digest
    try:
        with open(abs_path, "rb") as f:
            while True:
                chunk = f.read(_HASH_CHUNK)
                if not chunk:
                    break
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


# very tolerant URL regex — recall over precision
_URL_RE = re.compile(
    r"https?://[^\s\"'<>)\]\}]+",
    re.IGNORECASE,
)

# Absolute filesystem paths in free text. Anchored to a leading "/" or
# "~/" (we don't want to swallow random alphanumerics) and required to
# end in a recognisable extension token. Stops at whitespace, quotes,
# backticks, brackets, and common trailing punctuation.
#
# Examples that match:
#   /Users/foo/bar.mp4
#   `/tmp/output.png`
#   ~/Downloads/report.pdf
#   "/var/data/v 2.mp4"   (matched up to the space before "2")
#
# Examples that don't:
#   just/a/relative/thing.txt   (no leading /, no leading ~/)
#   plain word                  (no extension)
_PATH_RE = re.compile(
    # Left boundary: not in the middle of a word, and not after `:`,
    # `/`, or `.` — these prevent us from pulling
    #   - "/cdn.example.com/x.mp4" out of "https://cdn..." (the `:` and
    #     the second `/` of `://` both get blocked)
    #   - capturing "/foo.txt" out of "../foo.txt" twice
    r"(?<![A-Za-z0-9_/:.])"
    # Leading prefix: `./`, `../`, `~/`, or `/`. The relative forms
    # matter for assistant prose like "saved to ./output.mp4" — without
    # capturing the leading `.`, the value would normalize to a bogus
    # rooted path that fails the deliverable_dir whitelist on click.
    r"(?:\.{1,2}/|~/|/)"
    r"[^\s`'\"<>()\[\]{},;]*"       # body — no whitespace or common delimiters
    r"\.[A-Za-z0-9]{1,8}"           # required extension
)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def _looks_like_path(s: str) -> bool:
    """Heuristic: does this string look like a filesystem path?"""
    if not s or len(s) > 4096 or "\n" in s:
        return False
    if s.startswith(("http://", "https://", "data:", "ftp://")):
        return False
    if s.startswith(("/", "~", "./", "../")):
        return True
    if len(s) > 2 and s[1] == ":" and s[2] in ("\\", "/"):  # windows C:\ or C:/
        return True
    return False


def _basename(value: str) -> str:
    base = value.split("?", 1)[0].split("#", 1)[0]
    last = max(base.rfind("/"), base.rfind("\\"))
    name = base[last + 1:] if last >= 0 else base
    return name or value[:48]


def _label_for(value: str, info: FileKindInfo) -> str:
    name = _basename(value)
    if len(name) > 80:
        name = name[:77] + "..."
    return name


def _candidate(value: str, *, source: str = "") -> Optional[Dict[str, Any]]:
    """Build a candidate dict for `value`, classified by mime_registry.

    Returns None if `value` is not a recognised file (no extension)
    AND not a URL — i.e., we have nothing useful to record.

    `source` is "url" or "path" for provenance, optional.
    """
    info = info_for_value(value)
    is_url = value.startswith(("http://", "https://"))
    if info is DEFAULT_INFO and not is_url:
        # extensionless local string — probably not a file
        return None
    # extensionless URL falls back to ArtifactKind.URL
    if info is DEFAULT_INFO and is_url:
        kind = ArtifactKind.URL
        mime = None
        render_hint = "card"
        category = "other"
    else:
        kind = info.kind
        mime = info.mime
        render_hint = info.render_hint
        category = info.category
    return {
        "kind": kind,
        "value": value,
        "label": _label_for(value, info),
        "mime": mime,
        "metadata": {
            "filename": _basename(value),
            "render_hint": render_hint,
            "category": category,
            "extracted_from": source,
        },
    }


# ----------------------------------------------------------------------
# extraction passes
# ----------------------------------------------------------------------
def extract_from_text(text: str) -> List[Dict[str, Any]]:
    """Find every URL AND every absolute filesystem path in `text` and
    emit one candidate per unique match.

    Path extraction matters for assistant prose like::

        文件的本地存储路径是: /Users/pang/.tudou_claw/.../jiangbanya.mp4

    Without it, the path-in-prose case never deduplicates against the
    scan-ingested artifact and the FileCard fails to attach to the
    bubble that announced the file.
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()
    if not text:
        return out
    # 1) URLs
    for m in _URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:!?")
        if url in seen:
            continue
        seen.add(url)
        c = _candidate(url, source="url")
        if c is not None:
            out.append(c)
    # 2) Absolute filesystem paths (must end in a recognised extension)
    for m in _PATH_RE.finditer(text):
        p = m.group(0).rstrip(".,;:!?")
        if p in seen:
            continue
        seen.add(p)
        # Reuse mime_registry classification — _candidate skips strings
        # whose extension isn't recognised, so noisy junk like /tmp/x.lock
        # gets dropped automatically.
        info = info_for_value(p)
        if info is DEFAULT_INFO:
            continue
        c = _candidate(p, source="path")
        if c is not None:
            out.append(c)
    return out


def extract_from_json(obj: Any) -> List[Dict[str, Any]]:
    """Walk a JSON-decoded object and pull out every string that looks
    like a file URL or path. No field-name whitelist — we trust the
    registry to filter by extension.
    """
    out: List[Dict[str, Any]] = []
    seen: set = set()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, str):
            s = node.strip()
            if not s or s in seen:
                return
            if s.startswith(("http://", "https://")):
                seen.add(s)
                c = _candidate(s, source="url")
                if c is not None:
                    out.append(c)
            elif _looks_like_path(s):
                # require an extension that's in the registry, OR
                # the string must end in a known suffix — otherwise
                # we'd hoover up every random absolute path
                info = info_for_value(s)
                if info is not DEFAULT_INFO:
                    seen.add(s)
                    c = _candidate(s, source="path")
                    if c is not None:
                        out.append(c)

    visit(obj)
    return out


def extract_from_tool_result(result_str: str) -> List[Dict[str, Any]]:
    """Best-effort universal extractor.

    Tries JSON first (catches structured tool outputs from MCPs and
    builtin tools), falls back to plain-text URL scanning (catches
    URLs in prose, log lines, mixed output).

    Always returns a (possibly empty) list. Never raises.
    """
    if not result_str:
        return []
    candidates: List[Dict[str, Any]] = []
    try:
        parsed = json.loads(result_str)
        candidates.extend(extract_from_json(parsed))
    except Exception:
        pass
    text_candidates = extract_from_text(result_str)
    seen_values = {c["value"] for c in candidates}
    for c in text_candidates:
        if c["value"] not in seen_values:
            candidates.append(c)
            seen_values.add(c["value"])
    return candidates


# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# deliverable_dir scanner — ingests files that already exist on disk
# but never went through a tool_result extraction pass (e.g. files
# generated before ShadowRecorder was wired up, files dropped into
# the workspace by a side-channel, files from a previous portal run).
#
# Idempotent: skips anything whose absolute path is already in the
# store. Cheap to call repeatedly.
# ----------------------------------------------------------------------
def scan_deliverable_dir(
    store: ArtifactStore,
    deliverable_dir: str,
    *,
    produced_by: Optional[ProducedBy] = None,
    max_depth: int = _SCAN_MAX_DEPTH,
    max_files: int = _SCAN_MAX_FILES,
) -> List[Artifact]:
    """Walk `deliverable_dir`, ingest every file the registry recognises
    that isn't already in `store`. Returns the list of newly-added
    Artifacts (possibly empty). Never raises.
    """
    if not deliverable_dir or not os.path.isdir(deliverable_dir):
        return []
    try:
        root_abs = os.path.abspath(deliverable_dir)
    except Exception:
        return []

    existing_values = {a.value for a in store.all()}
    existing_ids = set(store._items.keys())  # noqa: SLF001
    added: List[Artifact] = []
    n_seen = 0

    import time as _time

    # Directories the agent writes to but whose contents are NOT deliverables:
    #   - tool_outputs/      : auto-spilled large tool_result bodies
    #   - skills/            : agent-local SKILL.md bundles
    #   - .shadow / cache    : agent-state metadata
    # Without this filter, every bash/read_file spill shows up as a
    # download card on the chat bubble after the next /files call.
    _SCAN_EXCLUDE_DIRS = {"tool_outputs", "skills", "cache", "__pycache__",
                          "attachments"}
    # Root-level workspace meta files that are context docs, not deliverables.
    _SCAN_EXCLUDE_ROOT_FILES = {
        "Project.md", "Skills.md", "MCP.md", "Tasks.md",
        "Scheduled.md", "ActiveThinking.md",
    }
    # Suffix-based exclusions: skill preview / draft markdowns.
    _SCAN_EXCLUDE_SUFFIXES = ("-skill.md",)
    _SCAN_EXCLUDE_EXACT = {"skill-full.md"}

    for dirpath, dirnames, filenames in os.walk(root_abs, followlinks=False):
        # depth guard
        try:
            rel = os.path.relpath(dirpath, root_abs)
            depth = 0 if rel == "." else rel.count(os.sep) + 1
        except Exception:
            depth = 0
        if depth >= max_depth:
            dirnames[:] = []  # don't recurse deeper
        # skip hidden + cache + agent-meta dirs to keep scan cheap and tidy
        dirnames[:] = [
            d for d in dirnames
            if not d.startswith(".") and d not in _SCAN_EXCLUDE_DIRS
        ]
        is_root = (dirpath == root_abs)

        for fn in filenames:
            if fn.startswith("."):
                continue
            # At workspace root, skip meta files (Project.md, etc.)
            if is_root and fn in _SCAN_EXCLUDE_ROOT_FILES:
                continue
            # Skip skill preview / draft markdowns anywhere in tree
            if fn in _SCAN_EXCLUDE_EXACT:
                continue
            if any(fn.endswith(sfx) for sfx in _SCAN_EXCLUDE_SUFFIXES):
                continue
            n_seen += 1
            if n_seen > max_files:
                return added  # safety stop

            try:
                full = os.path.abspath(os.path.join(dirpath, fn))
            except Exception:
                continue
            if full in existing_values:
                continue

            info = info_for_value(full)
            if info is DEFAULT_INFO:
                # unknown extension — skip rather than pollute the store
                continue

            # stable id by path; if two scans hit the same file the
            # second one short-circuits at the existing_values check
            # above. The id-collision branch below is just defence in
            # depth (e.g. an artifact with the same id was injected
            # via tool_result before the scan ran).
            stable_id = stable_artifact_id(full)
            if stable_id in existing_ids:
                continue

            try:
                st = os.stat(full)
                size = st.st_size
                mtime = st.st_mtime
            except Exception:
                size = None
                mtime = _time.time()

            chash = content_hash_blake2b(full)

            try:
                art = Artifact(
                    id=stable_id,
                    kind=info.kind,
                    value=full,
                    label=fn[:80],
                    # produced_at = file mtime, not scan time —
                    # critical for timestamp-adjacency bubble matching
                    produced_at=mtime,
                    produced_by=produced_by or ProducedBy(),
                    mime=info.mime,
                    size=size,
                    metadata={
                        "filename": fn,
                        "render_hint": info.render_hint,
                        "category": info.category,
                        "extracted_from": "deliverable_scan",
                        "content_hash": chash,    # may be None for huge files
                        "mtime": mtime,
                    },
                )
                store.put(art)
                added.append(art)
                existing_values.add(full)
                existing_ids.add(stable_id)
            except Exception:
                # store.put() can raise on duplicate id; treat as soft-skip
                continue

    return added


def normalize_path_candidates(
    candidates: List[Dict[str, Any]],
    base_dir: str,
) -> List[Dict[str, Any]]:
    """Resolve every relative-path candidate against ``base_dir`` so
    that ``./foo.mp4`` and ``/abs/dir/foo.mp4`` end up with the SAME
    ``value`` field — which is the dedup key inside the artifact store.

    URL candidates are untouched. Already-absolute paths are run through
    ``os.path.abspath`` so ``//`` and trailing-slash variants collapse.
    User-tilde paths are expanded via ``os.path.expanduser``.

    Mutates the candidates in-place AND returns the same list (for
    chaining). Never raises.
    """
    if not candidates or not base_dir:
        return candidates
    try:
        base_abs = os.path.abspath(base_dir)
    except Exception:
        return candidates
    for c in candidates:
        try:
            v = c.get("value", "")
            if not isinstance(v, str) or not v:
                continue
            if v.startswith(("http://", "https://", "data:", "ftp://")):
                continue
            if v.startswith("~"):
                v_norm = os.path.abspath(os.path.expanduser(v))
            elif os.path.isabs(v):
                v_norm = os.path.abspath(v)
            else:
                # Relative path: ./foo, ../foo, foo/bar, bare basename
                v_norm = os.path.abspath(os.path.join(base_abs, v))
            if v_norm != v:
                c["value"] = v_norm
                # also refresh the human-readable bits so the FileCard
                # shows the proper basename rather than e.g. "./foo.mp4"
                md = c.get("metadata") or {}
                md["filename"] = _basename(v_norm)
                c["metadata"] = md
                c["label"] = _label_for(v_norm, info_for_value(v_norm))
        except Exception:
            continue
    return candidates


def ingest_into_store(
    store: ArtifactStore,
    candidates: Iterable[Dict[str, Any]],
    *,
    produced_by: Optional[ProducedBy] = None,
    return_existing: bool = False,
) -> List[Artifact]:
    """Insert each candidate into `store`, deduping by value.

    By default returns ONLY the freshly-created artifacts (preserves the
    historical "new artifacts this turn" semantics used by live recording).

    With ``return_existing=True``, also returns the matching pre-existing
    artifact for any candidate whose value is already in the store. This
    is what callers need when they want a complete "every artifact this
    candidate set refers to" mapping (e.g. ``compute_file_index_from_events``
    has to bucket scan-ingested artifacts that get re-mentioned by a
    later tool_result event).
    """
    out: List[Artifact] = []
    # value -> existing Artifact (so we can return it without a second scan)
    existing_by_value: Dict[str, Artifact] = {a.value: a for a in store.all()}
    for c in candidates:
        v = c.get("value")
        if not v:
            continue
        if v in existing_by_value:
            if return_existing:
                out.append(existing_by_value[v])
            continue
        art = store.create(
            kind=c["kind"],
            value=v,
            label=c.get("label", v[:48]),
            produced_by=produced_by,
            mime=c.get("mime"),
            metadata=c.get("metadata"),
        )
        out.append(art)
        existing_by_value[v] = art
    return out
