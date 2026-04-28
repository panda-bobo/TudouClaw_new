"""One-time backfill: populate Agent.role_success_count / role_fail_count
from V2 task store history.

Run when:
  * The leaderboard shows all agents at "—" (smoothed prior, no real data).
  * Counters were added in a recent commit and existing V2 history hasn't
    been propagated.

Idempotent strategy:
  * Reads CURRENT V2 store totals (succeeded / failed) per agent.
  * OVERWRITES Agent.role_success_count / role_fail_count with those totals.
  * Updates Agent.role_last_success_at to the most recent SUCCEEDED task's
    completed_at.

Why overwrite (not add): the V2 store is the source of truth. Adding could
double-count if backfill is run twice. Overwriting makes the script safe
to re-run any time — it always converges to the V2 store reality.

Usage:
    python -m scripts.backfill_v2_counters
    python -m scripts.backfill_v2_counters --dry-run    # preview only
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would change but don't write.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )
    log = logging.getLogger("backfill")

    # Lazy imports — avoid heavy hub init if not needed
    from app.infra.database import get_database
    from app.v2.core.task_store import get_store
    from app.v2.core.task import TaskStatus

    db = get_database()
    v2 = get_store()

    # ── Aggregate V2 task counts per agent ─────────────────────────
    succ_count: dict[str, int] = defaultdict(int)
    fail_count: dict[str, int] = defaultdict(int)
    last_succ_at: dict[str, float] = defaultdict(float)

    # We can't use list_tasks(status=...) cleanly without a high LIMIT,
    # so paginate. 1000 per page handles most installations.
    PAGE = 1000
    total_scanned = 0
    for status_value in (TaskStatus.SUCCEEDED.value, TaskStatus.FAILED.value):
        offset = 0
        while True:
            tasks = v2.list_tasks(status=status_value, limit=PAGE, offset=offset)
            if not tasks:
                break
            for t in tasks:
                aid = t.agent_id or ""
                if not aid:
                    continue
                if status_value == TaskStatus.SUCCEEDED.value:
                    succ_count[aid] += 1
                    completed_ts = float(t.completed_at or t.updated_at or 0)
                    if completed_ts > last_succ_at[aid]:
                        last_succ_at[aid] = completed_ts
                else:
                    fail_count[aid] += 1
            total_scanned += len(tasks)
            if len(tasks) < PAGE:
                break
            offset += PAGE

    log.info("scanned %d V2 tasks (succeeded + failed)", total_scanned)

    affected_agents = set(succ_count.keys()) | set(fail_count.keys())
    if not affected_agents:
        log.info("no V2 task history found — nothing to backfill")
        return 0

    log.info("found history for %d agents", len(affected_agents))

    # ── Read each affected agent + apply ────────────────────────────
    changed = 0
    skipped_missing = 0

    for aid in sorted(affected_agents):
        row = db._conn.execute(
            "SELECT data FROM agents WHERE agent_id = ?", (aid,),
        ).fetchone()
        if row is None:
            skipped_missing += 1
            log.debug("agent %s in V2 store but not in V1 — skip", aid[:8])
            continue
        try:
            d = json.loads(row["data"])
        except Exception as e:
            log.warning("agent %s data unparseable: %s", aid[:8], e)
            continue
        old_s = int(d.get("role_success_count", 0) or 0)
        old_f = int(d.get("role_fail_count", 0) or 0)
        old_last = float(d.get("role_last_success_at", 0) or 0)
        new_s = succ_count.get(aid, 0)
        new_f = fail_count.get(aid, 0)
        new_last = last_succ_at.get(aid, 0)

        if old_s == new_s and old_f == new_f and old_last == new_last:
            continue  # no change needed

        log.info(
            "  %s (%s): s=%d→%d f=%d→%d last_at=%s→%s",
            aid[:8], d.get("name", "?"),
            old_s, new_s, old_f, new_f,
            int(old_last) or "-", int(new_last) or "-",
        )

        if args.dry_run:
            changed += 1
            continue

        d["role_success_count"] = new_s
        d["role_fail_count"] = new_f
        if new_last:
            d["role_last_success_at"] = new_last
        try:
            db._conn.execute(
                "UPDATE agents SET data = ? WHERE agent_id = ?",
                (json.dumps(d, ensure_ascii=False), aid),
            )
            db._conn.commit()
            changed += 1
        except Exception as e:
            log.warning("failed to update agent %s: %s", aid[:8], e)

    if skipped_missing:
        log.warning("%d agents in V2 store but absent from V1 (skipped)", skipped_missing)

    action = "would change" if args.dry_run else "updated"
    log.info("%s %d agent record(s)", action, changed)

    if args.dry_run:
        log.info("dry-run done — re-run without --dry-run to apply")
    else:
        log.info("backfill complete. Restart hub for in-memory agents to pick up changes,")
        log.info("or call hub.reload_agents() / restart uvicorn.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
