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
    return os.path.join(_config.template_dir(), f"{specialty}.yaml")


def list_available() -> list[str]:
    """Return sorted list of specialty ids that have a YAML on disk.

    Just inspects filenames — does NOT validate the contents. Use
    :func:`load_all` if you also want validation.
    """
    d = _config.template_dir()
    if not os.path.isdir(d):
        return []
    out: list[str] = []
    for fn in os.listdir(d):
        if fn.endswith(".yaml") and not fn.startswith("_"):
            out.append(fn[: -len(".yaml")])
    out.sort()
    return out


def load(specialty: str) -> SpecialtyTemplate:
    """Load a single specialty template by name (e.g. ``"legal"``).

    Returns a populated :class:`SpecialtyTemplate`. Raises
    :class:`TemplateNotFoundError` if the file doesn't exist or
    :class:`TemplateInvalidError` on parse/validation failures.
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

    raw = _read_and_validate(path)
    try:
        tpl = SpecialtyTemplate.from_dict(raw)
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

def _read_and_validate(path: str) -> dict[str, Any]:
    """Open file → YAML parse → JSONSchema validate → return dict."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except yaml.YAMLError as e:
        raise TemplateInvalidError(
            f"Template at {path} is not valid YAML: {e}"
        ) from e
    except OSError as e:
        # File disappeared between exists check and open, or perms error
        raise TemplateInvalidError(
            f"Template at {path} could not be read: {e}"
        ) from e

    if raw is None:
        raise TemplateInvalidError(
            f"Template at {path} is empty"
        )
    if not isinstance(raw, dict):
        raise TemplateInvalidError(
            f"Template at {path} must be a mapping, got {type(raw).__name__}"
        )

    try:
        jsonschema.validate(raw, schema())
    except jsonschema.ValidationError as e:
        raise TemplateInvalidError(
            f"Template at {path} failed schema validation: {e.message} "
            f"(at {'/'.join(str(p) for p in e.absolute_path) or '<root>'})"
        ) from e

    return raw
