"""Specialty template loader — read YAML, validate, cache, return dataclass.

Templates live under ``app/data/specialty_templates/`` (resolved via
:func:`app.domain_expert._config.template_dir`). One YAML per specialty,
filename = ``<specialty>.yaml`` (e.g. ``legal.yaml``).

Public API:
    load(specialty)           → SpecialtyTemplate
    load_all()                → list[SpecialtyTemplate]
    list_available()          → list[str]   (specialty ids on disk)
    invalidate_cache()        → clear in-process cache (test helper)

Caching is keyed by (path, mtime). Editing a template at runtime causes
the next ``load()`` to re-parse automatically. Templates are immutable
once loaded — callers are expected not to mutate the returned dataclass.
Mock-friendly: the underlying file IO can be redirected by patching
``_config.template_dir``.

Errors raised:
    TemplateNotFoundError    — file missing
    TemplateInvalidError     — YAML parse error or JSONSchema validation
                               failure (with original exception attached)
"""
from __future__ import annotations

import os
from typing import Any

import jsonschema
import yaml

from . import _config
from .template import SpecialtyTemplate, schema


# ── Errors ──

class TemplateError(Exception):
    """Base class for template loader errors."""


class TemplateNotFoundError(TemplateError):
    """Raised when the YAML file for a specialty does not exist."""


class TemplateInvalidError(TemplateError):
    """Raised when a YAML file fails parsing or schema validation."""


# ── Cache ──
# key: absolute_path → (mtime, SpecialtyTemplate)
_cache: dict[str, tuple[float, SpecialtyTemplate]] = {}


def invalidate_cache() -> None:
    """Drop the in-process cache. Test-helper / hot-reload trigger."""
    _cache.clear()


# ── Public API ──

def _yaml_path_for(specialty: str) -> str:
    """Resolve a specialty key to its YAML path.

    Supports nested paths like ``legal/civil_law`` → ``<dir>/legal/civil_law.yaml``
    so a domain (e.g. ``legal``) can have multiple sub-specialties
    (民法 / 刑法 / 中东法) sharing a common base via ``extends``.
    """
    return os.path.join(_config.template_dir(), f"{specialty}.yaml")


def list_available() -> list[str]:
    """Return sorted list of specialty ids on disk (excluding abstract bases).

    Walks the template directory tree so nested sub-specialties
    (``legal/civil_law``, ``medical/cardiology``) are discovered.
    Files starting with ``_`` (e.g. ``_base.yaml``) are treated as
    inheritance helpers and excluded; same for any YAML whose top-level
    has ``abstract: true``.
    """
    base = _config.template_dir()
    if not os.path.isdir(base):
        return []
    out: list[str] = []
    for root, _dirs, files in os.walk(base):
        for fn in files:
            if not fn.endswith(".yaml") or fn.startswith("_"):
                continue
            full = os.path.join(root, fn)
            rel = os.path.relpath(full, base)
            key = rel[: -len(".yaml")].replace(os.sep, "/")
            # Cheap abstract check — just peek the top-level "abstract" key
            try:
                with open(full, "r", encoding="utf-8") as f:
                    raw = yaml.safe_load(f) or {}
                if isinstance(raw, dict) and raw.get("abstract"):
                    continue
            except Exception:
                # Don't let one bad file hide all others
                pass
            out.append(key)
    out.sort()
    return out


def load(specialty: str) -> SpecialtyTemplate:
    """Load a single specialty template by name.

    Examples:
        load("legal")            → top-level legal.yaml
        load("legal/civil_law")  → legal/civil_law.yaml (with inheritance resolved)

    Resolves any ``extends:`` chain and deep-merges into a single dict
    BEFORE schema validation, so child YAMLs only need to specify what
    they add/override. Raises :class:`TemplateNotFoundError` if file
    missing or :class:`TemplateInvalidError` on parse/validation.
    """
    if not specialty:
        raise ValueError("specialty name required")

    path = _yaml_path_for(specialty)
    if not os.path.isfile(path):
        raise TemplateNotFoundError(
            f"No template YAML for specialty {specialty!r} at {path}"
        )

    mtime = os.path.getmtime(path)
    cached = _cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]

    # Read raw, resolve any extends: chain, then validate the merged dict
    raw_self = _read_raw_yaml(path)
    if raw_self.get("abstract"):
        raise TemplateInvalidError(
            f"Template {specialty!r} is marked abstract: true and cannot be "
            "loaded as a concrete specialty (it's only meant for `extends:`)."
        )
    merged = _resolve_inheritance(raw_self, path)
    # Strip control-only keys so schema doesn't complain
    merged.pop("extends", None)
    merged.pop("abstract", None)
    _validate_against_schema(merged, path)
    try:
        tpl = SpecialtyTemplate.from_dict(merged)
    except (TypeError, ValueError) as e:
        raise TemplateInvalidError(
            f"Template at {path} failed dataclass build: {e}"
        ) from e

    _cache[path] = (mtime, tpl)
    return tpl


