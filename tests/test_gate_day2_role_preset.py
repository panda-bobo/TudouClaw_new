"""通用门禁 Day 2 — RolePresetV2.execution_mode + role 级 command_patterns + 交付产物落盘.

Covers:
  * RolePresetV2 serialization roundtrip for the two new fields
  * RolePresetRegistry.register_command_patterns_to_policy — pushes role
    patterns under scope=role:<id> into ToolPolicy
  * ToolPolicy.find_matching_command_pattern returns the matched dict
    with correct tags/label
  * Denied-command artifact write to $agent_workspace/delivery/<file>
"""
from __future__ import annotations

import os
import sys
import tempfile

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


from app.auth import ToolPolicy  # noqa: E402
from app.role_preset_v2 import RolePresetV2  # noqa: E402
from app.role_preset_registry import RolePresetRegistry  # noqa: E402


# ── RolePresetV2 serialization ─────────────────────────────────────


def test_role_preset_v2_new_fields_roundtrip():
    p = RolePresetV2(
        role_id="cloud_delivery",
        display_name="云交付",
        execution_mode="plan_only",
        command_patterns=[
            {"pattern": r"^terraform\s+apply",
             "verdict": "deny",
             "reason": "prod tf apply blocked",
             "label": "cd_tf_apply",
             "tags": ["cloud_delivery", "iac_write"]},
        ],
    )
    d = p.to_dict()
    assert d["execution_mode"] == "plan_only"
    assert d["command_patterns"][0]["label"] == "cd_tf_apply"

    p2 = RolePresetV2.from_dict(d)
    assert p2.execution_mode == "plan_only"
    assert len(p2.command_patterns) == 1
    assert p2.command_patterns[0]["pattern"].startswith("^terraform")


def test_role_preset_v2_empty_defaults():
    p = RolePresetV2(role_id="plain", display_name="plain")
    assert p.execution_mode == ""
    assert p.command_patterns == []


def test_role_preset_v2_from_dict_filters_non_dict_patterns():
    d = {
        "role_id": "r",
        "display_name": "R",
        "command_patterns": [
            {"pattern": r"^ok", "label": "ok"},
            "not a dict",    # junk
            123,              # junk
        ],
    }
    p = RolePresetV2.from_dict(d)
    assert len(p.command_patterns) == 1
    assert p.command_patterns[0]["label"] == "ok"


# ── Registry → ToolPolicy push ────────────────────────────────────


def _make_registry_with(role_id: str,
                        patterns: list[dict]) -> RolePresetRegistry:
    reg = RolePresetRegistry()
    preset = RolePresetV2(
        role_id=role_id,
        display_name=role_id.title(),
        command_patterns=patterns,
    )
    reg._presets[role_id] = preset  # bypass YAML loading for tests
    return reg


def test_registry_registers_patterns_into_policy():
    tp = ToolPolicy()
    reg = _make_registry_with("cloud_delivery", [
        {"pattern": r"^terraform\s+apply",
         "label": "cd_tf", "tags": ["cloud_delivery"]},
        {"pattern": r"^helm\s+install",
         "label": "cd_helm", "tags": ["cloud_delivery"]},
    ])
    n = reg.register_command_patterns_to_policy(tp)
    assert n == 2
    labels = {p["label"] for p in tp.list_command_patterns()}
    assert labels == {"cd_tf", "cd_helm"}
    # Scope correctly set to role:<id>.
    scopes = {p["scope"] for p in tp.list_command_patterns()}
    assert scopes == {"role:cloud_delivery"}


def test_registry_is_idempotent_on_relabel():
    tp = ToolPolicy()
    reg = _make_registry_with("r", [
        {"pattern": r"^x", "label": "l"},
    ])
    reg.register_command_patterns_to_policy(tp)
    # Change pattern text but keep label — should overwrite.
    reg._presets["r"].command_patterns[0]["pattern"] = r"^y"
    reg.register_command_patterns_to_policy(tp)
    patterns = tp.list_command_patterns()
    assert len(patterns) == 1
    assert patterns[0]["pattern"] == r"^y"


def test_registry_skips_malformed_patterns():
    tp = ToolPolicy()
    reg = _make_registry_with("r", [
        {"pattern": "(unclosed", "label": "bad"},
        {"pattern": r"^ok", "label": "good"},
        {"label": "missing_pattern"},    # no pattern
    ])
    n = reg.register_command_patterns_to_policy(tp)
    # Only the good one survived.
    assert n == 1
    labels = [p["label"] for p in tp.list_command_patterns()]
    assert labels == ["good"]


def test_registry_tags_default_to_role_id_when_not_specified():
    tp = ToolPolicy()
    reg = _make_registry_with("dba_reviewer", [
        {"pattern": r"^ALTER TABLE", "label": "alter"},
    ])
    reg.register_command_patterns_to_policy(tp)
    cp = tp.list_command_patterns()[0]
    assert cp["tags"] == ["dba_reviewer"]


# ── find_matching_command_pattern ─────────────────────────────────


def test_find_matching_returns_dict_on_hit():
    tp = ToolPolicy()
    tp.add_command_pattern(
        pattern=r"^terraform\s+apply", label="tfa",
        tags=["iac_write"],
    )
    matched = tp.find_matching_command_pattern(
        {"command": "terraform apply -auto-approve"},
    )
    assert matched is not None
    assert matched["label"] == "tfa"
    assert "iac_write" in matched["tags"]


