"""Phase 0 task 3 tests: ExpertProfile + SpecialtyTemplate + _config."""
from __future__ import annotations

import os
import pytest

from app.domain_expert import _config
from app.domain_expert.profile import ExpertProfile
from app.domain_expert.template import SpecialtyTemplate
from app.domain_expert.manager import get_manager


# ── ExpertProfile ──

def test_profile_roundtrip(tmp_path, monkeypatch):
    """Save then load reproduces all fields."""
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    p = ExpertProfile(
        agent_id="ag1", specialty="legal",
        template_id="legal-expert", template_version="1.0",
    )
    p.save()
    loaded = ExpertProfile.load("ag1")
    assert loaded is not None
    assert loaded.agent_id == "ag1"
    assert loaded.specialty == "legal"
    assert loaded.template_id == "legal-expert"
    assert loaded.template_version == "1.0"
    assert loaded.level == "novice"


def test_profile_load_returns_none_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    assert ExpertProfile.load("nonexistent_agent") is None


def test_profile_load_returns_none_on_corrupt_json(tmp_path, monkeypatch):
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    d = tmp_path / "expert" / "ag1"
    d.mkdir(parents=True)
    (d / "config.json").write_text("not valid json {{{")
    assert ExpertProfile.load("ag1") is None  # graceful


def test_profile_save_atomic(tmp_path, monkeypatch):
    """Save uses .tmp + os.replace, so partial writes don't corrupt."""
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    p = ExpertProfile(
        agent_id="ag2", specialty="legal",
        template_id="legal-expert", template_version="1.0",
    )
    p.save()
    target = tmp_path / "expert" / "ag2" / "config.json"
    assert target.exists()
    # No leftover .tmp
    tmp_file = tmp_path / "expert" / "ag2" / "config.json.tmp"
    assert not tmp_file.exists()


def test_profile_updated_at_advances(tmp_path, monkeypatch):
    import time as _t
    monkeypatch.setattr(_config, "expert_dir_for",
                        lambda a: str(tmp_path / "expert" / a))
    p = ExpertProfile(
        agent_id="ag3", specialty="legal",
        template_id="legal-expert", template_version="1.0",
    )
    p.save()
    first = p.updated_at
    _t.sleep(0.01)
    p.save()
    assert p.updated_at > first


# ── SpecialtyTemplate ──

def test_template_minimal():
    t = SpecialtyTemplate(
        id="legal-expert", version="1.0",
        name="法律专家", specialty="legal",
    )
    assert t.id == "legal-expert"
    assert t.specialty == "legal"
    assert t.icon == ""  # default


# ── _config feature flag ──

def test_config_disabled_default():
    """TUDOU_EXPERT_DISABLED unset → module enabled."""
    # ensure unset
    os.environ.pop(_config.DISABLED_ENV_VAR, None)
    assert _config.is_disabled() is False


def test_config_disabled_when_set(monkeypatch):
    monkeypatch.setenv(_config.DISABLED_ENV_VAR, "1")
    assert _config.is_disabled() is True


def test_config_disabled_zero_means_enabled(monkeypatch):
    """Only '1' counts as disabled — '0' or anything else = enabled."""
    monkeypatch.setenv(_config.DISABLED_ENV_VAR, "0")
    assert _config.is_disabled() is False


# ── _config paths ──

def test_expert_dir_for_requires_agent_id():
    with pytest.raises(ValueError):
        _config.expert_dir_for("")


def test_expert_dir_for_under_root():
    p = _config.expert_dir_for("test_agent_xyz")
    assert p.endswith("expert/test_agent_xyz") or p.endswith("expert\\test_agent_xyz")


def test_template_dir_resolves():
    """Template dir should be app/data/specialty_templates/."""
    td = _config.template_dir()
    assert td.endswith("specialty_templates") or td.endswith("specialty_templates" + os.sep.rstrip("/"))


# ── ExpertManager ──

def test_manager_is_singleton():
    m1 = get_manager()
    m2 = get_manager()
    assert m1 is m2


def test_manager_is_available_when_enabled(monkeypatch):
    monkeypatch.setenv(_config.DISABLED_ENV_VAR, "0")
    assert get_manager().is_available() is True


def test_manager_unavailable_when_disabled(monkeypatch):
    monkeypatch.setenv(_config.DISABLED_ENV_VAR, "1")
    assert get_manager().is_available() is False
