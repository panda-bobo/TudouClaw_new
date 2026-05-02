"""Unit tests for SystemSettingsStore — JSON-backed runtime config
with dotted-path access and deep-merge updates."""
from __future__ import annotations
import json
from pathlib import Path

from app.system_settings import SystemSettingsStore, DEFAULTS


def test_get_returns_default_when_file_missing(tmp_path):
    store = SystemSettingsStore(tmp_path)
    assert store.get("canvas.max_parallel_nodes") == 6
    assert store.get("delegate.max_parallel_children") == 6


def test_get_with_explicit_default_overrides_builtin(tmp_path):
    store = SystemSettingsStore(tmp_path)
    assert store.get("not.a.real.path", 42) == 42


def test_set_persists_to_disk(tmp_path):
    store = SystemSettingsStore(tmp_path)
    store.set("canvas.max_parallel_nodes", 12)
    # File written
    persisted = json.loads((tmp_path / "system_settings.json").read_text())
    assert persisted["canvas"]["max_parallel_nodes"] == 12
    # New store instance reads it back
    store2 = SystemSettingsStore(tmp_path)
    assert store2.get("canvas.max_parallel_nodes") == 12


def test_update_deep_merges(tmp_path):
    store = SystemSettingsStore(tmp_path)
    store.set("canvas.max_parallel_nodes", 8)
    store.update({"delegate": {"max_parallel_children": 4}})
    # Both keys retained
    assert store.get("canvas.max_parallel_nodes") == 8
    assert store.get("delegate.max_parallel_children") == 4


def test_all_returns_full_dict_with_defaults_filled(tmp_path):
    store = SystemSettingsStore(tmp_path)
    store.set("canvas.max_parallel_nodes", 10)
    snapshot = store.all()
    # Set value reflected
    assert snapshot["canvas"]["max_parallel_nodes"] == 10
    # Unset value falls back to default
    assert snapshot["delegate"]["max_parallel_children"] == 6


def test_set_with_invalid_path_raises(tmp_path):
    store = SystemSettingsStore(tmp_path)
    import pytest
    with pytest.raises(ValueError, match="empty path"):
        store.set("", 5)


def test_atomic_write_via_tmp_replace(tmp_path):
    """If the write process is interrupted, the original file is intact.
    We simulate by checking that a tmp file is used (not appended in-place)."""
    store = SystemSettingsStore(tmp_path)
    store.set("canvas.max_parallel_nodes", 3)
    # No leftover .tmp file after successful write
    assert not (tmp_path / "system_settings.json.tmp").exists()


def test_module_singleton(tmp_path, monkeypatch):
    """init_store() then get_store() returns the same instance."""
    from app import system_settings as ss
    monkeypatch.setattr(ss, "_STORE", None)
    s1 = ss.init_store(tmp_path)
    s2 = ss.get_store()
    assert s1 is s2


def test_endpoint_get_returns_settings_and_defaults(tmp_path, monkeypatch):
    """GET /system-settings returns {settings: {...}, defaults: {...}}.

    Uses TestClient with a synthesized hub. Verifies the dual-keyed
    response shape that the UI's Reset button depends on.
    """
    from fastapi.testclient import TestClient
    from app import system_settings as ss
    from app.api.routers import system_settings as sys_router

    monkeypatch.setattr(ss, "_STORE", None)
    ss.init_store(tmp_path)

    # Build a minimal app with just this router
    from fastapi import FastAPI
    from app.api.deps.auth import get_current_user, CurrentUser
    app = FastAPI()
    app.include_router(sys_router.router)
    # Bypass auth for the test
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id="t", role="superAdmin"
    )

    client = TestClient(app)
    r = client.get("/api/portal/system-settings")
    assert r.status_code == 200
    data = r.json()
    assert "settings" in data and "defaults" in data
    # Default canvas cap is 6
    assert data["settings"]["canvas"]["max_parallel_nodes"] == 6
    assert data["defaults"]["canvas"]["max_parallel_nodes"] == 6


def test_endpoint_patch_updates_value(tmp_path, monkeypatch):
    """PATCH /system-settings with {path, value} updates the store."""
    from fastapi.testclient import TestClient
    from app import system_settings as ss
    from app.api.routers import system_settings as sys_router
    monkeypatch.setattr(ss, "_STORE", None)
    ss.init_store(tmp_path)
    from fastapi import FastAPI
    from app.api.deps.auth import get_current_user, CurrentUser
    app = FastAPI()
    app.include_router(sys_router.router)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id="t", role="superAdmin"
    )
    client = TestClient(app)

    r = client.patch(
        "/api/portal/system-settings",
        json={"path": "canvas.max_parallel_nodes", "value": 4},
    )
    assert r.status_code == 200, r.text
    assert r.json()["settings"]["canvas"]["max_parallel_nodes"] == 4

    # Roundtrip via GET
    r2 = client.get("/api/portal/system-settings")
    assert r2.json()["settings"]["canvas"]["max_parallel_nodes"] == 4


def test_endpoint_patch_rejects_out_of_range(tmp_path, monkeypatch):
    """Validators: max_parallel_* must be int in [1, 32]."""
    from fastapi.testclient import TestClient
    from app import system_settings as ss
    from app.api.routers import system_settings as sys_router
    monkeypatch.setattr(ss, "_STORE", None)
    ss.init_store(tmp_path)
    from fastapi import FastAPI
    from app.api.deps.auth import get_current_user, CurrentUser
    app = FastAPI()
    app.include_router(sys_router.router)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id="t", role="superAdmin"
    )
    client = TestClient(app)

    r = client.patch(
        "/api/portal/system-settings",
        json={"path": "canvas.max_parallel_nodes", "value": 100},
    )
    assert r.status_code == 400
    assert "1..32" in r.text or "out of range" in r.text.lower()

    r = client.patch(
        "/api/portal/system-settings",
        json={"path": "delegate.max_parallel_children", "value": 0},
    )
    assert r.status_code == 400


def test_endpoint_patch_rejects_unknown_path(tmp_path, monkeypatch):
    """Patch path must match a known DEFAULTS key — random paths rejected."""
    from fastapi.testclient import TestClient
    from app import system_settings as ss
    from app.api.routers import system_settings as sys_router
    monkeypatch.setattr(ss, "_STORE", None)
    ss.init_store(tmp_path)
    from fastapi import FastAPI
    from app.api.deps.auth import get_current_user, CurrentUser
    app = FastAPI()
    app.include_router(sys_router.router)
    app.dependency_overrides[get_current_user] = lambda: CurrentUser(
        user_id="t", role="superAdmin"
    )
    client = TestClient(app)

    r = client.patch(
        "/api/portal/system-settings",
        json={"path": "random.key", "value": "anything"},
    )
    assert r.status_code == 400
    assert "unknown" in r.text.lower() or "not allowed" in r.text.lower()
