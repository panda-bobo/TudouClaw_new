"""Seed a per-agent KB from a SpecialtyTemplate's kb_seeds list.

KBSeeds are reference materials (red-line lists, SOPs, statute
excerpts, internal docs, etc.) that EVERY agent of a sub-specialty
should start with. On cultivate, the seed loader reads each file,
chunks it via the paragraph chunker, and writes it to the agent's
private KB at ``~/.tudou_claw/expert/<agent_id>/corpus/<source_id>/``.

Once seeded, the chunks are part of that agent's KB just like
user-uploaded documents — retrieval treats them uniformly. The
``metadata.type`` mirrors KBSeed.type so R5's typed RAG injection
can group / filter (red_line / sop / law / template / case /
internal_doc / reference).

Idempotency: re-running with the same template OVERWRITES the seeded
sources (the source_id is derived from KBSeed.file). User-uploaded
sources are untouched — they have unrelated source_ids without the
``seed_`` prefix.

Public API:
    ingest_seeds_into_agent_kb(agent_id, template,
                               *, template_dir=None) -> list[CorpusSourceEntry]
        Ingest every KBSeed on the template. Returns the manifest
        entries actually added/updated. Missing seed files are
        skipped silently — the cultivation flow continues.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time

from . import _config
from .corpus import chunker as _chunker
from .corpus.manifest import CorpusManifest, CorpusSourceEntry
from .template import KBSeed, SpecialtyTemplate

logger = logging.getLogger("tudouclaw.expert.kb_seed_loader")

# Source-ids for seeded chunks carry this prefix so the UI can mark
# them as "shipped with template" vs "user uploaded".
SEED_SOURCE_PREFIX = "seed_"


def resolve_seed_path(seed_file: str, template_dir: str | None = None) -> str:
    """Resolve a seed-file path.

    Absolute paths used as-is. Relative paths resolved against
    ``<template_dir>/seeds/`` (default = production template dir),
    so seeds ship next to the YAML that references them::

        app/data/specialty_templates/seeds/legal/civil_code.md
    """
    if not seed_file:
        raise ValueError("seed.file is required")
    if os.path.isabs(seed_file):
        return seed_file
    base = template_dir or _config.template_dir()
    return os.path.join(base, "seeds", seed_file)


def _seed_source_id(seed: KBSeed) -> str:
    """Derive a filesystem-safe source_id from KBSeed.file.

    Strips dirs + extension, replaces invalid chars with '_', and
    prefixes ``seed_``. Matches the API validator regex
    ``^[A-Za-z0-9][A-Za-z0-9_.:-]*$``.
    """
    base = os.path.basename(seed.file)
    name, _ext = os.path.splitext(base)
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]", "_", name)
    if not cleaned:
        cleaned = "anon"
    return SEED_SOURCE_PREFIX + cleaned


def ingest_seeds_into_agent_kb(
    agent_id: str,
    template: SpecialtyTemplate | None,
    *,
    template_dir: str | None = None,
) -> list[CorpusSourceEntry]:
    """For each entry in ``template.kb_seeds``:

      1. Resolve the file path via :func:`resolve_seed_path`.
      2. Read content (skip silently if missing or empty).
      3. Chunk via the ``paragraph`` chunker, attaching ``type`` and
         ``title`` to chunk metadata so R5 typed RAG can group/filter.
      4. Write ``corpus/<source_id>/chunks.jsonl`` (overwriting any
         prior seed of the same name — idempotent).
      5. Update the agent's CorpusManifest with the new entry.

    Returns the list of manifest entries added/updated. The function
    NEVER raises on a missing/unreadable seed file — partial seeding
    still produces a usable agent. Inspect the return value if you
    want to know which seeds actually landed.
    """
    if not agent_id:
        raise ValueError("agent_id required")
    if template is None or not template.kb_seeds:
        return []

    manifest = CorpusManifest.load(agent_id)
    base_dir = _config.expert_dir_for(agent_id)
    out: list[CorpusSourceEntry] = []

    for seed in template.kb_seeds:
        try:
            seed_path = resolve_seed_path(seed.file, template_dir=template_dir)
        except ValueError:
            logger.warning("kb_seed for template %s has empty file; skipping",
                           template.id)
            continue
        if not os.path.isfile(seed_path):
            logger.info("kb_seed file not found, skipping: %s (template %s)",
                        seed_path, template.id)
            continue
        try:
            with open(seed_path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            logger.warning("kb_seed read failed: %s (%s)", seed_path, e)
            continue
        if not content.strip():
            continue

        source_id = _seed_source_id(seed)
        chunks_dir = os.path.join(base_dir, "corpus", source_id)
        os.makedirs(chunks_dir, exist_ok=True)
        chunks_jsonl = os.path.join(chunks_dir, "chunks.jsonl")

        # Use paragraph chunker — works for plain text + light Markdown.
        # Specialty-specific chunkers (legal §-aware, etc.) come in R6
        # when we wire real RAG.
        chunker = _chunker.get("paragraph")
        seed_type = (seed.type or "reference").strip() or "reference"
        seed_title = seed.title.strip() if seed.title else os.path.basename(seed.file)
        source_meta = {
            "source_id": source_id,
            "type": seed_type,
            "title": seed_title,
            "ingested_at": time.time(),
            "from_template": template.id,
            "template_version": template.version,
        }

        chunk_count = 0
        bytes_written = 0
        with open(chunks_jsonl, "w", encoding="utf-8") as f:
            for chunk in chunker.chunk(content, source_meta):
                rec = {"text": chunk.text, "metadata": dict(chunk.metadata)}
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                chunk_count += 1
                bytes_written += len(chunk.text.encode("utf-8"))

        entry = CorpusSourceEntry(
            source_id=source_id,
            version=template.version,
            chunk_count=chunk_count,
            bytes=bytes_written,
            indexed_at=time.time(),
            chunker_strategy="paragraph",
            notes=f"seeded from {template.id} ({seed_type}: {seed_title})",
        )
        manifest.add_source(entry)
        out.append(entry)

    if out:
        manifest.save()
    return out
