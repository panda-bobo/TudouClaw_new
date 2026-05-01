"""通用门禁 Day 4 — End-to-end integration.

Stitches together the full pipeline:

  cloud_delivery.yaml
        ↓ load_role_yaml
  RolePresetV2 (execution_mode + command_patterns)
        ↓ register_command_patterns_to_policy
  ToolPolicy (+ rule_command_patterns registered)
        ↓ policy.check_tool(bash, {command: "terraform apply"})
  verdict=deny
        ↓ agent_execution deny branch
  _save_denied_command_as_delivery()
        ↓
  $workspace/delivery/<ts>_<label>.txt is written

One test walks this entire path; others verify single-point invariants.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)


from app.auth import ToolPolicy  # noqa: E402
from app.role_preset_v2 import load_role_yaml  # noqa: E402
from app.role_preset_registry import RolePresetRegistry  # noqa: E402

_YAML = Path(_ROOT) / "data" / "roles" / "cloud_delivery.yaml"


# ── Stubs for the full deny-branch flow ────────────────────────────


class _FakeHub:
    def __init__(self, agents):
        self.agents = agents

    def get_agent(self, aid):
        return self.agents.get(aid)


class _StubAgent:
    def __init__(self, aid, name, role, ws):
        self.id = aid
        self.name = name
        self.role = role
        self._ws = ws
        self.logs: list = []

    def _log(self, kind, payload):
        self.logs.append((kind, payload))

    def _get_agent_workspace(self):
        return self._ws


def _bind_save():
    # AgentExecutionMixin retired (2026-04-30); see agent_execution.py
    # tombstone. Skip if the mixin's no longer there.
    from app import agent_execution as _ae
    if not hasattr(_ae, "AgentExecutionMixin"):
        import pytest as _pt
        _pt.skip("AgentExecutionMixin retired; covered by e2e instead")
    _StubAgent._save_denied_command_as_delivery = (
        _ae.AgentExecutionMixin._save_denied_command_as_delivery
    )


@pytest.fixture
def preset():
    p = load_role_yaml(_YAML)
    assert p is not None
    return p


@pytest.fixture
def tp(preset):
    tp = ToolPolicy()
    reg = RolePresetRegistry()
    reg._presets[preset.role_id] = preset
    reg.register_command_patterns_to_policy(tp)
    return tp


@pytest.fixture
def hub_patch(monkeypatch, preset):
    agent = _StubAgent("a-alice", "Alice", preset.role_id, "")
    fake = _FakeHub({"a-alice": agent})
    import app.hub as hub_mod
    monkeypatch.setattr(hub_mod, "get_hub", lambda: fake)
    return agent


# ── E2E: terraform apply is blocked + command saved ────────────────


def test_cloud_delivery_tf_apply_blocks_and_persists(tp, hub_patch, tmp_path):
    _bind_save()
    agent = hub_patch
    agent._ws = str(tmp_path)

    # 1. Rule chain → deny verdict
    verdict, reason = tp.check_tool(
        "bash",
        {"command": "terraform apply -auto-approve ./prod/"},
        agent_id="a-alice",
    )
    assert verdict == "deny"
    assert "terraform" in reason.lower() or "cd_tf" in reason.lower() or "云交付" in reason

    # 2. Dispatcher would now call find_matching_command_pattern to
    #    decide if this is a delivery-worthy deny. Simulate that.
    matched = tp.find_matching_command_pattern(
        {"command": "terraform apply -auto-approve ./prod/"},
        agent_id="a-alice", agent_role="cloud_delivery",
    )
    assert matched is not None
    assert matched["label"] == "cd_tf_apply"
    assert "cloud_delivery" in matched["tags"]

    # 3. Delivery artifact written
    path = agent._save_denied_command_as_delivery(
        "bash",
        {"command": "terraform apply -auto-approve ./prod/"},
        matched, reason,
    )
    assert path
    assert os.path.isfile(path)
    # File is under the agent workspace's delivery/ dir
    assert str(tmp_path) in path
    assert os.path.dirname(path).endswith("delivery")

    body = open(path, encoding="utf-8").read()
    assert "terraform apply -auto-approve ./prod/" in body
    assert "cd_tf_apply" in body
    assert "DID NOT execute" in body
    assert "role:cloud_delivery" in body


# ── Each expected dangerous command surfaces a delivery file ───────


@pytest.mark.parametrize("cmd,label", [
    ("terraform apply", "cd_tf_apply"),
    ("kubectl apply -f deploy.yaml", "cd_kubectl_write"),
    ("helm install myapp ./chart", "cd_helm_write"),
    ("ansible-playbook site.yml", "cd_ansible_playbook"),
    ("aws ec2 create-instance --image-id ami-x", "cd_aws_write"),
    ("DROP TABLE users", "cd_sql_ddl"),
])
def test_each_dangerous_command_produces_delivery(
        tp, hub_patch, tmp_path, cmd, label):
    _bind_save()
    agent = hub_patch
    agent._ws = str(tmp_path / label)   # separate dir per case
    os.makedirs(agent._ws, exist_ok=True)
    verdict, reason = tp.check_tool(
        "bash", {"command": cmd}, agent_id="a-alice",
    )
    assert verdict == "deny"
    matched = tp.find_matching_command_pattern(
        {"command": cmd}, agent_id="a-alice", agent_role="cloud_delivery",
    )
    assert matched is not None and matched["label"] == label
    path = agent._save_denied_command_as_delivery(
        "bash", {"command": cmd}, matched, reason,
    )
    assert os.path.isfile(path)
    # Filename encodes the label.
    assert label in path


# ── Safe commands pass through (chain returns allow) ──────────────


@pytest.mark.parametrize("cmd", [
    "terraform plan",
    "kubectl diff -f deploy.yaml",
    "helm template myapp ./chart",
    "ansible-playbook site.yml --check",
    "aws ec2 describe-instances",
    "SELECT * FROM users LIMIT 10",
])
def test_safe_cloud_delivery_commands_allowed(tp, hub_patch, cmd):
    verdict, _ = tp.check_tool(
        "bash", {"command": cmd}, agent_id="a-alice",
    )
    # These are "moderate/high" in the bash_analyzer chain — they should
    # land on "allow" or "agent_approvable" (the chain may auto-approve
    # for a priority-3 agent). The key assertion is NOT "deny".
    assert verdict != "deny", (
        f"safe cloud-delivery command wrongly denied: {cmd!r}"
    )


# ── Isolation: other roles NOT affected ───────────────────────────


def test_non_cloud_delivery_role_can_run_terraform_apply(tp, monkeypatch):
    # Coder role is NOT scoped into cloud_delivery patterns.
    coder = _StubAgent("a-bob", "Bob", "coder", "")
    import app.hub as hub_mod
    monkeypatch.setattr(hub_mod, "get_hub",
                        lambda: _FakeHub({"a-bob": coder}))
    verdict, _ = tp.check_tool(
        "bash", {"command": "terraform apply"}, agent_id="a-bob",
    )
    # coder role has no role-scoped pattern matching this.
    # Whether it's "allow" or escalates depends on bash_analyzer chain,
    # but it must NOT be blocked by the cloud_delivery rules.
    # (Concretely: find_matching_command_pattern returns None.)
    assert tp.find_matching_command_pattern(
        {"command": "terraform apply"},
        agent_role="coder",
    ) is None


# ── Delivery file naming / structure ──────────────────────────────


def test_delivery_file_naming_and_body_shape(tp, hub_patch, tmp_path):
    _bind_save()
    agent = hub_patch
    agent._ws = str(tmp_path)
    cmd = "kubectl apply -f prod-deploy.yaml"
    matched = tp.find_matching_command_pattern(
        {"command": cmd}, agent_id="a-alice", agent_role="cloud_delivery",
    )
    path = agent._save_denied_command_as_delivery(
        "bash", {"command": cmd}, matched, "🛡 test reason",
    )
    fname = os.path.basename(path)
    # Naming: <YYYYMMDD>_<HHMMSS>_<label>.txt
    import re as _re
    assert _re.match(r"\d{8}_\d{6}_cd_kubectl_write\.txt$", fname), fname
    body = open(path, encoding="utf-8").read()
    # Structured header lines
    for key in ("# Agent:", "# Tool:", "# Blocked at:",
                "# Rule label:", "# Verdict:", "# Reason:"):
        assert key in body, f"missing header {key!r}"
    # Command content preserved verbatim
    assert cmd in body


# ── Full-preset smoke test (shipping config integrity) ────────────


def test_shipped_preset_registers_exactly_expected_labels(preset):
    tp = ToolPolicy()
    reg = RolePresetRegistry()
    reg._presets[preset.role_id] = preset
    n = reg.register_command_patterns_to_policy(tp)
    assert n == len(preset.command_patterns)
    labels = {p["label"] for p in tp.list_command_patterns()}
    expected = {
        "cd_tf_apply", "cd_tf_destroy", "cd_tf_state",
        "cd_kubectl_write", "cd_helm_write",
        "cd_ansible_playbook", "cd_ansible_adhoc_write",
        "cd_ssh_remote",
        "cd_aws_write", "cd_aliyun_write", "cd_gcloud_write",
        "cd_sql_ddl", "cd_db_exec",
    }
    assert labels == expected, (
        f"unexpected label delta:\n"
        f"  missing: {expected - labels}\n"
        f"  extra:   {labels - expected}"
    )
