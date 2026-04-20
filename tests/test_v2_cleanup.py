"""Tests for the agent-data cascade-cleanup layer (``app.cleanup``).

``purge_agent`` must walk every subsystem and delete rows that reference
the agent — V1 SQLite tables, MCP bindings, skill grants, V2 tasks +
events, workspace directories. Each subsystem is best-effort; a broken
one must not block the rest.

Tests use monkeypatch to inject fake subsystems so we verify each branch
in isolation rather than spinning up the whole hub.
"""
from __future__ import annotations

import os
import sqlite3
import types

import pytest


# ── shared fake subsystems ────────────────────────────────────────────


class _FakeSkillInstall:
    def __init__(self, skill_id):
        self.manifest = types.SimpleNamespace(id=skill_id, name=skill_id)


class _FakeSkillRegistry:
    def __init__(self, grants: dict[str, list[str]]):
        # skill_id → [agent_id, ...]
        self._grants = {k: list(v) for k, v in grants.items()}
        self.revoke_calls: list[tuple[str, str]] = []

    def list_for_agent(self, agent_id):
        return [_FakeSkillInstall(sk)
                for sk, ags in self._grants.items() if agent_id in ags]

    def revoke(self, skill_id, agent_id):
        self.revoke_calls.append((skill_id, agent_id))
        lst = self._grants.get(skill_id, [])
        if agent_id in lst:
            lst.remove(agent_id)
            return True
        return False


class _FakeNodeConfig:
    def __init__(self):
        self.agent_bindings: dict[str, list[str]] = {}
        self.agent_env_overrides: dict[str, dict[str, dict[str, str]]] = {}
        self.updated_at = 0.0


class _FakeMCPManager:
    def __init__(self):
        self.node_configs = {"local": _FakeNodeConfig()}
        self.saves = 0

    def save_to_disk(self):
        self.saves += 1


# ── helpers ───────────────────────────────────────────────────────────


def _make_v1_db(tmp_path, agent_id="a1"):
    """Build a minimal V1 SQLite with the agent-referencing tables seeded."""
    dbp = tmp_path / "tudou.db"
    conn = sqlite3.connect(str(dbp))
    conn.executescript("""
        CREATE TABLE agents (agent_id TEXT PRIMARY KEY);
        CREATE TABLE agent_routes (agent_id TEXT PRIMARY KEY, node_id TEXT);
        CREATE TABLE memory_episodic (id TEXT PRIMARY KEY, agent_id TEXT);
        CREATE TABLE memory_semantic (id TEXT PRIMARY KEY, agent_id TEXT);
        CREATE TABLE memory_config (agent_id TEXT PRIMARY KEY);
        CREATE TABLE file_manifests (id INTEGER PRIMARY KEY, agent_id TEXT, file_path TEXT);
        CREATE TABLE approvals (request_id TEXT PRIMARY KEY, agent_id TEXT);
        CREATE TABLE agent_messages (id TEXT PRIMARY KEY, from_agent TEXT, to_agent TEXT);
        CREATE TABLE delegations (request_id TEXT PRIMARY KEY, from_agent TEXT, to_agent TEXT);
    """)
    # Seed data for target agent AND a sibling that must survive.
    conn.executemany("INSERT INTO memory_episodic VALUES (?, ?)",
                     [("e1", agent_id), ("e2", agent_id), ("e3", "other")])
    conn.executemany("INSERT INTO memory_semantic VALUES (?, ?)",
                     [("s1", agent_id), ("s2", "other")])
    conn.execute("INSERT INTO memory_config VALUES (?)", (agent_id,))
    conn.execute("INSERT INTO agent_routes VALUES (?, ?)", (agent_id, "local"))
    conn.execute("INSERT INTO agent_routes VALUES (?, ?)", ("other", "local"))
    conn.executemany("INSERT INTO file_manifests (agent_id, file_path) VALUES (?, ?)",
                     [(agent_id, "/a"), (agent_id, "/b"), ("other", "/c")])
    conn.execute("INSERT INTO approvals VALUES (?, ?)", ("r1", agent_id))
    conn.executemany("INSERT INTO agent_messages VALUES (?, ?, ?)",
                     [("m1", agent_id, "bob"), ("m2", "bob", agent_id),
                      ("m3", "alice", "bob")])
    conn.executemany("INSERT INTO delegations VALUES (?, ?, ?)",
                     [("d1", agent_id, "bob"), ("d2", "alice", "bob")])
    conn.commit()
    return conn


