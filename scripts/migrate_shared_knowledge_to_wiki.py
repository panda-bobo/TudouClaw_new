#!/usr/bin/env python3
"""
One-shot migration: copy Shared Knowledge entries into the Wiki layer.

Step C of the Wiki / Shared-Knowledge merge plan.

Why
---
Pre-merge, two stores held overlapping content:
  - ~/.tudou_claw/shared_knowledge.json  (legacy_kb.py)
  - ~/.tudou_claw/wiki/...               (wiki_store.py)

Step A landed a unified admin UI for Wiki; Step B landed the import
endpoint. This script does the one-time bulk move so the Wiki tab
shows EVERYTHING admins have curated, not just new uploads.

What it does
------------
For each entry in shared_knowledge.json, write a wiki page to
``global/reference/<slug>.md`` with:

    title, body, tags                ← copied verbatim
    created_at, updated_at           ← copied verbatim
    kind                             = "reference"  (admin-curated docs)
    scope                            = "global"     (was implicitly global)
    tags += ["source:admin-migrated"] ← traceability

Idempotent
----------
Running twice is safe — if the target wiki page already exists with
the same body, the script SKIPS it. Use --force to overwrite.

Backup
------
Before writing the first page, a timestamped backup of
``shared_knowledge.json`` is dropped at
``~/.tudou_claw/shared_knowledge.json.pre-wiki-migration.<ts>``.

The script does NOT delete entries from shared_knowledge.json. After
verifying the wiki side works (open Portal → Wiki tab → spot-check),
you can either:

  - Leave shared_knowledge.json in place (Shared Knowledge tab keeps
    showing the same content; the merge is purely additive), OR
  - Empty shared_knowledge.json (write []) to fully decommission the
    legacy store. Step E (later) automates this cleanly.

Usage
-----
    # Dry run (recommended first)
    python scripts/migrate_shared_knowledge_to_wiki.py --dry-run

    # Real run (creates wiki pages, leaves SK untouched)
    python scripts/migrate_shared_knowledge_to_wiki.py

    # Overwrite existing wiki pages with same slug
    python scripts/migrate_shared_knowledge_to_wiki.py --force
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

# Make the app importable when invoked from repo root.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from app.knowledge.wiki_store import (  # noqa: E402
    WikiPage, WikiStore, slugify, get_wiki_store,
)


def _resolve_data_dir() -> Path:
    env = (os.environ.get("TUDOU_CLAW_DATA_DIR", "").strip()
           or os.environ.get("TUDOU_CLAW_HOME", "").strip())
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".tudou_claw"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be migrated without writing.")
    p.add_argument("--force", action="store_true",
                   help="Overwrite existing wiki pages with the same slug.")
    p.add_argument("--source-tag", default="admin-migrated",
                   help="Tag added to all migrated pages "
                        "(default: 'admin-migrated' → stored as "
                        "'source:admin-migrated').")
    args = p.parse_args()

    data_dir = _resolve_data_dir()
    sk_path = data_dir / "shared_knowledge.json"
    if not sk_path.is_file():
        print(f"shared_knowledge.json not found at {sk_path} — nothing to migrate")
        return 0

    try:
        entries = json.loads(sk_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"ERROR: failed to read {sk_path}: {e}", file=sys.stderr)
        return 1
    if not isinstance(entries, list):
        print(f"ERROR: shared_knowledge.json is not a list (got {type(entries).__name__})",
              file=sys.stderr)
        return 1
    print(f"Loaded {len(entries)} entries from {sk_path}")

    # Backup before writing.
    if not args.dry_run and entries:
        ts = int(time.time())
        backup = sk_path.with_suffix(
            sk_path.suffix + f".pre-wiki-migration.{ts}"
        )
        shutil.copy2(sk_path, backup)
        print(f"Backup written: {backup}")

    store: WikiStore = get_wiki_store()

    migrated = 0
    skipped = 0
    overwrote = 0
    for entry in entries:
        if not isinstance(entry, dict):
            print(f"  SKIP (not a dict): {entry!r}")
            skipped += 1
            continue

        title = str(entry.get("title") or "").strip()
        body = str(entry.get("content") or "").strip()
        if not title:
            print(f"  SKIP (empty title): {entry.get('id', '<no id>')}")
            skipped += 1
            continue
        if not body:
            print(f"  SKIP (empty content): {title!r}")
            skipped += 1
            continue

        slug = slugify(title)
        scope = "global"
        kind = "reference"

        existing = store.read_page(scope, kind, slug)
        if existing is not None:
            if existing.body.strip() == body and not args.force:
                print(f"  skip-already-migrated: {title!r}  (slug={slug})")
                skipped += 1
                continue
            elif not args.force:
                print(f"  SKIP (exists with different body, use --force): {title!r}")
                skipped += 1
                continue
            else:
                overwrote += 1

        # Tags: copy original + stamp source for traceability.
        raw_tags = list(entry.get("tags") or [])
        tags = [str(t).strip() for t in raw_tags if str(t).strip()]
        source_tag = f"source:{args.source_tag}"
        if source_tag not in tags:
            tags.append(source_tag)

        page = WikiPage(
            scope=scope, kind=kind, slug=slug,
            title=title, body=body, tags=tags,
            sources=[], related=[],
            created_at=float(entry.get("created_at") or 0.0),
            updated_at=float(entry.get("updated_at") or 0.0),
        )

        if args.dry_run:
            verb = "WOULD-OVERWRITE" if existing is not None else "WOULD-CREATE"
            print(f"  {verb}: {scope}/{kind}/{slug}  ({len(body)} chars, "
                  f"{len(tags)} tags)")
        else:
            store.write_page(page, log_action="import-from-shared-knowledge")
            verb = "OVERWROTE" if existing is not None else "CREATED"
            print(f"  {verb}: {scope}/{kind}/{slug}  ({len(body)} chars)")
            migrated += 1

    print()
    print(f"Done. migrated={migrated}  skipped={skipped}  overwrote={overwrote}")
    if args.dry_run:
        print("(dry-run — nothing written. Re-run without --dry-run to commit.)")
    else:
        print("Verify in Portal → Knowledge & Memory → Wiki / 经验库 tab.")
        print("Note: shared_knowledge.json was NOT modified — both stores")
        print("      now contain the entries. Empty out SK manually after")
        print("      you confirm the wiki side looks right.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
