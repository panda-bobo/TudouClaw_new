"""Phase 0 task 4 tests: API router structure + 501/503 behaviors.

Direct unit test on the router (no live HTTP). Verifies:
  - All 12 endpoints registered
  - Each endpoint returns 501 (not implemented) when called and module enabled
  - Each endpoint returns 503 when TUDOU_EXPERT_DISABLED=1
"""
from __future__ import annotations

import os
import pytest

from app.domain_expert.api.routers import router
from app.domain_expert import _config


def test_router_prefix_and_count():
    assert router.prefix == "/api/portal"
    # Phase 0 spec declared 12 endpoints. V2 onward added more:
    #   + POST /specialty-templates       (create template)
    #   + DELETE /specialty-templates/{id} (delete template)
    #   + POST /agent/{id}/expert/lora/train (V4 step 3: queue request)
    #   + GET  /agent/{id}/expert/lora       (V4 step 3: list versions+queue)
    #   + PUT  /specialty-templates/{id}     (full CRUD: edit existing template)
    #   + GET  /agent/{id}/expert/routing    (V5: read routing config + stats)
    #   + PUT  /agent/{id}/expert/routing    (V5: update routing config)
    # If you add an endpoint, bump this number AND add to expected_paths below.
    assert len(router.routes) == 19


def test_router_paths_match_spec():
    """All registered endpoints (Phase 0 spec §5.1 + later additions)."""
    expected_paths = {
        # Phase 0 baseline
        ("GET", "/api/portal/specialty-templates"),
        ("GET", "/api/portal/specialty-templates/{template_id}"),
        ("GET", "/api/portal/agent/{agent_id}/expert"),
        ("POST", "/api/portal/agent/{agent_id}/expert/initialize"),
        ("DELETE", "/api/portal/agent/{agent_id}/expert"),
        ("POST", "/api/portal/agent/{agent_id}/expert/corpus/ingest"),
        ("GET", "/api/portal/agent/{agent_id}/expert/corpus"),
        ("POST", "/api/portal/agent/{agent_id}/expert/corpus/reindex"),
        ("POST", "/api/portal/agent/{agent_id}/expert/query"),
        ("POST", "/api/portal/agent/{agent_id}/expert/feedback"),
        ("GET", "/api/portal/agent/{agent_id}/expert/traces"),
        ("GET", "/api/portal/agent/{agent_id}/expert/stats"),
        # Template CRUD (cultivation hub UI)
        ("POST", "/api/portal/specialty-templates"),
        ("DELETE", "/api/portal/specialty-templates/{template_id}"),
        # V4 step 3: LoRA training trigger + listing
        ("POST", "/api/portal/agent/{agent_id}/expert/lora/train"),
        ("GET",  "/api/portal/agent/{agent_id}/expert/lora"),
        # Template edit (full CRUD)
        ("PUT", "/api/portal/specialty-templates/{template_id}"),
        # V5: routing config (per-agent confidence threshold / mode)
        ("GET", "/api/portal/agent/{agent_id}/expert/routing"),
        ("PUT", "/api/portal/agent/{agent_id}/expert/routing"),
    }
    actual = set()
    for r in router.routes:
        for m in (r.methods or set()):
            if m == "HEAD":
                continue
            actual.add((m, r.path))
    assert actual == expected_paths


def test_check_enabled_raises_503_when_flag_set(monkeypatch):
    monkeypatch.setenv(_config.DISABLED_ENV_VAR, "1")
    from app.domain_expert.api.routers import _check_enabled
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as excinfo:
        _check_enabled()
    assert excinfo.value.status_code == 503
    assert "disabled" in excinfo.value.detail.lower()


def test_check_enabled_passes_when_flag_unset():
    """No env var → enabled → _check_enabled returns None silently."""
    os.environ.pop(_config.DISABLED_ENV_VAR, None)
    from app.domain_expert.api.routers import _check_enabled
    # Should not raise
    _check_enabled()


def test_full_app_includes_expert_router():
    """The expert router gets registered into the full app via main.py."""
    # Import the full app builder
    from app.api.main import create_app
    app = create_app()
    # Find expert routes among all app routes
    expert_paths = [
        r.path for r in app.routes
        if getattr(r, "path", "").startswith("/api/portal")
        and ("expert" in getattr(r, "path", "") or "specialty-templates" in getattr(r, "path", ""))
    ]
    assert len(expert_paths) >= 12, (
        f"Expected ≥12 expert routes registered; got: {expert_paths}"
    )
