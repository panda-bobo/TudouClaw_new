"""Prune low-signal garbage from agents' L3 semantic memory (Chroma).

Removes:
  - Templated "本次会话执行了 N 个操作 (工具: ...)" noise facts
  - Per-step "[步骤N] ..." action_plan facts (replaced by single goal per plan)
  - Content that's just punctuation / fragments / too short to be useful

Run pattern:
    python scripts/prune_l3_facts.py --dry-run         # preview
    python scripts/prune_l3_facts.py --apply           # delete
    python scripts/prune_l3_facts.py --apply --agent <id>   # single agent

Safe: never touches a fact category in (preference / user_pref) —
user-profile entries are always preserved regardless of length.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Import pattern from app tree
_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

# Regex patterns for templated garbage. Conservative — only match things
# that CAN'T carry reusable signal.
_GARBAGE_PATTERNS = [
    # "本次会话执行了 N 个操作 (工具: X, Y)" and minor variants
    re.compile(r"本次会话执行了\s*\d+\s*个操作"),
    # "[日期] 完成 N 次文件写入和 M 次命令" — pure count summary
    re.compile(r"^\[?[^\]]*\]?\s*(最终结果[:：])?\s*完成\s*\d+\s*次[^。]*和\s*\d+\s*次"),
    # "[步骤N] ..." — per-step action_plan (deprecated writer)
    re.compile(r"^\[步骤\s*\d+\]"),
    # "[当前日期] 操作执行完毕，计划与命令均已成功处理，状态已更新，无失败原因"
    # — LLM aggregator placeholder when it had nothing concrete to say
    re.compile(r"操作执行完毕"),
    # "未提供日期，最终结果未明确" variants
    re.compile(r"未提供日期"),
    re.compile(r"最终结果未明确"),
    # "状态已更新，无失败原因" — generic non-content summary
    re.compile(r"状态已?更新.{0,8}无失败"),
    # "执行了该步骤" / "步骤完成无异常" — content-free
    re.compile(r"执行了该步骤"),
    re.compile(r"步骤完成无异常"),
    # Pure open bracket / single char / empty-ish content
    re.compile(r"^[\[\](){},，。.;；\s]*$"),
    # Nov 2026: more LLM template noise
    re.compile(r"无法确定.{0,20}缺少日期"),
    re.compile(r"\[日期\]\s*无法"),
    re.compile(r"\[未知日期\]"),
    re.compile(r"缺少日期.*关键信息"),
    re.compile(r"状态已?更新.{0,5}无失败"),
]
_MIN_USEFUL_LEN = 15  # excluding preference category

# Categories that bypass all length / garbage filters
_ALWAYS_KEEP = {"preference", "user_pref"}


def _is_garbage(content: str, category: str) -> tuple[bool, str]:
    """Return (is_garbage, reason). Preferences always kept."""
    if category in _ALWAYS_KEEP:
        return (False, "preference-always-kept")
    if not content or not content.strip():
        return (True, "empty")
    c = content.strip()
    for pat in _GARBAGE_PATTERNS:
        if pat.search(c):
            return (True, f"pattern:{pat.pattern[:40]}")
    if len(c) < _MIN_USEFUL_LEN:
        return (True, f"too-short({len(c)})")
    return (False, "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="Preview only (default)")
    ap.add_argument("--apply", action="store_true",
                    help="Actually delete matched facts")
    ap.add_argument("--agent", default="",
                    help="Restrict to one agent_id (default: all)")
    args = ap.parse_args()

    if args.apply:
        args.dry_run = False

    # Connect directly to the Chroma store used by MemoryManager.
    try:
        import chromadb
    except ImportError:
        print("ERROR: chromadb package not installed in this env", file=sys.stderr)
        return 2

    persist_dir = str(Path.home() / ".tudou_claw" / "chromadb")
    client = chromadb.PersistentClient(path=persist_dir)

    try:
        coll = client.get_collection("tudou_memory_facts")
    except Exception as e:
        print(f"ERROR: collection tudou_memory_facts not found: {e}")
        return 2

    # Pull everything in one page — ok for a few hundred facts
    try:
        result = coll.get(include=["documents", "metadatas"])
    except Exception as e:
        print(f"ERROR: coll.get failed: {e}", file=sys.stderr)
        return 2

    ids = result.get("ids", []) or []
    docs = result.get("documents", []) or []
    metas = result.get("metadatas", []) or []

    total = len(ids)
    to_delete: list[str] = []
    kept_by_reason: dict[str, int] = {}
    drop_by_reason: dict[str, int] = {}
    print(f"Scanning {total} facts...\n")

    for fid, doc, meta in zip(ids, docs, metas):
        meta = meta or {}
        cat = str(meta.get("category") or "")
        aid = str(meta.get("agent_id") or "")
        if args.agent and aid != args.agent:
            continue
        is_g, reason = _is_garbage(doc or "", cat)
        if is_g:
            to_delete.append(fid)
            drop_by_reason[reason] = drop_by_reason.get(reason, 0) + 1
        else:
            kept_by_reason[cat or "?"] = kept_by_reason.get(cat or "?", 0) + 1

    print(f"Keep  {sum(kept_by_reason.values())}  ({kept_by_reason})")
    print(f"Drop  {len(to_delete)}")
    for reason, n in sorted(drop_by_reason.items(), key=lambda x: -x[1]):
        print(f"  {n:4d}  {reason}")

    if not to_delete:
        print("\nNothing to prune. Done.")
        return 0

    if args.dry_run:
        print("\n(dry-run — no changes made; add --apply to delete)")
        # Print first 10 sample
        print("\nSample of entries that would be deleted:")
        for fid, doc in zip(ids, docs):
            if fid in to_delete:
                print(f"  [{fid[:8]}] {(doc or '')[:80]}")
                if sum(1 for x in to_delete[:10] if x == fid) >= 1:
                    pass
        return 0

    # Apply
    print(f"\nDeleting {len(to_delete)} facts from Chroma ...")
    coll.delete(ids=to_delete)

    # Also delete from SQLite memory.db if rows exist there
    try:
        import sqlite3
        dbp = Path.home() / ".tudou_claw" / "memory.db"
        if dbp.exists():
            con = sqlite3.connect(str(dbp))
            placeholders = ",".join(["?"] * len(to_delete))
            con.execute(
                f"DELETE FROM memory_semantic WHERE id IN ({placeholders})",
                to_delete,
            )
            con.commit()
            con.close()
    except Exception as e:
        print(f"(non-fatal) sqlite cleanup: {e}")

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
