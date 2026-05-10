"""R4 — Agent._load_specialty_template resolves sub-specialty templates.

Regression guard: bundle_apply stamps ``agent.expert_specialty`` from
``template.specialty`` (e.g. "civil_law"), but the template-loader keys
are *paths* (e.g. "legal/civil_law"). Without the load_all() fallback,
cultivated agents whose template lives under a sub-directory get a
silent None back from _load_specialty_template — meaning red-line
checks and PromptBlock front-loading both no-op without warning.

End-to-end smoke test of this lookup ran against a live server in R4
verification (commit 025c323 + this fix).
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.agent import Agent
from app.domain_expert import _config


# ── Fixtures ──

@pytest.fixture
def tpl_dir(tmp_path, monkeypatch):
    """Redirect _config.template_dir() to a tmp dir + clear loader cache."""
    d = tmp_path / "templates"
    d.mkdir()
    monkeypatch.setattr(_config, "template_dir", lambda: str(d))
    from app.domain_expert.template_loader import invalidate_cache
    invalidate_cache()
    yield d
    invalidate_cache()


def _write_template(parent_dir, key: str, body: str):
    """Write a YAML template at <tpl_dir>/<key>.yaml. ``key`` may
    include slashes for nested sub-specialty paths."""
    import os
    full = parent_dir / f"{key}.yaml"
    os.makedirs(os.path.dirname(str(full)) or str(parent_dir), exist_ok=True)
    full.write_text(body, encoding="utf-8")


# ── Direct (filename matches specialty) ──

def test_loads_top_level_template_by_specialty_name(tpl_dir):
    """specialty="legal" + legal.yaml present → direct load() works."""
    _write_template(tpl_dir, "legal", """\
id: legal-expert
version: "1.0"
name: 法律专家
specialty: legal
""")
    a = Agent(id="ag1", name="Test")
    a.expert_specialty = "legal"
    a.expert_template_version = "1.0"
    tpl = a._load_specialty_template()
    assert tpl is not None
    assert tpl.id == "legal-expert"
    assert tpl.specialty == "legal"


# ── Sub-specialty fallback ──

def test_loads_sub_specialty_via_load_all_fallback(tpl_dir):
    """specialty="civil_law" but file lives at legal/civil_law.yaml →
    direct load("civil_law") fails, fallback scan finds it via
    .specialty match."""
    import os
    os.makedirs(str(tpl_dir / "legal"), exist_ok=True)
    _write_template(tpl_dir, "legal/civil_law", """\
id: civil-law-expert
version: "1.0"
name: 民法专家
specialty: civil_law
""")
    a = Agent(id="ag1", name="Test")
    a.expert_specialty = "civil_law"
    a.expert_template_version = "1.0"
    tpl = a._load_specialty_template()
    assert tpl is not None
    assert tpl.specialty == "civil_law"
    assert tpl.id == "civil-law-expert"


def test_returns_none_when_no_template_matches(tpl_dir):
    """No template on disk has the requested specialty → None,
    no exception."""
    _write_template(tpl_dir, "legal", """\
id: legal-expert
version: "1.0"
name: 法律专家
specialty: legal
""")
    a = Agent(id="ag1", name="Test")
    a.expert_specialty = "ghost_specialty"
    a.expert_template_version = "1.0"
    assert a._load_specialty_template() is None


def test_returns_none_for_uncultivated_agent(tpl_dir):
    """Empty expert_specialty → no IO, no scan, just None."""
    a = Agent(id="ag1", name="Test")
    a.expert_specialty = ""
    assert a._load_specialty_template() is None


# ── Caching ──

def test_caches_by_specialty_and_version(tpl_dir):
    """Re-calling with the same (specialty, version) returns the
    same instance — no re-load from disk."""
    _write_template(tpl_dir, "legal", """\
id: legal-expert
version: "1.0"
name: 法律专家
specialty: legal
""")
    a = Agent(id="ag1", name="Test")
    a.expert_specialty = "legal"
    a.expert_template_version = "1.0"
    t1 = a._load_specialty_template()
    t2 = a._load_specialty_template()
    assert t1 is t2


def test_cache_invalidates_on_version_bump(tpl_dir):
    """Bumping expert_template_version (re-cultivation) reloads."""
    _write_template(tpl_dir, "legal", """\
id: legal-expert
version: "1.0"
name: 法律专家
specialty: legal
""")
    a = Agent(id="ag1", name="Test")
    a.expert_specialty = "legal"
    a.expert_template_version = "1.0"
    t1 = a._load_specialty_template()

    # Simulate re-cultivation to a new version
    a.expert_template_version = "2.0"
    # Update the template on disk so the new load picks up the change
    _write_template(tpl_dir, "legal", """\
id: legal-expert
version: "2.0"
name: 法律专家 v2
specialty: legal
""")
    from app.domain_expert.template_loader import invalidate_cache
    invalidate_cache()  # also drop loader's mtime cache for the rewrite

    t2 = a._load_specialty_template()
    assert t2 is not t1
    assert t2.version == "2.0"
