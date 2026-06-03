#!/usr/bin/env python3
"""Kill backend → patch SQLite → wait — then YOU restart.

Marks all stale active plans on an agent as 'completed' so the
task_continuation nudge stops pushing the agent into work the user
has already moved past.

Why this exists
---------------
@user 2026-06-03: 小新 had 7 active plans in execution_plans (RPG fix,
workspace reorg, etc.) that were de-facto done but never got marked
complete via plan_update(complete_step). The continuity nudge
(task_continuation) reads ALL active plans' open steps as "open work
remains" and keeps re-prompting the agent to continue them — even
after the user gives a NEW directive. The UI has no "abandon plan"
button (only per-step continue/skip/fail), and there's no
plan-management HTTP endpoint, so we patch SQLite directly.

The catch: while backend is running, the next _maybe_persist call
overwrites our patch with the backend's in-memory active plans. So
this script:
  1. Sends SIGTERM to the backend (graceful shutdown — flushes
     in-memory state to SQLite one last time on the way out).
  2. Waits until the process is fully gone + port :9090 is free
     (so we know SQLite isn't being written anymore).
  3. Patches SQLite.
  4. Tells you to restart manually (script never starts backend —
     user policy: "you decide when to restart").

Usage
-----
  python scripts/abandon_stale_plans.py <agent_id_prefix>
  example:
  python scripts/abandon_stale_plans.py f8bc9bf4
"""
from __future__ import annotations

import sys
import json
import os
import time
import signal
import socket
import sqlite3
import subprocess


def _backend_pid() -> int | None:
    """Return the python -m app portal PID, or None if no such process.

    Don't use `lsof -sTCP:LISTEN` — when backend is shutting down or
    half-stuck (uvloop quirk @user saw 2026-06-03), listener socket
    closes BEFORE the process exits, so listener check returns None
    but the persist threads are still alive and overwriting SQLite.
    Match the actual process command line instead.
    """
    try:
        r = subprocess.run(
            ["pgrep", "-f", "app portal"],
            capture_output=True, text=True, timeout=5,
        )
        out = r.stdout.strip().splitlines()
        if not out:
            return None
        # pgrep returns one PID per line; take the first
        return int(out[0])
    except Exception:
        return None


def _wait_for_pid_gone(timeout: float = 30.0) -> bool:
    """Poll until the backend Python process is gone."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _backend_pid() is None:
            # Extra wait so any final fsync completes.
            time.sleep(1.0)
            return True
        time.sleep(0.5)
    return False


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: python scripts/abandon_stale_plans.py <agent_id_prefix>")
        print("example: python scripts/abandon_stale_plans.py f8bc9bf4")
        return 2

    prefix = sys.argv[1]
    db_path = os.path.expanduser("~/.tudou_claw/tudou_claw.db")
    if not os.path.exists(db_path):
        print(f"ERROR: SQLite DB not found at {db_path}")
        return 1

    # ── Step 1: kill backend (graceful) ──────────────────────────
    pid = _backend_pid()
    if pid is not None:
        print(f"[1/4] backend PID={pid} (matched 'app portal') — "
              f"sending SIGTERM...")
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        if not _wait_for_pid_gone(timeout=30):
            # SIGTERM didn't take — try SIGKILL.
            print("[1/4] SIGTERM timed out, escalating to SIGKILL...")
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if not _wait_for_pid_gone(timeout=10):
                print(f"ERROR: PID {pid} won't die. Manual intervention:")
                print(f"       kill -9 {pid}")
                return 1
        print("[1/4] backend process gone — SQLite no longer being written.")
    else:
        print("[1/4] backend process not found — proceeding to patch.")

    # ── Step 2: read current agent blob ──────────────────────────
    print(f"[2/4] reading agent {prefix!r}...")
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT agent_id, data FROM agents WHERE agent_id LIKE ?",
            (prefix + "%",)
        ).fetchone()
        if not row:
            print(f"ERROR: no agent matches prefix {prefix!r}")
            return 1
        agent_id, data_blob = row
        d = json.loads(data_blob)
        print(f"      agent_id={agent_id} name={d.get('name','?')!r}")

        # ── Step 3: mark all active plans completed ───────────────
        plans = d.get("execution_plans") or []
        abandoned = []
        for p in plans:
            if p.get("status") != "active":
                continue
            abandoned.append((p.get("id", "?")[:10],
                              (p.get("task_summary") or "")[:60]))
            p["status"] = "completed"
            p["abandoned_at"] = time.time()
            p["abandoned_reason"] = (
                "user moved on to a new task; nudge was pushing this "
                "stale plan, killed via abandon_stale_plans.py"
            )
            for s in (p.get("steps") or []):
                _st = s.get("status")
                if isinstance(_st, dict):
                    _stv = _st.get("value", "")
                else:
                    _stv = str(_st or "")
                if _stv in ("pending", "in_progress"):
                    s["status"] = "skipped"

        # ── Clear _current_plan if still active/interrupted ───────
        cp = d.get("_current_plan")
        cleared_cp = False
        if cp and cp.get("status") in ("active", "interrupted"):
            cp["status"] = "completed"
            cp["abandoned_at"] = time.time()
            cleared_cp = True

        if not abandoned and not cleared_cp:
            print("[3/4] nothing to abandon — already clean.")
            print("[4/4] no SQLite write needed.")
            print()
            print("Done. You can restart the backend normally.")
            return 0

        print(f"[3/4] abandoning {len(abandoned)} active plan(s):")
        for pid_short, summary in abandoned:
            print(f"      - {pid_short}  {summary!r}")
        if cleared_cp:
            print(f"      - _current_plan ({cp.get('id','?')[:10]}) cleared")

        # ── Step 4: write back ────────────────────────────────────
        con.execute("UPDATE agents SET data = ? WHERE agent_id = ?",
                    (json.dumps(d, ensure_ascii=False), agent_id))
        con.commit()
        print(f"[4/4] SQLite written ({len(json.dumps(d))//1024} KB).")

        print()
        print("=" * 60)
        print("DONE — backend is STOPPED. Restart it manually:")
        print()
        print("  cd /Users/pangwanchun/AIProjects/TudouClaw_new")
        print("  nohup python -m app portal --port 9090 --secret admin123 \\")
        print("      > /tmp/tudou_portal.log 2>&1 &")
        print()
        print("After restart, from_persist_dict will load the patched")
        print("plans (status=completed) → the task_continuation nudge")
        print("will see open_work=empty → no more 'continue old work'.")
        print("=" * 60)
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
