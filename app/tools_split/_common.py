"""Shared helpers used by multiple category modules.

Kept minimal — only functions genuinely used from 2+ modules belong
here. Single-use helpers stay in their category module to keep imports
tight and cohesion high.
"""
from __future__ import annotations


def _get_hub():
    """Return the singleton hub. Lazy import breaks the import cycle
    app.tools -> app.tools_split.* -> app.hub -> app.agent -> app.tools.
    """
    from ..hub import get_hub
    return get_hub()