# ── purge_agent: SQLite cascade ───────────────────────────────────────


def test_purge_db_deletes_only_target_agent_rows(tmp_path, monkeypatch):
    conn = _make_v1_db(tmp_path)
    # Stub get_database to return an object with ._conn = our conn.
    from app.infra import database as _db_mod
    stub = types.SimpleNamespace(_conn=conn)
    monkeypatch.setattr(_db_mod, "get_database", lambda: stub)
    # Skip mcp/skills/v2/workspace subsystems for this focused test.
    import app.cleanup as _c
    monkeypatch.setattr(_c, "_purge_mcp",       lambda _a: 0)
    monkeypatch.setattr(_c, "_purge_skills",    lambda _a: 0)
    monkeypatch.setattr(_c, "_purge_v2",        lambda _a: 0)
    monkeypatch.setattr(_c, "_purge_workspace", lambda _a: 0)

    from app.cleanup import purge_agent
    report = purge_agent("a1")
    assert report["db_tables"] >= 8  # 2 episodic + 1 semantic + 1 config + 1 route + 2 manifests + 1 approval + 1 msg-from + 1 msg-to + 1 deleg

    # Sibling "other" rows survive.
    assert conn.execute(
        "SELECT COUNT(*) FROM memory_episodic WHERE agent_id = ?", ("other",)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM memory_semantic WHERE agent_id = ?", ("other",)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM file_manifests WHERE agent_id = ?", ("other",)
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM agent_routes WHERE agent_id = ?", ("other",)
    ).fetchone()[0] == 1
    # agent_messages between non-target agents survives.
    assert conn.execute(
        "SELECT COUNT(*) FROM agent_messages"
    ).fetchone()[0] == 1

    # Target rows gone.
    assert conn.execute(
        "SELECT COUNT(*) FROM memory_episodic WHERE agent_id = ?", ("a1",)
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM agent_messages "
        "WHERE from_agent = 'a1' OR to_agent = 'a1'"
    ).fetchone()[0] == 0


def test_purge_db_survives_missing_table(tmp_path, monkeypatch):
    """If a table doesn't exist (older schema), ``_purge_db`` logs and
    moves on — it must not abort the whole cascade."""
    dbp = tmp_path / "x.db"
    conn = sqlite3.connect(str(dbp))
    # Only create ONE of the expected tables.
    conn.execute("CREATE TABLE memory_episodic (id TEXT, agent_id TEXT)")
    conn.execute("INSERT INTO memory_episodic VALUES ('e', 'a1')")
    conn.commit()

    from app.infra import database as _db_mod
    stub = types.SimpleNamespace(_conn=conn)
    monkeypatch.setattr(_db_mod, "get_database", lambda: stub)

    import app.cleanup as _c
    monkeypatch.setattr(_c, "_purge_mcp",       lambda _a: 0)
    monkeypatch.setattr(_c, "_purge_skills",    lambda _a: 0)
    monkeypatch.setattr(_c, "_purge_v2",        lambda _a: 0)
    monkeypatch.setattr(_c, "_purge_workspace", lambda _a: 0)

    report = _c.purge_agent("a1")
    # Should still report non-negative count (the one real table cleaned).
    assert report["db_tables"] == 1
    # The one real row is gone.
    assert conn.execute(
        "SELECT COUNT(*) FROM memory_episodic WHERE agent_id = 'a1'"
    ).fetchone()[0] == 0


# ── purge_agent: MCP subsystem ────────────────────────────────────────


