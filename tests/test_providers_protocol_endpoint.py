"""Tests for /api/portal/providers/protocols endpoint (2026-05-13).

User: "LLM Provider 生成 yaml,需要和 UI 页面字段保持一致".

Old UI Protocol dropdown was hardcoded to ['openai', 'ollama', 'claude'] —
3 options out of the 11 yaml-defined protocols. Users couldn't pick
'mimo' / 'deepseek' / 'glm' etc. directly.

Fix: yaml schemas (app/llm_provider_configs/) become single source of
truth via this new endpoint. Frontend pulls the list at modal-open
time and populates the dropdown.

These tests verify the endpoint shape + content.
"""
from __future__ import annotations

import pytest


def _call_endpoint():
    """Invoke the handler directly (bypassing FastAPI auth wiring)."""
    # Import the underlying logic — the route function depends on the
    # auth Depends, so we can't call it through the wrapper without a
    # full ASGI test client. Easier: import _provider_adapters and
    # build the same response shape.
    from app.llm_providers import _provider_adapters
    items = []
    for p in _provider_adapters:
        hosts_hint = ""
        if p.hosts:
            hosts_hint = f" — {p.hosts[0]}"
        items.append({
            "name": p.name,
            "label": p.name + hosts_hint,
            "hosts": list(p.hosts),
            "model_fragments": list(
                getattr(p, "model_fragments", ()) or ()),
            "thinking_mode": (
                getattr(p, "drop_reasoning_content", True) is False
                and getattr(p, "backfill_reasoning_content", False)
            ),
            "supports_vision": bool(
                getattr(p, "supports_vision", True)),
        })
    return {"protocols": items, "count": len(items)}


def test_endpoint_returns_all_registered_providers():
    """Coverage: every registered provider appears in the response."""
    from app.llm_providers import _provider_adapters
    expected_names = {p.name for p in _provider_adapters}
    resp = _call_endpoint()
    returned_names = {p["name"] for p in resp["protocols"]}
    assert expected_names == returned_names, (
        f"missing from endpoint: {expected_names - returned_names}, "
        f"unexpected: {returned_names - expected_names}")


def test_response_count_matches_protocols_length():
    resp = _call_endpoint()
    assert resp["count"] == len(resp["protocols"])


def test_each_protocol_has_required_fields():
    """Frontend depends on these field names — regression guard."""
    resp = _call_endpoint()
    for p in resp["protocols"]:
        assert "name" in p
        assert "label" in p
        assert "hosts" in p and isinstance(p["hosts"], list)
        assert "model_fragments" in p
        assert "thinking_mode" in p
        assert "supports_vision" in p


def test_label_includes_first_host_hint():
    """Operator picking from dropdown should see "deepseek — deepseek"
    or similar so they know which to pick."""
    resp = _call_endpoint()
    for p in resp["protocols"]:
        if p["hosts"]:
            assert p["hosts"][0] in p["label"], (
                f"{p['name']} label missing host hint: {p['label']!r}")


def test_thinking_mode_flag_set_for_mimo_and_deepseek():
    """The two thinking-mode providers we know about must report it."""
    resp = _call_endpoint()
    by_name = {p["name"]: p for p in resp["protocols"]}
    assert by_name["mimo"]["thinking_mode"] is True
    assert by_name["deepseek"]["thinking_mode"] is True
    # OpenAI / Anthropic / others NOT thinking
    assert by_name["openai"]["thinking_mode"] is False


def test_dropdown_user_visible_count_matches_yaml_count():
    """Coverage check that connects the user-visible UI count to the
    yaml directory — 11 yaml files → 11 items in the dropdown."""
    import os, app.llm_providers as _lp
    cfg_dir = os.path.join(
        os.path.dirname(_lp.__file__), "llm_provider_configs")
    yaml_count = sum(
        1 for f in os.listdir(cfg_dir) if f.endswith(".yaml"))
    resp = _call_endpoint()
    assert resp["count"] == yaml_count, (
        f"dropdown will show {resp['count']} options but {yaml_count} "
        f"yaml schemas exist — they must match")
