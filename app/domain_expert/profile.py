"""ExpertProfile — per-agent persistent expert configuration.

Lives at ~/.tudou_claw/expert/<agent_id>/config.json.

Phase 0: minimal fields. Track D / V1 vertical adds full schema.
"""
from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass, field, asdict


@dataclass
class ExpertProfile:
    """Phase 0 minimal struct. Track D will add corpus_sources,
    cite_required, confidence_threshold, etc."""
    agent_id: str
    specialty: str                         # "legal" / "medical" / ...
    template_id: str                       # e.g. "legal-expert"
    template_version: str                  # "1.0"
    level: str = "novice"                  # novice / journeyman / expert / master
    active_lora_version: str = ""
    last_eval_score: float = 0.0
    initialized_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ExpertProfile":
        return ExpertProfile(**{
            k: v for k, v in d.items()
            if k in ExpertProfile.__dataclass_fields__
        })

    @staticmethod
    def load(agent_id: str) -> "ExpertProfile | None":
        """Load profile from disk. Returns None if not initialized yet."""
        from ._config import expert_dir_for
        p = os.path.join(expert_dir_for(agent_id), "config.json")
        if not os.path.exists(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                return ExpertProfile.from_dict(json.load(f))
        except (json.JSONDecodeError, OSError):
            return None

    def save(self) -> None:
        """Persist to ~/.tudou_claw/expert/<agent_id>/config.json."""
        from ._config import expert_dir_for
        d = expert_dir_for(self.agent_id)
        os.makedirs(d, exist_ok=True)
        self.updated_at = time.time()
        path = os.path.join(d, "config.json")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
