"""Audit chroma `tudou_domain_dkb_*` collections vs live DKB references.

DKBs (Domain Knowledge Bases) are scoped chroma collections named
``tudou_domain_dkb_<10-hex-id>``. A collection is "orphan" when its
id appears nowhere we know to look:
  - agents.json (granted_skills, etc.)
  - projects.data JSON
  - conversation_tasks.db (any text column)
  - memory_* tables in tudou_claw.db
  - workspaces/<live-agent>/agent.json files

Default mode is DRY-RUN — reports orphan ids only. Use ``--apply`` to
actually delete the chroma collections.

Be careful: "orphan" here means "no live reference found" — the
embedding may still be useful as historical context. Recommend
inspecting ``--list`` output and approving specific ids before
running ``--apply``.
"""
from __future__ import annotations
import argparse
import json
import os
import re
import sqlite3
import sys
from typing import Set

CHROMA_DB = "/Users/pangwanchun/.tudou_claw/chromadb/chroma.sqlite3"
TUDOU_DB = "/Users/pangwanchun/.tudou_claw/tudou_claw.db"
CONV_DB = "/Users/pangwanchun/.tudou_claw/conversation_tasks.db"
AGENTS_JSON = "/Users/pangwanchun/.tudou_claw/agents.json"
WS_ROOT = "/Users/pangwanchun/.tudou_claw/workspaces"
DKB_PREFIX = "tudou_domain_dkb_"
HEX10 = re.compile(r"[a-f0-9]{10}")


def list_dkb_ids() -> Set[str]:
    conn = sqlite3.connect(CHROMA_DB)
    rows = conn.execute("SELECT name FROM collections").fetchall()
    conn.close()
    return {n[0][len(DKB_PREFIX):] for n in rows
            if n[0].startswith(DKB_PREFIX)}


def collect_referenced_ids(all_ids: Set[str]) -> Set[str]:
    referenced: Set[str] = set()

    def scan_text(text: str):
        for m in HEX10.findall(text):
            if m in all_ids:
                referenced.add(m)

    # 1. agents.json
    if os.path.exists(AGENTS_JSON):
        with open(AGENTS_JSON) as f:
            scan_text(f.read())

    # 2. projects.data + memory_* tables
    if os.path.exists(TUDOU_DB):
        conn = sqlite3.connect(TUDOU_DB)
        try:
            for (data_str,) in conn.execute(
                "SELECT data FROM projects WHERE data IS NOT NULL"
            ):
                scan_text(data_str)
            for tbl in ("memory_topic", "memory_episodic", "memory_semantic"):
                try:
                    cols = [r[1] for r in conn.execute(
                        f"PRAGMA table_info({tbl})").fetchall()]
                    for col in cols:
                        try:
                            for (v,) in conn.execute(
                                f"SELECT {col} FROM {tbl}"
                            ):
                                if v and isinstance(v, str):
                                    scan_text(v)
                        except sqlite3.OperationalError:
                            pass
                except sqlite3.OperationalError:
                    pass
        finally:
            conn.close()

    # 3. conversation_tasks.db
    if os.path.exists(CONV_DB):
        conn = sqlite3.connect(CONV_DB)
        try:
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
            for t in tables:
                cols = [r[1] for r in conn.execute(
                    f"PRAGMA table_info({t})").fetchall()]
                for col in cols:
                    try:
                        for (v,) in conn.execute(f"SELECT {col} FROM {t}"):
                            if v and isinstance(v, str):
                                scan_text(v)
                    except sqlite3.OperationalError:
                        pass
        finally:
            conn.close()

    # 4. Live agent workspaces' agent.json
    if os.path.isdir(WS_ROOT):
        with open(AGENTS_JSON) as f:
            agents_data = json.load(f)
        live_ids = {(a.get("id") or a.get("agent_id") or "").strip()
                    for a in agents_data.get("agents", [])}
        for aid in live_ids:
            if not aid:
                continue
            fp = os.path.join(WS_ROOT, aid, "agent.json")
            if os.path.isfile(fp):
                try:
                    with open(fp) as f:
                        scan_text(f.read())
                except OSError:
                    pass

    return referenced


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true",
                   help="Actually delete orphan collections from chroma.")
    p.add_argument("--list", action="store_true",
                   help="List orphan ids (otherwise just summary).")
    args = p.parse_args(argv)

    all_ids = list_dkb_ids()
    referenced = collect_referenced_ids(all_ids)
    orphans = all_ids - referenced

    print(f"Total tudou_domain_dkb_* collections: {len(all_ids)}")
    print(f"Referenced (any live source):          {len(referenced)}")
    print(f"Orphan (no reference found):           {len(orphans)}")

    if args.list:
        print("\nOrphan ids:")
        for oid in sorted(orphans):
            print(f"  {oid}")

    if not args.apply:
        print("\n[DRY RUN] Pass --apply to delete orphan chroma collections.")
        return 0

    # Apply: delete orphan chroma collections
    # We use chromadb's PersistentClient to do this so it cleans up
    # both the metadata row and the embedding shards.
    try:
        import chromadb  # type: ignore
    except ImportError:
        print("ERROR: chromadb not importable in this env.", file=sys.stderr)
        return 1
    client = chromadb.PersistentClient(path="/Users/pangwanchun/.tudou_claw/chromadb")
    deleted = 0
    for oid in orphans:
        cname = f"{DKB_PREFIX}{oid}"
        try:
            client.delete_collection(cname)
            deleted += 1
        except Exception as e:
            print(f"  FAIL {cname}: {e}")
    print(f"Deleted {deleted}/{len(orphans)} orphan collections.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
