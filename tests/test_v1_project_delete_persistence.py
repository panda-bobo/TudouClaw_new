"""Regression test for the "deleted project comes back after restart" bug.

Root cause was: ``_save_projects`` upserted every in-memory row but never
issued DELETE for rows no longer in memory. ``remove_project`` removed
the dict entry and re-saved, but the DB row survived → next
``_load_projects`` (SQLite-first) resurrected it.

Fix was two-layer:
  1. ``remove_project`` explicitly ``db.delete_project`` before re-saving.
  2. ``_save_projects`` is now a sync: upsert + delete-missing.

This test simulates: create two projects → save → delete one → save →
re-load from the same DB → only the survivor remains.
"""
from __future__ import annotations

import os
import tempfile
import types

import pytest


class _FakeProject:
    def __init__(self, pid, name):
        self.id = pid
        self.name = name
    def to_dict(self): return {"id": self.id, "name": self.name}
    def to_persist_dict(self): return {"project_id": self.id, "name": self.name}
    @classmethod
    def from_persist_dict(cls, d):
        return cls(d["project_id"], d.get("name", ""))


class _InMemDB:
    """Minimal Database shim implementing the methods persistence.py calls."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    def count(self, table): return len(self.rows)

    def save_project(self, d):
        self.rows[d["project_id"]] = dict(d)

    def load_projects(self):
        return list(self.rows.values())

    def delete_project(self, pid):
        return self.rows.pop(pid, None) is not None

    def delete(self, table, col, val):
        return self.rows.pop(val, None) is not None

    # Other methods persistence.py might touch (unused for this test).
    def count(self, table): return len(self.rows)


def test_save_projects_deletes_stale_rows(monkeypatch):
    """Core invariant: after ``_save_projects``, DB must match memory."""
    from app.hub import persistence

    # Patch the Project import inside _save_projects.
    monkeypatch.setattr(persistence, "Project", _FakeProject, raising=False)

    # Build a minimal persistence-mixin-like object.
    with tempfile.TemporaryDirectory() as tmp:
        obj = types.SimpleNamespace()
        obj._db = _InMemDB()
        obj._data_dir = tmp
        obj._hub = types.SimpleNamespace(
            projects={
                "p1": _FakeProject("p1", "Alpha"),
                "p2": _FakeProject("p2", "Beta"),
            },
            _projects_file=os.path.join(tmp, "projects.json"),
            _data_dir=tmp,
        )
        # Bind the method on a dummy (so `self` resolves correctly).
        obj.agents = {}
        # Skip markdown export helpers referenced by save_projects:
        obj._save_agent_workspace = lambda a: None

        # Put a stray row in the DB as if a previous process left it behind.
        obj._db.rows["ghost"] = {"project_id": "ghost", "name": "Should die"}
        assert len(obj._db.rows) == 1

        # First save: memory has p1+p2; DB has ghost only.
        persistence.PersistenceManager._save_projects(obj)

        # After save, DB must contain exactly {p1, p2} — ghost is GONE.
        assert set(obj._db.rows.keys()) == {"p1", "p2"}, (
            f"expected {{p1,p2}}, got {set(obj._db.rows.keys())}"
        )

        # Now remove p1 from memory and re-save. DB must drop p1 too.
        del obj._hub.projects["p1"]
        persistence.PersistenceManager._save_projects(obj)
        assert set(obj._db.rows.keys()) == {"p2"}


def test_save_agents_deletes_stale_rows(monkeypatch):
    """Same invariant for agents — catches the bug class, not just one
    symptom."""
    from app.hub import persistence

    class _FakeAgent:
        def __init__(self, aid):
            self.id = aid
            self.name = aid
            self.role = "x"
        def to_persist_dict(self): return {"agent_id": self.id, "name": self.name}
        def to_dict(self): return self.to_persist_dict()

    class _AgentDB(_InMemDB):
        def save_agent(self, d): self.rows[d["agent_id"]] = dict(d)
        def load_agents(self): return list(self.rows.values())
        def delete_agent(self, aid): return self.rows.pop(aid, None) is not None

    with tempfile.TemporaryDirectory() as tmp:
        obj = types.SimpleNamespace()
        obj._db = _AgentDB()
        obj._data_dir = tmp
        obj._hub = types.SimpleNamespace(
            _agents_file=os.path.join(tmp, "agents.json"),
        )
        obj.agents = {"a1": _FakeAgent("a1"), "a2": _FakeAgent("a2")}
        obj._save_agent_workspace = lambda a: None

        # Stray row.
        obj._db.rows["ghost"] = {"agent_id": "ghost", "name": "x"}

        persistence.PersistenceManager._save_agents(obj)
        assert set(obj._db.rows.keys()) == {"a1", "a2"}

        del obj.agents["a1"]
        persistence.PersistenceManager._save_agents(obj)
        assert set(obj._db.rows.keys()) == {"a2"}