def load_all() -> list[SpecialtyTemplate]:
    """Load every available template. Skips invalid files but logs them
    via raised TemplateInvalidError for the offending one — this is the
    strict-by-default version. If you need lenient behavior, iterate
    :func:`list_available` and call :func:`load` per id with try/except."""
    out: list[SpecialtyTemplate] = []
    for sid in list_available():
        out.append(load(sid))
    return out


# ── Helpers ──


def _read_raw_yaml(path: str) -> dict[str, Any]:
    """Open + parse YAML. No schema validation, no inheritance resolution.

    Used both as a final reader (when there's no ``extends:``) and as the
    intermediate step inside :func:`_resolve_inheritance`.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise TemplateInvalidError(
            f"Template at {path} is not valid YAML: {e}"
        ) from e
    except OSError as e:
        raise TemplateInvalidError(
            f"Template at {path} could not be read: {e}"
        ) from e

    if raw is None:
        raise TemplateInvalidError(f"Template at {path} is empty")
    if not isinstance(raw, dict):
        raise TemplateInvalidError(
            f"Template at {path} must be a mapping, got {type(raw).__name__}"
        )
    return raw


def _resolve_inheritance(raw: dict[str, Any], current_path: str,
                         _seen: set[str] | None = None) -> dict[str, Any]:
    """If ``raw['extends']`` is set, recursively load+merge the parent.

    Parents may themselves have ``extends:``; the chain is resolved
    bottom-up (root parent first, then progressively more specific
    overrides on top). Cycles raise TemplateInvalidError.

    Merge semantics (see :func:`_deep_merge`):
      - Scalar / dict at same key: child wins (recursive for dicts).
      - List at same key: child APPENDED to parent's. Use a magic
        ``__replace__: true`` marker to override fully (rare, escape
        hatch for sub-specialty that explicitly disables a parent
        red-line).
    """
    parent_key = raw.get("extends")
    if not parent_key:
        return raw

    if _seen is None:
        _seen = set()
    if current_path in _seen:
        raise TemplateInvalidError(
            f"Cycle detected in extends chain at {current_path}; "
            f"chain: {' → '.join(_seen)} → {current_path}"
        )
    _seen.add(current_path)

    parent_path = os.path.join(_config.template_dir(), f"{parent_key}.yaml")
    if not os.path.isfile(parent_path):
        raise TemplateInvalidError(
            f"Template at {current_path} extends {parent_key!r} but "
            f"parent file not found at {parent_path}"
        )
    parent_raw = _read_raw_yaml(parent_path)
    parent_resolved = _resolve_inheritance(parent_raw, parent_path, _seen)
    # Strip parent's `abstract` so the merged result isn't mistakenly
    # considered abstract just because a base was.
    parent_resolved = {k: v for k, v in parent_resolved.items() if k != "abstract"}
    return _deep_merge(parent_resolved, raw)


def _deep_merge(parent: dict, child: dict) -> dict:
    """Deep-merge child into parent. Child wins for scalars; lists APPEND.

    Special markers in child:
      - ``__replace__: true`` (inside a dict) means: don't merge with parent
        at this key, fully replace.

    Always returns a new dict; doesn't mutate either input.
    """
    out = dict(parent)
    for k, v in child.items():
        # Skip control fields — handled at outer level
        if k in ("extends",):
            continue
        if k == "abstract":
            # Children may set abstract: false to override parent's true,
            # but this is rare; pass through anyway.
            out[k] = v
            continue
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            if v.pop("__replace__", False):
                out[k] = v
            else:
                out[k] = _deep_merge(out[k], v)
        elif k in out and isinstance(out[k], list) and isinstance(v, list):
            # Append child to parent (preserving order). Use set-dedup
            # only for lists of strings to avoid double-adding identical
            # ids (e.g. required_skills); keep dict/object items as-is.
            if all(isinstance(x, str) for x in out[k] + v):
                seen: set[str] = set()
                merged: list[str] = []
                for item in out[k] + v:
                    if item not in seen:
                        merged.append(item)
                        seen.add(item)
                out[k] = merged
            else:
                out[k] = out[k] + v
        else:
            out[k] = v
    return out


def _validate_against_schema(raw: dict[str, Any], path: str) -> None:
    """Run the JSONSchema validator against the (post-merge) dict."""
    try:
        jsonschema.validate(raw, schema())
    except jsonschema.ValidationError as e:
        raise TemplateInvalidError(
            f"Template at {path} failed schema validation: {e.message} "
            f"(at {'/'.join(str(p) for p in e.absolute_path) or '<root>'})"
        ) from e


# Back-compat shim: old callers may import this name.
def _read_and_validate(path: str) -> dict[str, Any]:
    raw = _read_raw_yaml(path)
    merged = _resolve_inheritance(raw, path)
    merged.pop("extends", None)
    merged.pop("abstract", None)
    _validate_against_schema(merged, path)
    return merged

    return raw
