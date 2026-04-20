"""Tests for the V2 tier-bindings REST endpoints.

These run against an isolated FastAPI app that exposes only
``app.api.routers.v2``. We shim a minimal V1 ProviderRegistry via
monkeypatch so the tests don't touch disk or the real providers.json.
"""
from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def v2_providers_client(monkeypatch, tmp_path):
    """Isolated client with a fake V1 ProviderRegistry in place."""
    # Isolate V2 SQLite.
    monkeypatch.setenv("TUDOU_CLAW_DB_PATH", str(tmp_path / "v2.db"))
    import app.v2.core.task_store as ts_mod
    monkeypatch.setattr(ts_mod, "_STORE", None)

    import app.api.routers.v2 as v2mod
    monkeypatch.setattr(v2mod, "_bus_singleton", None)

    # Minimal fake registry mirroring the methods the router calls.
    class _Entry:
        def __init__(self, id, name="N", kind="openai", base_url="",
                     enabled=True, tier_models=None,
                     supports_multimodal=False, models=None,
                     manual=None):
            self.id = id
            self.name = name
            self.kind = kind
            self.base_url = base_url
            self.enabled = enabled
            self.tier_models = dict(tier_models or {})
            self.supports_multimodal = supports_multimodal
            self.models_cache = list(models or [])
            self.manual_models = list(manual or [])
            self.priority = 10

    class _FakeReg:
        def __init__(self):
            self._ps: dict[str, _Entry] = {
                "prov_a": _Entry(
                    "prov_a", name="Local MLX",
                    base_url="http://localhost:10240/v1",
                    models=["qwen-30b", "qwen-vl"],
                    tier_models={"default": "qwen-30b"},
                ),
                "prov_b": _Entry(
                    "prov_b", name="OpenAI",
                    base_url="https://api.openai.com/v1",
                    models=["gpt-4o", "gpt-4o-mini"],
                    supports_multimodal=True,
                ),
            }

        def list(self, include_disabled=False):
            out = list(self._ps.values())
            if not include_disabled:
                out = [p for p in out if p.enabled]
            return out

        def get(self, pid): return self._ps.get(pid)

        def update(self, pid, **kw):
            p = self._ps.get(pid)
            if p is None: return None
            for k, v in kw.items():
                if hasattr(p, k):
                    setattr(p, k, v)
            return p

        def detect_models(self, pid, timeout=10.0):
            p = self._ps.get(pid)
            if p is None: return []
            p.models_cache = ["detected-1", "detected-2"]
            return p.models_cache

        def pick_for_tier(self, tier): return None

    reg = _FakeReg()
    import app.llm as _llm
    monkeypatch.setattr(_llm, "get_registry", lambda: reg)

    # Build the test app.
    from app.api.deps.auth import get_current_user, CurrentUser

    async def _fake_user():
        return CurrentUser(user_id="u1", role="superAdmin")

    app = FastAPI()
    app.dependency_overrides[get_current_user] = _fake_user
    app.dependency_overrides[v2mod._sse_auth_dep] = _fake_user
    app.include_router(v2mod.router)

    with TestClient(app) as client:
        yield client, reg


# ── GET /providers ────────────────────────────────────────────────────


def test_list_providers_returns_summary(v2_providers_client):
    client, reg = v2_providers_client
    r = client.get("/api/v2/providers")
    assert r.status_code == 200
    body = r.json()
    ids = [p["id"] for p in body["providers"]]
    assert set(ids) == {"prov_a", "prov_b"}
    prov_b = next(p for p in body["providers"] if p["id"] == "prov_b")
    assert prov_b["supports_multimodal"] is True
    assert "gpt-4o" in prov_b["models"]


# ── PATCH /providers/{id}/tiers ───────────────────────────────────────


def test_patch_tiers_updates_bindings(v2_providers_client):
    client, reg = v2_providers_client
    r = client.patch(
        "/api/v2/providers/prov_a/tiers",
        json={
            "tier_models": {"coding_strong": "qwen-30b", "vision": "qwen-vl"},
            "supports_multimodal": True,
        },
    )
    assert r.status_code == 200
    prov = r.json()["provider"]
    assert prov["tier_models"] == {"coding_strong": "qwen-30b",
                                    "vision": "qwen-vl"}
    assert prov["supports_multimodal"] is True


def test_patch_tiers_drops_empty_entries(v2_providers_client):
    """Empty-string keys/values are silently dropped — they'd break the
    registry anyway."""
    client, _ = v2_providers_client
    r = client.patch(
        "/api/v2/providers/prov_a/tiers",
        json={"tier_models": {"": "x", "coding_strong": ""}},
    )
    assert r.status_code == 200
    assert r.json()["provider"]["tier_models"] == {}


def test_patch_tiers_rejects_empty_body(v2_providers_client):
    client, _ = v2_providers_client
    r = client.patch("/api/v2/providers/prov_a/tiers", json={"irrelevant": 1})
    assert r.status_code == 400
    assert r.json()["detail"]["error_code"] == "INVALID_BODY"


def test_patch_tiers_404(v2_providers_client):
    client, _ = v2_providers_client
    r = client.patch("/api/v2/providers/nope/tiers",
                     json={"tier_models": {"default": "x"}})
    assert r.status_code == 404


def test_patch_tiers_requires_admin(v2_providers_client, monkeypatch):
    client, _ = v2_providers_client
    from app.api.deps.auth import get_current_user, CurrentUser

    async def _member():
        return CurrentUser(user_id="u2", role="member")

    client.app.dependency_overrides[get_current_user] = _member
    r = client.patch("/api/v2/providers/prov_a/tiers",
                     json={"tier_models": {"default": "x"}})
    assert r.status_code == 403


# ── POST /providers/{id}/detect-models ────────────────────────────────


def test_detect_models(v2_providers_client):
    client, _ = v2_providers_client
    r = client.post("/api/v2/providers/prov_a/detect-models")
    assert r.status_code == 200
    assert "detected-1" in r.json()["models"]


# ── GET /tiers ────────────────────────────────────────────────────────


def test_list_tier_catalog(v2_providers_client):
    client, _ = v2_providers_client
    r = client.get("/api/v2/tiers")
    assert r.status_code == 200
    tiers = r.json()["tiers"]
    # All well-known tiers must appear.
    for expected in ("default", "coding_strong", "vision", "fast_cheap"):
        assert expected in tiers
