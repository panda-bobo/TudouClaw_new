"""Track D Task D2 — YAML loader: parse + schema validate + cache."""
from __future__ import annotations

import os
import time

import pytest

from app.domain_expert import _config, template_loader
from app.domain_expert.template import SpecialtyTemplate
from app.domain_expert.template_loader import (
    TemplateInvalidError,
    TemplateNotFoundError,
    invalidate_cache,
    list_available,
    load,
    load_all,
)


# ── Test fixtures ──

@pytest.fixture
def tpl_dir(tmp_path, monkeypatch):
    """Redirect _config.template_dir() to a tmp dir + clear cache."""
    d = tmp_path / "templates"
    d.mkdir()
    monkeypatch.setattr(_config, "template_dir", lambda: str(d))
    invalidate_cache()
    yield d
    invalidate_cache()


def _write(d, name, body):
    (d / f"{name}.yaml").write_text(body, encoding="utf-8")


VALID_LEGAL = """\
id: legal-expert
version: "1.0"
name: 法律专家
specialty: legal
required_packs:
  - agency_legal_lawyer
eval_suite:
  - runner_id: legalbench_zh
    weight: 0.5
    threshold: 0.5
"""


# ── load() happy paths ──

def test_load_returns_specialty_template(tpl_dir):
    _write(tpl_dir, "legal", VALID_LEGAL)
    t = load("legal")
    assert isinstance(t, SpecialtyTemplate)
    assert t.id == "legal-expert"
    assert t.specialty == "legal"
    assert t.required_packs == ["agency_legal_lawyer"]
    assert t.eval_suite[0].runner_id == "legalbench_zh"


def test_load_caches_by_mtime(tpl_dir):
    _write(tpl_dir, "legal", VALID_LEGAL)
    t1 = load("legal")
    t2 = load("legal")
    assert t1 is t2  # cache hit returns identical instance


def test_load_invalidates_when_file_changes(tpl_dir):
    _write(tpl_dir, "legal", VALID_LEGAL)
    t1 = load("legal")
    # Bump mtime AND change content
    time.sleep(0.05)
    altered = VALID_LEGAL.replace("法律专家", "Legal Expert")
    _write(tpl_dir, "legal", altered)
    # Force a distinct mtime in case fs resolution is coarse
    new_mtime = time.time() + 1
    os.utime(str(tpl_dir / "legal.yaml"), (new_mtime, new_mtime))
    t2 = load("legal")
    assert t1 is not t2
    assert t2.name == "Legal Expert"


def test_invalidate_cache_forces_reload(tpl_dir):
    _write(tpl_dir, "legal", VALID_LEGAL)
    t1 = load("legal")
    invalidate_cache()
    t2 = load("legal")
    # Same content → equal but distinct objects after cache flush
    assert t1 == t2
    assert t1 is not t2


# ── load() error cases ──

def test_load_raises_when_file_missing(tpl_dir):
    with pytest.raises(TemplateNotFoundError):
        load("nonexistent")


def test_load_raises_value_error_for_empty_specialty(tpl_dir):
    with pytest.raises(ValueError):
        load("")


def test_load_raises_invalid_for_bad_yaml(tpl_dir):
    _write(tpl_dir, "legal", "id: legal-expert\n  bad indent: : :")
    with pytest.raises(TemplateInvalidError, match="not valid YAML"):
        load("legal")


def test_load_raises_invalid_for_empty_file(tpl_dir):
    _write(tpl_dir, "legal", "")
    with pytest.raises(TemplateInvalidError, match="empty"):
        load("legal")


def test_load_raises_invalid_for_non_mapping_root(tpl_dir):
    _write(tpl_dir, "legal", "- just\n- a\n- list\n")
    with pytest.raises(TemplateInvalidError, match="must be a mapping"):
        load("legal")


def test_load_raises_invalid_when_schema_fails(tpl_dir):
    bad = """\
id: legal-expert
version: "not-a-version"
name: x
specialty: x
"""
    _write(tpl_dir, "legal", bad)
    with pytest.raises(TemplateInvalidError, match="schema validation"):
        load("legal")


def test_load_raises_invalid_when_required_field_missing(tpl_dir):
    bad = """\
id: legal-expert
version: "1.0"
name: x
"""
    _write(tpl_dir, "legal", bad)
    with pytest.raises(TemplateInvalidError):
        load("legal")


# ── list_available + load_all ──

def test_list_available_empty(tpl_dir):
    assert list_available() == []


def test_list_available_returns_sorted_specialties(tpl_dir):
    _write(tpl_dir, "legal", VALID_LEGAL)
    _write(tpl_dir, "medical", VALID_LEGAL.replace("legal", "medical"))
    # Files starting with _ should be skipped (e.g. _shared.yaml)
    _write(tpl_dir, "_shared", VALID_LEGAL)
    assert list_available() == ["legal", "medical"]


def test_list_available_when_dir_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(_config, "template_dir",
                        lambda: str(tmp_path / "does_not_exist"))
    assert list_available() == []


def test_load_all_loads_every_template(tpl_dir):
    _write(tpl_dir, "legal", VALID_LEGAL)
    _write(tpl_dir, "medical", VALID_LEGAL
           .replace("legal-expert", "medical-expert")
           .replace("specialty: legal", "specialty: medical")
           .replace("法律专家", "医学专家"))
    out = load_all()
    assert len(out) == 2
    ids = {t.specialty for t in out}
    assert ids == {"legal", "medical"}


def test_load_all_propagates_invalid_template(tpl_dir):
    _write(tpl_dir, "legal", VALID_LEGAL)
    _write(tpl_dir, "broken", "id: x\nversion: bad\nname: x\nspecialty: x\n")
    with pytest.raises(TemplateInvalidError):
        load_all()
