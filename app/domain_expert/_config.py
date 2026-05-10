"""Module-wide config + path helpers + feature flag.

Single source of truth for paths and the TUDOU_EXPERT_DISABLED escape
hatch. Imported by every other sub-module that needs to know where to
read/write data.
"""
from __future__ import annotations
import os
from pathlib import Path

DISABLED_ENV_VAR = "TUDOU_EXPERT_DISABLED"


def is_disabled() -> bool:
    """Env-var feature flag — when '1', the entire module is a no-op.

    Used by api/routers.py (returns 503), agent reply pipeline hook
    (skips), and ExpertManager.is_available().
    """
    return os.environ.get(DISABLED_ENV_VAR, "0") == "1"


def expert_root() -> str:
    """Root for all expert persistent data (~/.tudou_claw/expert)."""
    home = os.path.expanduser("~")
    return os.path.join(home, ".tudou_claw", "expert")


def expert_dir_for(agent_id: str) -> str:
    """Per-agent expert data dir. Caller is responsible for makedirs.

    Layout (filled by Track A/C/D):
        <agent_id>/
            config.json           (ExpertProfile)
            corpus/               (raw source files)
            corpus/_manifest.json (CorpusManifest)
            vector_store.db       (sqlite-vss)
            lora/<v>/             (LoRA adapter snapshots)
            lora/current          (symlink → active version)
            traces/               (Q/A trace history, by month)
            datasets/             (synthesized RAFT data)
            eval/                 (eval reports)
    """
    if not agent_id:
        raise ValueError("agent_id required for expert_dir_for")
    return os.path.join(expert_root(), agent_id)


def template_dir() -> str:
    """Where shipped specialty templates live (app/data/specialty_templates/)."""
    here = Path(__file__).resolve().parent.parent  # app/
    return str(here / "data" / "specialty_templates")