def test_purge_mcp_drops_bindings_and_overrides(monkeypatch):
    mgr = _FakeMCPManager()
    node = mgr.node_configs["local"]
    node.agent_bindings["a1"]        = ["mcp_x", "mcp_y"]
    node.agent_bindings["other"]     = ["mcp_z"]
    node.agent_env_overrides["a1"]   = {"mcp_x": {"KEY": "v"}}
    node.agent_env_overrides["other"] = {"mcp_z": {}}

    import app.mcp.manager as _mgr
    monkeypatch.setattr(_mgr, "get_mcp_manager", lambda: mgr)

    import app.cleanup as _c
    monkeypatch.setattr(_c, "_purge_db",        lambda _a: 0)
    monkeypatch.setattr(_c, "_purge_skills",    lambda _a: 0)
    monkeypatch.setattr(_c, "_purge_v2",        lambda _a: 0)
    monkeypatch.setattr(_c, "_purge_workspace", lambda _a: 0)

    report = _c.purge_agent("a1")
    assert report["mcp_bindings"] > 0
    # Target purged.
    assert "a1" not in node.agent_bindings
    assert "a1" not in node.agent_env_overrides
    # Sibling untouched.
    assert node.agent_bindings["other"] == ["mcp_z"]
    # Save was called.
    assert mgr.saves == 1


# ── purge_agent: skills ───────────────────────────────────────────────


def test_purge_skills_revokes_all_grants(monkeypatch):
    reg = _FakeSkillRegistry({
        "skill_a": ["a1", "other"],
        "skill_b": ["a1"],
        "skill_c": ["other"],
    })
    import app.skills.engine as _sk
    monkeypatch.setattr(_sk, "get_registry", lambda: reg)

    import app.cleanup as _c
    monkeypatch.setattr(_c, "_purge_db",        lambda _a: 0)
    monkeypatch.setattr(_c, "_purge_mcp",       lambda _a: 0)
    monkeypatch.setattr(_c, "_purge_v2",        lambda _a: 0)
    monkeypatch.setattr(_c, "_purge_workspace", lambda _a: 0)

    report = _c.purge_agent("a1")
    assert report["skill_grants"] == 2
    # Both grants revoked.
    assert ("skill_a", "a1") in reg.revoke_calls
    assert ("skill_b", "a1") in reg.revoke_calls
    # Siblings untouched.
    assert reg._grants["skill_a"] == ["other"]
    assert reg._grants["skill_c"] == ["other"]


# ── purge_agent: V2 tasks + events ────────────────────────────────────


def test_purge_v2_deletes_tasks_and_events(monkeypatch, tmp_path):
    """Hit the real V2 TaskStore with a temp DB."""
    monkeypatch.setenv("TUDOU_CLAW_DB_PATH", str(tmp_path / "v2.db"))
    import app.v2.core.task_store as ts_mod
    monkeypatch.setattr(ts_mod, "_STORE", None)
    store = ts_mod.get_store()

    # Create the parent agents so tasks_v2 FK to agents_v2 resolves.
    from app.v2.agent.agent_v2 import AgentV2, Capabilities
    for aid in ("a1", "other"):
        a = AgentV2(id=aid, name=aid, role="x",
                    capabilities=Capabilities(), created_at=0.0)
        store.save_agent(a)

    from app.v2.core.task import Task, TaskPhase, TaskStatus
    import time as _t
    t1 = Task(id="t1", agent_id="a1", template_id="x", intent="a",
              created_at=_t.time(), updated_at=_t.time())
    t2 = Task(id="t2", agent_id="a1", template_id="x", intent="b",
              created_at=_t.time(), updated_at=_t.time())
    t3 = Task(id="t3", agent_id="other", template_id="x", intent="c",
              created_at=_t.time(), updated_at=_t.time())
    for t in [t1, t2, t3]:
        store.save(t)

    # Seed events for target and sibling tasks.
    from app.v2.core.task_events import TaskEvent
    for tid in ["t1", "t2", "t3"]:
        for i in range(3):
            store.append_event(TaskEvent(
                task_id=tid, ts=_t.time() + i,
                phase="intake", type="phase_enter", payload={"i": i},
            ))

    # Skip other subsystems.
    import app.cleanup as _c
    monkeypatch.setattr(_c, "_purge_db",        lambda _a: 0)
    monkeypatch.setattr(_c, "_purge_mcp",       lambda _a: 0)
    monkeypatch.setattr(_c, "_purge_skills",    lambda _a: 0)
    monkeypatch.setattr(_c, "_purge_workspace", lambda _a: 0)

    report = _c.purge_agent("a1")
    # 2 tasks + 6 events = 8 rows.
    assert report["v2_tasks"] == 8

    # Sibling's task + events survive.
    assert store.get_task("t3") is not None
    assert len(store.load_events("t3")) == 3
    # Target tasks gone.
    assert store.get_task("t1") is None
    assert store.get_task("t2") is None
    assert store.load_events("t1") == []
    assert store.load_events("t2") == []


