"""ExpertManager singleton — module entry point.

Phase 0 stub. Track D + verticals expand: list profiles, get profile by
agent, apply template, etc.
"""
from __future__ import annotations
from . import _config


class ExpertManager:
    """Singleton entry. Phase 0 only knows whether the module is enabled."""

    def __init__(self):
        pass

    def is_available(self) -> bool:
        """False if TUDOU_EXPERT_DISABLED=1 in env."""
        return not _config.is_disabled()


_singleton: ExpertManager | None = None


def get_manager() -> ExpertManager:
    global _singleton
    if _singleton is None:
        _singleton = ExpertManager()
    return _singleton