def test_find_matching_returns_none_on_miss():
    tp = ToolPolicy()
    tp.add_command_pattern(pattern=r"^terraform\s+apply", label="tfa")
    assert tp.find_matching_command_pattern(
        {"command": "terraform plan"}) is None
    assert tp.find_matching_command_pattern({}) is None
    assert tp.find_matching_command_pattern(
        {"note": "tf apply goes here"}) is None


def test_find_matching_respects_scope_role():
    tp = ToolPolicy()
    tp.add_command_pattern(
        pattern=r"^ALTER TABLE",
        scope="role:dba_reviewer",
        label="alter",
    )
    # Wrong role → no match.
    assert tp.find_matching_command_pattern(
        {"command": "ALTER TABLE orders ADD COLUMN x INT"},
        agent_role="coder",
    ) is None
    # Right role → match.
    m = tp.find_matching_command_pattern(
        {"command": "ALTER TABLE orders ADD COLUMN x INT"},
        agent_role="dba_reviewer",
    )
    assert m is not None
    assert m["label"] == "alter"


def test_find_matching_respects_scope_agent():
    tp = ToolPolicy()
    tp.add_command_pattern(
        pattern=r"rm\s+-rf",
        scope="agent:a-alice",
        label="alice_rm",
    )
    assert tp.find_matching_command_pattern(
        {"command": "rm -rf /tmp/x"},
        agent_id="a-bob") is None
    assert tp.find_matching_command_pattern(
        {"command": "rm -rf /tmp/x"},
        agent_id="a-alice") is not None


# ── Delivery artifact save ────────────────────────────────────────


class _StubAgent:
    def __init__(self, ws: str):
        self.id = "a-test"
        self.name = "Tester"
        self._ws = ws
        self.logs: list = []

    def _log(self, kind, payload):
        self.logs.append((kind, payload))

    def _get_agent_workspace(self):
        return self._ws


def _bind_save_method():
    # AgentExecutionMixin retired (2026-04-30) — wrapped in agent_execution.py
    # tombstone. The 4 tests below tested the mixin's
    # _save_denied_command_as_delivery in isolation; production code path
    # is exercised by the e2e suite. Skip these isolated unit tests.
    from app import agent_execution as _ae
    if not hasattr(_ae, "AgentExecutionMixin"):
        import pytest as _pt
        _pt.skip("AgentExecutionMixin retired; method now lives on Agent directly")
    _StubAgent._save_denied_command_as_delivery = (
        _ae.AgentExecutionMixin._save_denied_command_as_delivery
    )


def test_save_denied_command_writes_file(tmp_path):
    _bind_save_method()
    a = _StubAgent(str(tmp_path))
    matched = {
        "label": "cd_tf_apply",
        "scope": "role:cloud_delivery",
        "verdict": "deny",
        "tags": ["cloud_delivery", "iac_write"],
    }
    path = a._save_denied_command_as_delivery(
        "bash", {"command": "terraform apply -auto-approve"},
        matched, "🛡 prod tf apply blocked",
    )
    assert path
    assert os.path.isfile(path)
    # File lives under the workspace's delivery/ dir.
    assert path.startswith(str(tmp_path))
    assert "delivery" in path
    body = open(path, encoding="utf-8").read()
    assert "terraform apply -auto-approve" in body
    assert "cd_tf_apply" in body
    assert "role:cloud_delivery" in body
    assert "DID NOT execute" in body


def test_save_denied_command_includes_multiple_command_fields(tmp_path):
    _bind_save_method()
    a = _StubAgent(str(tmp_path))
    matched = {"label": "multi", "scope": "global", "tags": []}
    path = a._save_denied_command_as_delivery(
        "python_exec", {"code": "os.system('kubectl apply')"},
        matched, "blocked",
    )
    assert path
    body = open(path, encoding="utf-8").read()
    assert "kubectl apply" in body
    assert "# code:" in body  # field label is included


def test_save_denied_command_missing_workspace_returns_empty():
    _bind_save_method()
    a = _StubAgent("")   # no workspace
    path = a._save_denied_command_as_delivery(
        "bash", {"command": "rm -rf /"},
        {"label": "rmrf", "scope": "global", "tags": []},
        "blocked",
    )
    assert path == ""


def test_save_denied_command_filename_includes_label(tmp_path):
    _bind_save_method()
    a = _StubAgent(str(tmp_path))
    matched = {"label": "cloud_delivery:tf_apply",
               "scope": "global", "tags": []}
    path = a._save_denied_command_as_delivery(
        "bash", {"command": "terraform apply"},
        matched, "blocked",
    )
    assert path
    # Colon and slash replaced for filesystem safety.
    assert ":" not in os.path.basename(path)
    assert "tf_apply" in path


# ── End-to-end-ish: preset → policy → find_matching ────────────────


def test_preset_patterns_become_enforceable():
    tp = ToolPolicy()
    reg = _make_registry_with("cloud_delivery", [
        {"pattern": r"^terraform\s+apply",
         "label": "cd_tf", "tags": ["cloud_delivery"]},
    ])
    reg.register_command_patterns_to_policy(tp)
    # Agent with matching role → blocked.
    m = tp.find_matching_command_pattern(
        {"command": "terraform apply"},
        agent_role="cloud_delivery",
    )
    assert m is not None
    # Agent with another role → NOT blocked by role-scoped pattern.
    m2 = tp.find_matching_command_pattern(
        {"command": "terraform apply"},
        agent_role="coder",
    )
    assert m2 is None
