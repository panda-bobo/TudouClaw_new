"""
TaskTemplate loader — reads YAML from ~/.tudou_claw/v2/templates/ and
the bundled ``app/v2/templates/data/*.yaml`` defaults (PRD §12).

No dataclass yet — returns plain dicts. A proper TaskTemplate
dataclass will land when Intake / Plan / Verify handlers need typed
access to ``required_slots``, ``plan_prompt``, ``verify_rules`` etc.
"""
from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

try:
    import yaml  # PyYAML is already a project dep (used elsewhere)
except ImportError:  # pragma: no cover
    yaml = None   # type: ignore[assignment]


_BUILTIN_DIR = os.path.join(os.path.dirname(__file__), "data")


def _user_dir() -> str:
    from app import DEFAULT_DATA_DIR
    return os.path.join(DEFAULT_DATA_DIR, "v2", "templates")


def _load_yaml(path: str) -> dict | None:
    if yaml is None:
        raise RuntimeError("PyYAML is required to load V2 task templates")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        return None
    if not isinstance(data, dict):
        return None
    return data


def list_templates() -> list[dict]:
    """Return metadata for all available templates (builtin + user)."""
    seen: dict[str, dict] = {}
    # Builtin first — user overrides by id.
    for src in (_BUILTIN_DIR, _user_dir()):
        if not os.path.isdir(src):
            continue
        for name in sorted(os.listdir(src)):
            if not name.endswith((".yaml", ".yml")):
                continue
            data = _load_yaml(os.path.join(src, name))
            if not data:
                continue
            tid = str(data.get("id") or os.path.splitext(name)[0])
            data["id"] = tid
            seen[tid] = data
    return list(seen.values())


def get_template(template_id: str) -> dict | None:
    """Look up a template by id (user dir overrides builtin)."""
    for src in (_user_dir(), _BUILTIN_DIR):
        candidate = os.path.join(src, f"{template_id}.yaml")
        data = _load_yaml(candidate)
        if data:
            data.setdefault("id", template_id)
            return data
    return None


__all__ = ["list_templates", "get_template"]
