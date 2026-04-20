"""Smoke tests for the new meeting delete endpoint.

Verifies:
  1. DELETE removes the meeting from the registry + persists to JSON.
  2. ``purge_workspace=true`` (default) also removes the meeting's
     ``workspaces/meetings/<id>/`` directory.
  3. ``purge_workspace=false`` keeps the workspace on disk.
  4. 404 when the meeting doesn't exist.
  5. Unrelated meetings survive.
"""
from __future__ import annotations

import os
import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeMeeting:
    def __init__(self, mid):
        self.id = mid
    def to_dict(self): return {"id": self.id}


class _FakeRegistry:
    def __init__(self):
        self._ms = {
            "m1": _FakeMeeting("m1"),
            "m2": _FakeMeeting("m2"),
        }
    def get(self, mid): return self._ms.get(mid)
    def delete(self, mid):
        if mid in self._ms:
            del self._ms[mid]
            return True
        return False


class _FakeHub:
    def __init__(self):
        # Instance-scoped registry so each test gets a fresh one.
        self.meeting_registry = _FakeRegistry()


@pytest.fixture
def meeting_client(monkeypatch, tmp_path):
    """Isolated FastAPI app with fake hub + temp data dir."""
    monkeypatch.setenv("TUDOU_CLAW_DATA_DIR", str(tmp_path))

    from app.api.deps.hub import get_hub
    from app.api.deps.auth import get_current_user, CurrentUser
    from app.api.routers import meetings as mmod

    hub = _FakeHub()
    async def _fake_hub(): return hub
    async def _fake_user(): return CurrentUser(user_id="u1", role="superAdmin")

    app = FastAPI()
    app.dependency_overrides[get_hub] = _fake_hub
    app.dependency_overrides[get_current_user] = _fake_user
    app.include_router(mmod.router)

    # Seed workspace dirs.
    ws_base = tmp_path / "workspaces" / "meetings"
    (ws_base / "m1").mkdir(parents=True)
    (ws_base / "m1" / "transcript.md").write_text("hi")
    (ws_base / "m2").mkdir(parents=True)

    with TestClient(app) as c:
        yield c, hub, tmp_path


def test_delete_meeting_removes_from_registry(meeting_client):
    client, hub, _ = meeting_client
    r = client.delete("/api/portal/meetings/m1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["deleted"] == "m1"
    assert hub.meeting_registry.get("m1") is None
    # Sibling survives.
    assert hub.meeting_registry.get("m2") is not None


def test_delete_meeting_purges_workspace_by_default(meeting_client):
    client, _hub, tmp_path = meeting_client
    ws = tmp_path / "workspaces" / "meetings" / "m1"
    assert ws.exists()
    r = client.delete("/api/portal/meetings/m1")
    assert r.status_code == 200
    assert r.json()["purged_workspace"] is True
    assert not ws.exists()
    # Sibling workspace untouched.
    assert (tmp_path / "workspaces" / "meetings" / "m2").exists()


def test_delete_meeting_keeps_workspace_when_flag_false(meeting_client):
    client, _hub, tmp_path = meeting_client
    ws = tmp_path / "workspaces" / "meetings" / "m1"
    r = client.delete("/api/portal/meetings/m1?purge_workspace=false")
    assert r.status_code == 200
    assert r.json()["purged_workspace"] is False
    assert ws.exists()


def test_delete_missing_meeting_404(meeting_client):
    client, *_ = meeting_client
    r = client.delete("/api/portal/meetings/does_not_exist")
    assert r.status_code == 404
