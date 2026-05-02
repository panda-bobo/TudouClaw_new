"""Tests for the canvas-executor deliverable variable contract.

Companion to docs/superpowers/specs/2026-05-02-canvas-deliverable-design.md.
"""
from __future__ import annotations
import os
import tempfile
from pathlib import Path
import pytest

from app.canvas_artifacts import ArtifactStore


def test_outputs_dict_has_deliverable_no_legacy_keys():
    """outputs returned by _exec_agent contain `deliverable` and
    `deliverable_relative` but NOT the legacy `deliverable_type` or
    `success_marker_file` keys (cleaned up after spec approval)."""
    import app.canvas_executor as ce
    src = Path(ce.__file__).read_text(encoding="utf-8")
    assert '"deliverable_type"' not in src, (
        "deliverable_type leftover in canvas_executor.py — should be dropped"
    )
    assert '"success_marker_file"' not in src, (
        "success_marker_file leftover in canvas_executor.py — superseded by deliverable"
    )
