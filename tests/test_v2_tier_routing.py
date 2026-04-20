"""Tests for ``app.v2.bridges.llm_tier_routing``.

Resolution now prefers the V1 ``ProviderRegistry`` (what the UI writes
to), with environment variables as an emergency escape and
``("", "")`` as the "let V1 pick a default" fall-through.
"""
from __future__ import annotations

import pytest

from app.v2.bridges.llm_tier_routing import resolve_tier, known_tiers


# ── pure fall-through behaviour (no registry provider matches) ────────


def test_empty_tier_falls_through():
    assert resolve_tier("") == ("", "")
    assert resolve_tier(None) == ("", "")


def test_unknown_tier_falls_through(monkeypatch):
    # Clear any stray env for the tier we test.
    monkeypatch.delenv("TUDOU_LLM_TIER_NO_SUCH_TIER_XYZ", raising=False)
    assert resolve_tier("no_such_tier_xyz") == ("", "")


def test_tier_name_stripped(monkeypatch):
    """Whitespace around tier names is tolerated; registry lookup should
    use the same key with both calls."""
    monkeypatch.setenv("TUDOU_LLM_TIER_CODING_STRONG", "prov_x:model_y")
    assert resolve_tier("  coding_strong  ") == ("prov_x", "model_y")


# ── env escape hatch ──────────────────────────────────────────────────


def test_env_override(monkeypatch):
    monkeypatch.setenv("TUDOU_LLM_TIER_CODING_STRONG", "ollama:my-model")
    assert resolve_tier("coding_strong") == ("ollama", "my-model")


def test_env_escape_for_unknown_tier(monkeypatch):
    """Env lets operators add tier bindings even without a provider
    entry — useful for CI / headless test setups."""
    monkeypatch.setenv("TUDOU_LLM_TIER_NEW_SPECIAL", "provX:modelY")
    assert resolve_tier("new_special") == ("provX", "modelY")


def test_malformed_env_is_ignored(monkeypatch):
    """Missing ':' in the env value → treat as unset and fall through.
    Without registry or valid env, resolution returns ("", "")."""
    monkeypatch.setenv("TUDOU_LLM_TIER_JUST_GIBBERISH", "oops-no-colon")
    assert resolve_tier("just_gibberish") == ("", "")


# ── registry is preferred over env ────────────────────────────────────


def test_registry_takes_precedence_over_env(monkeypatch):
    """If a V1 provider declares it serves the tier, env is NOT consulted."""
    # Fake a registry that serves "coding_strong".
    class _FakeEntry:
        id = "fake_provider_id"
        enabled = True
        priority = 1
        tier_models = {"coding_strong": "llama3-70b"}

    class _FakeReg:
        def pick_for_tier(self, tier):
            if tier == "coding_strong":
                return (_FakeEntry(), _FakeEntry.tier_models[tier])
            return None
        def list(self, include_disabled=False):
            return [_FakeEntry()]

    import app.llm as _llm
    monkeypatch.setattr(_llm, "get_registry", lambda: _FakeReg())
    monkeypatch.setenv("TUDOU_LLM_TIER_CODING_STRONG", "should_be_ignored:x")

    assert resolve_tier("coding_strong") == ("fake_provider_id", "llama3-70b")


def test_registry_miss_falls_through_to_env(monkeypatch):
    """Registry returns None for the tier → env is consulted next."""
    class _EmptyReg:
        def pick_for_tier(self, _): return None
        def list(self, include_disabled=False): return []

    import app.llm as _llm
    monkeypatch.setattr(_llm, "get_registry", lambda: _EmptyReg())
    monkeypatch.setenv("TUDOU_LLM_TIER_FAST_CHEAP", "ollama:tinyllama")

    assert resolve_tier("fast_cheap") == ("ollama", "tinyllama")


# ── known_tiers surface ───────────────────────────────────────────────


def test_known_tiers_contains_defaults():
    tiers = known_tiers()
    for t in ("default", "reasoning_strong", "coding_strong", "fast_cheap"):
        assert t in tiers


def test_known_tiers_merges_provider_custom_tiers(monkeypatch):
    """A provider can declare a brand-new tier name in ``tier_models`` —
    it should show up in the UI's known-tier list."""
    class _E:
        enabled = True
        priority = 5
        tier_models = {"my_custom_tier": "m", "default": "m"}

    class _Reg:
        def pick_for_tier(self, _): return None
        def list(self, include_disabled=False): return [_E()]

    import app.llm as _llm
    monkeypatch.setattr(_llm, "get_registry", lambda: _Reg())
    tiers = known_tiers()
    assert "my_custom_tier" in tiers
