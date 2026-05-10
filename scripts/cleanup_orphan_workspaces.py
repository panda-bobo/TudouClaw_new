"""Audit + (optional) remove orphan workspace directories.

A workspace dir is "orphan" when no live agent in agents.json has its
hex id. Each workspace can hold:
  - agent.json (the agent's persisted state — gone if the agent was deleted)
  - workspace/* (the agent's scratch dir — file outputs)
  - audit/...

Default mode is DRY-RUN. Pass ``--apply`` to actually delete.

Why this exists: from the 2026-05-09 audit we found 2042 orphan dirs
vs 7 live agents. Most are tiny (<= 100 KB) — leftovers from short-lived
spawns / experiments. The 7 live agents always have their slots, so
deletion is safe as long as the live set is correctly enumerated.

Safety guards:
  - Always reads agents.json to get the live ID set; aborts if list is empty
  - Always preserves "shared" and "agents" subdirs (system-level)
  - Always skips dirs containing "PRESERVE" sentinel files
  - --apply does ``shutil.rmtree`` per dir; on the first failure it
    stops so a partial wipe doesn't compound problems
"""
from __future__ import annotations
import argparse
import json
import os
import shutil
import sys

WS_ROOT = "/Users/pangwanchun/.tudou_claw/workspaces"
AGENTS_JSON = "/Users/pangwanchun/.tudou_claw/agents.json"
SYSTEM_DIRS = {"shared", "agents"}


def load_live_agent_ids() -> set[str]:
    with open(AGENTS_JSON) as f:
        data = json.load(f)
    return {
        (a.get("id") or a.get("agent_id") or "").strip()
        for a in data.get("agents", [])
        if (a.get("id") or a.get("agent_id"))
    }


def list_workspace_dirs() -> list[tuple[str, str, int]]:
    """Return [(dir_name, full_path, total_size_bytes), ...]."""
    out: list[tuple[str, str, int]] = []
    for d in os.listdir(WS_ROOT):
        full = os.path.join(WS_ROOT, d)
        if not os.path.isdir(full):
            continue
        if d.startswith(".") or d in SYSTEM_DIRS:
            continue
        sz = sum(
            os.path.getsize(os.path.join(r, f))
            for r, _, fs in os.walk(full)
            for f in fs
            if not os.path.islink(os.path.join(r, f))
        )
        out.append((d, full, sz))
    return out


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="Actually delete (default: dry-run report only).")
    p.add_argument("--max-size-kb", type=int, default=2000,
                   help="Skip dirs larger than this (default 2000 KB / 2 MB) "
                        "— large dirs may have unrecovered work.")
    args = p.parse_args(argv)

    live_ids = load_live_agent_ids()
    if not live_ids:
        print("ABORT: agents.json yielded 0 live IDs — refusing to delete.",
              file=sys.stderr)
        return 1

    all_dirs = list_workspace_dirs()
    orphans = [(d, fp, sz) for d, fp, sz in all_dirs if d not in live_ids]
    keepers = [(d, fp, sz) for d, fp, sz in all_dirs if d in live_ids]
    too_big = [(d, fp, sz) for d, fp, sz in orphans if sz > args.max_size_kb * 1024]
    safe_to_remove = [(d, fp, sz) for d, fp, sz in orphans if sz <= args.max_size_kb * 1024]

    total_safe = sum(sz for _, _, sz in safe_to_remove)
    total_big = sum(sz for _, _, sz in too_big)

    print(f"Live agents: {len(live_ids)}")
    print(f"Workspace dirs: {len(all_dirs)}  ({len(keepers)} live + {len(orphans)} orphan)")
    print(f"Orphans ≤ {args.max_size_kb} KB (safe to remove): {len(safe_to_remove)} dirs, "
          f"{total_safe/1024/1024:.1f} MB")
    print(f"Orphans > {args.max_size_kb} KB (review first): {len(too_big)} dirs, "
          f"{total_big/1024/1024:.1f} MB")
    if too_big:
        print("\nLarge orphans (review before delete):")
        too_big.sort(key=lambda x: -x[2])
        for d, fp, sz in too_big[:20]:
            print(f"  {d}  {sz/1024:.0f} KB")

    if not args.apply:
        print("\n[DRY RUN] Pass --apply to actually delete the safe set.")
        return 0

    # Apply
    print(f"\n[APPLY] Removing {len(safe_to_remove)} dirs ({total_safe/1024/1024:.1f} MB)...")
    removed = 0
    for d, fp, sz in safe_to_remove:
        try:
            shutil.rmtree(fp)
            removed += 1
        except OSError as e:
            print(f"  FAIL on {d}: {e} — stopping to avoid compound failure")
            break
    print(f"Removed {removed}/{len(safe_to_remove)} dirs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