# ── purge_agent: empty id is a no-op ──────────────────────────────────


def test_purge_agent_empty_id_noop():
    from app.cleanup import purge_agent
    assert purge_agent("") == {}
    assert purge_agent(None) == {}  # type: ignore[arg-type]


# ── V2 hard delete endpoint cascades ──────────────────────────────────


def test_v2_hard_delete_cascades(monkeypatch, tmp_path):
    """DELETE /agents/{id}?hard=true must remove the agent row AND run
    the purge helper. We verify both by checking that a seeded V2 task
    for the agent is gone afterwards."""
    monkeypatch.setenv("TUDOU_CLAW_DB_PATH", str(tmp_path / "v2.db"))
    import app.v2.core.task_store as ts_mod
    monkeypatch.setattr(ts_mod, "_STORE", None)

    import app.api.routers.v2 as v2mod
    monkeypatch.setattr(v2mod, "_bus_singleton", None)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.deps.auth import get_current_user, CurrentUser

    async def _admin():
        return CurrentUser(user_id="u1", role="superAdmin")

    app = FastAPI()
    app.dependency_overrides[get_current_user] = _admin
    app.dependency_overrides[v2mod._sse_auth_dep] = _admin
    app.include_router(v2mod.router)

    with TestClient(app) as c:
        # Create agent + seed a task.
        r = c.post("/api/v2/agents", json={
            "name": "Doomed", "role": "x",
            "capabilities": {}, "task_template_ids": [],
        })
        aid = r.json()["agent"]["id"]

        store = ts_mod.get_store()
        from app.v2.core.task import Task
        import time as _t
        store.save(Task(id="tt", agent_id=aid, template_id="x", intent="y",
                        created_at=_t.time(), updated_at=_t.time()))
        assert store.get_task("tt") is not None

        # Hard delete.
        rd = c.delete(f"/api/v2/agents/{aid}?hard=true")
        assert rd.status_code == 200, rd.text
        body = rd.json()
        assert body["hard"] is True
        assert "purge" in body

        # Agent row + task both gone.
        rr = c.get(f"/api/v2/agents/{aid}")
        assert rr.status_code == 404
        assert store.get_task("tt") is None


def test_sweep_orphans_endpoint(monkeypatch, tmp_path):
    """POST /admin/sweep-orphans accepts a list of agent ids and runs
    purge_agent for each."""
    monkeypatch.setenv("TUDOU_CLAW_DB_PATH", str(tmp_path / "v2.db"))
    import app.v2.core.task_store as ts_mod
    monkeypatch.setattr(ts_mod, "_STORE", None)

    import app.api.routers.v2 as v2mod
    monkeypatch.setattr(v2mod, "_bus_singleton", None)

    calls: list[str] = []
    import app.cleanup as _c
    monkeypatch.setattr(_c, "purge_agent",
                        lambda aid: (calls.append(aid), {"db_tables": 0})[1])

    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api.deps.auth import get_current_user, CurrentUser

    async def _admin():
        return CurrentUser(user_id="u1", role="superAdmin")

    app = FastAPI()
    app.dependency_overrides[get_current_user] = _admin
    app.dependency_overrides[v2mod._sse_auth_dep] = _admin
    app.include_router(v2mod.router)

    with TestClient(app) as c:
        r = c.post("/api/v2/admin/sweep-orphans",
                   json={"agent_ids": ["g1", "g2", "g3"]})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["swept"] == 3
        assert sorted(calls) == ["g1", "g2", "g3"]
