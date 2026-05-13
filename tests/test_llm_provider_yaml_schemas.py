"""Tests for the per-provider yaml schemas in app/llm_provider_configs/.

User: "我们每个 LLM provider 都有个 schema". Convention is now that
every registered LLMProvider has a same-named yaml file in
app/llm_provider_configs/ even if all fields equal the Python class
defaults — so listing the directory tells you what providers ship +
what their quirks are.

These tests verify:
  1. Every registered provider has a corresponding yaml
  2. Each yaml only contains keys that are in _OVERLAY_KEYS (no typos)
  3. Loading the yaml at provider __init__ doesn't crash
  4. After loading, provider behaviour matches what the yaml declares
     (regression guard against silent yaml drift)
"""
from __future__ import annotations

import os
import pytest

import yaml

from app import llm_providers


_CONFIG_DIR = os.path.join(
    os.path.dirname(llm_providers.__file__), "llm_provider_configs")


def _registered_provider_names() -> list[str]:
    return [p.name for p in llm_providers._provider_adapters]


# ── Coverage: every provider has a yaml ─────────────────────────────

def test_every_provider_has_yaml():
    """Convention: each registered provider has a matching yaml."""
    yaml_files = {
        f[:-5] for f in os.listdir(_CONFIG_DIR)
        if f.endswith(".yaml")
    }
    missing = [p for p in _registered_provider_names()
               if p not in yaml_files]
    assert not missing, (
        f"providers without yaml schema: {missing}\n"
        f"create app/llm_provider_configs/<name>.yaml for each")


def test_every_yaml_has_a_provider():
    """Inverse — no orphan yamls for unregistered providers."""
    yaml_files = {
        f[:-5] for f in os.listdir(_CONFIG_DIR)
        if f.endswith(".yaml")
    }
    registered = set(_registered_provider_names())
    orphans = yaml_files - registered
    assert not orphans, (
        f"yaml without registered Python class: {orphans}")


# ── Schema validity: only whitelisted keys ──────────────────────────

@pytest.mark.parametrize("name", [
    "mimo", "deepseek", "glm", "qwen", "volces",
    "openai", "anthropic", "ollama", "lmstudio",
])
def test_yaml_only_contains_whitelisted_keys(name):
    path = os.path.join(_CONFIG_DIR, f"{name}.yaml")
    if not os.path.exists(path):
        pytest.skip(f"{name}.yaml not yet written")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    bad = [k for k in data.keys()
           if k not in llm_providers.LLMProvider._OVERLAY_KEYS]
    assert not bad, (
        f"{name}.yaml has un-whitelisted keys: {bad}. "
        f"Either add to _OVERLAY_KEYS or fix the typo.")


@pytest.mark.parametrize("name", [
    "mimo", "deepseek", "glm", "qwen", "volces",
    "openai", "anthropic", "ollama", "lmstudio",
])
def test_yaml_loads_without_error(name):
    """Each yaml is valid YAML + parses to a dict."""
    path = os.path.join(_CONFIG_DIR, f"{name}.yaml")
    if not os.path.exists(path):
        pytest.skip(f"{name}.yaml not yet written")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    assert isinstance(data, dict), (
        f"{name}.yaml didn't parse as a dict: {type(data).__name__}")


# ── Per-provider behavioural assertions ─────────────────────────────

def _provider_by_name(name: str):
    for p in llm_providers._provider_adapters:
        if p.name == name:
            return p
    raise KeyError(name)


def test_mimo_thinking_mode_config_applied():
    p = _provider_by_name("mimo")
    assert p.drop_reasoning_content is False
    assert p.backfill_reasoning_content is True
    assert p.drop_empty_content_with_tools is True
    assert "xiaomimimo.com" in p.hosts
    assert "mimo" in p.model_fragments


def test_deepseek_thinking_mode_config_applied():
    p = _provider_by_name("deepseek")
    assert p.drop_reasoning_content is False
    assert p.backfill_reasoning_content is True
    assert "deepseek" in p.model_fragments


def test_glm_quirks_applied():
    p = _provider_by_name("glm")
    assert p.coerce_list_content_to_string is True


def test_openai_baseline():
    p = _provider_by_name("openai")
    # OpenAI is the baseline — defaults
    assert p.drop_reasoning_content is True
    assert p.backfill_reasoning_content is False
    assert p.supports_vision is True


def test_yaml_can_route_by_model_alone():
    """Sanity: model_fragments lets us route MiMo-style URLs that
    don't match hosts (e.g. proxy).  No URL but mimo model →
    MiMo provider."""
    adapter = llm_providers.resolve_strategy("", "mimo-v2.5-pro")
    assert adapter.name == "mimo"


def test_url_match_still_wins_over_model():
    """If both URL and model could match different providers, URL wins
    (preserves old behavior)."""
    # OpenAI URL but mimo model — URL takes precedence
    adapter = llm_providers.resolve_strategy(
        "https://api.openai.com/v1/chat", "mimo-v2.5-pro")
    assert adapter.name == "openai"


def test_overlay_key_includes_model_fragments():
    """Regression guard: the new model_fragments key must be in the
    overlay whitelist or yaml-driven fragments won't apply."""
    assert "model_fragments" in llm_providers.LLMProvider._OVERLAY_KEYS


def test_yaml_list_to_tuple_normalization():
    """Schemas use list syntax (yaml-friendly); loader normalizes to
    tuple. Verify this for both hosts and model_fragments."""
    for name in ("mimo", "deepseek"):
        try:
            p = _provider_by_name(name)
        except KeyError:
            continue
        assert isinstance(p.hosts, tuple), (
            f"{name}.hosts should be tuple after yaml load")
        assert isinstance(p.model_fragments, tuple), (
            f"{name}.model_fragments should be tuple after yaml load")
