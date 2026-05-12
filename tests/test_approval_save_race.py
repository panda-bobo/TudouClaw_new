"""Tests for the approvals_log.json save race fix (2026-05-12).

Real-world symptom: tudou.log had recurring warnings:

    failed to save approvals_log.json: [Errno 2] No such file or directory:
      .../approvals_log.json.tmp -> .../approvals_log.json

Root cause: two threads call _save_pending_history concurrently. Both
build `tmp = p + ".tmp"` (same name). Thread A writes + renames; the
rename moves the tmp away. Thread B's open(tmp, "w") may overlap with
A's rename, then B's os.replace finds the tmp gone → ENOENT.

Fix: per-call unique tmp name (pid + thread_id + uuid frag) so
concurrent writers never collide.
"""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from app.auth import ToolPolicy


def _make_policy(tmp_path) -> ToolPolicy:
    """Build a ToolPolicy with a real on-disk pending-history file."""
    pending_file = str(tmp_path / "approvals_log.json")
    p = ToolPolicy()
    # Wire up the file path (the field exists on the dataclass)
    p._pending_history_file = pending_file
    return p


# ── Sequential save still works ──────────────────────────────────────

def test_single_save_writes_file(tmp_path):
    p = _make_policy(tmp_path)
    p._save_pending_history()
    assert os.path.exists(tmp_path / "approvals_log.json")
    body = json.loads((tmp_path / "approvals_log.json").read_text())
    assert "pending" in body
    assert "history" in body


def test_save_overwrites_atomically(tmp_path):
    p = _make_policy(tmp_path)
    p._save_pending_history()
    # Simulate state mutation
    p.history = []
    p._save_pending_history()
    body = json.loads((tmp_path / "approvals_log.json").read_text())
    assert body["history"] == []


# ── Concurrent saves don't ENOENT each other ─────────────────────────

def test_concurrent_saves_no_enoent(tmp_path, caplog):
    """20 threads hammering _save_pending_history simultaneously must
    not produce any ENOENT warnings — the prior bug logged one per
    losing thread."""
    p = _make_policy(tmp_path)
    errors: list[str] = []
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()  # release all 20 at once
        try:
            p._save_pending_history()
        except Exception as e:
            errors.append(str(e))

    with caplog.at_level("WARNING", logger="tudou.auth"):
        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    # No exceptions bubbled out
    assert errors == []
    # No ENOENT warnings logged
    bad_records = [r for r in caplog.records
                   if "No such file" in r.getMessage()]
    assert bad_records == [], (
        f"got {len(bad_records)} ENOENT warnings — race not fixed")
    # Final file state is valid JSON (last writer wins, body is intact)
    body = json.loads((tmp_path / "approvals_log.json").read_text())
    assert "pending" in body and "history" in body


def test_no_leftover_tmp_files_after_concurrent_saves(tmp_path):
    """Each thread cleans up its own tmp on exception — and on success,
    os.replace consumes the tmp. After all done, no .tmp should linger."""
    p = _make_policy(tmp_path)
    barrier = threading.Barrier(10)

    def worker():
        barrier.wait()
        p._save_pending_history()

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Look for ANY *.tmp file in the dir
    leftover = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftover == [], (
        f"{len(leftover)} tmp files leaked: {leftover}")


def test_tmp_filename_includes_pid_and_thread_id(tmp_path, monkeypatch):
    """The unique tmp name must vary between threads — verify by
    checking that two calls in DIFFERENT threads produce different
    tmp names. We capture os.replace's source argument."""
    p = _make_policy(tmp_path)
    captured: list[str] = []

    real_replace = os.replace

    def spy_replace(src, dst):
        captured.append(src)
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy_replace)

    def worker():
        p._save_pending_history()

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t1.join()
    t2.start(); t2.join()

    assert len(captured) == 2
    # Different threads → different tmp filenames (per-call uniqueness)
    assert captured[0] != captured[1], (
        f"both saves used the same tmp name: {captured[0]}")


# ── Failure path: tmp cleanup on exception ──────────────────────────

def test_tmp_cleaned_up_on_save_failure(tmp_path, monkeypatch):
    """If json.dump or os.replace raises mid-save, the partial tmp
    must be unlinked — otherwise we leak files in the data dir."""
    p = _make_policy(tmp_path)

    # Inject a failure: make os.replace fail
    def boom(src, dst):
        raise OSError("simulated disk error")
    monkeypatch.setattr(os, "replace", boom)

    p._save_pending_history()  # logs warning, doesn't raise

    # No tmp files should linger
    leftover = [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    assert leftover == [], f"tmp leaked on failure: {leftover}"


# ── No file path → no-op ─────────────────────────────────────────────

def test_no_path_silent_skip(tmp_path):
    p = ToolPolicy()
    p._pending_history_file = ""
    # Must not raise, must not create anything
    p._save_pending_history()
    # tmp_path should still be empty
    assert os.listdir(tmp_path) == []
