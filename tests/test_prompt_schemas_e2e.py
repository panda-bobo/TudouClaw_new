"""End-to-end tests for app/core/prompt_schemas.py and its wiring.

Covers:
  1. ToolSchema round-trip is lossless on every live TOOL_DEFINITIONS
     entry (so re-routing tools[] through ToolSchema doesn't drop
     fields the LLM expects).
  2. tools[] payload and tool-validation error signatures are
     single-sourced — both pulls go through the same ToolSchema.
  3. AdminInstructionSchema preserves the legacy markdown unchanged
     (so the project.py admin block migration didn't change what the
     LLM sees).
  4. Field-level llm-visibility — to_llm_dict() strips internal /
     audit / server-only fields per the metadata={"llm": False} flag.
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


# ─── 1. ToolSchema round-trip lossless ─────────────────────────────


def test_tool_schema_round_trip_all_definitions():
    """Every entry in TOOL_DEFINITIONS must survive ToolSchema round-trip
    with name/description/required/properties intact (excluding
    underscore-prefixed server-side params)."""
    from app import tools as _tools
    from app.core.prompt_schemas import from_tool_definition

    defs = _tools.get_tool_definitions()
    assert len(defs) > 0, "expected at least one tool definition"

    for td in defs:
        in_fn = td.get("function", {})
        ts = from_tool_definition(td)
        out = ts.to_openai_payload()
        out_fn = out["function"]

        # Name and description must match
        assert out_fn["name"] == in_fn.get("name", ""), \
            f"name drift on {in_fn.get('name')}"

        # Required set must match
        in_req = set(in_fn.get("parameters", {}).get("required", []))
        out_req = set(out_fn.get("parameters", {}).get("required", []))
        assert in_req == out_req, \
            f"required drift on {in_fn.get('name')}: {in_req} vs {out_req}"

        # All visible (non-underscore) properties present in output
        in_props = in_fn.get("parameters", {}).get("properties", {})
        in_visible = {k for k in in_props.keys() if not k.startswith("_")}
        out_props = set(out_fn.get("parameters", {}).get("properties", {}).keys())
        assert in_visible == out_props, \
            f"property drift on {in_fn.get('name')}: in={in_visible} out={out_props}"


# ─── 2. Single source: tools[] and error signature ─────────────────


def test_tool_payload_and_error_signature_single_source():
    """The tool error signature path (middleware._format_schema_signature)
    and the tools[] payload path (ToolSchema.to_openai_payload) must pull
    from the same ToolSchema source — so a fix to one immediately reflects
    in the other."""
    from app.middleware import _format_schema_signature, _find_tool_schema
    from app.core.prompt_schemas import from_tool_definition

    schema = _find_tool_schema("read_file")
    assert schema, "read_file must be findable"

    # Error path
    err_sig = _format_schema_signature(schema)
    assert err_sig, "error signature non-empty"
    err_required = set()
    for line in err_sig.split("\n"):
        if "[REQUIRED]" in line:
            nm = line.strip().split(":")[0].strip()
            err_required.add(nm)

    # Payload path
    ts = from_tool_definition(schema)
    payload = ts.to_openai_payload()
    payload_required = set(
        payload["function"]["parameters"].get("required", []))

    assert err_required == payload_required, \
        f"single-source consistency broken: err={err_required} " \
        f"payload={payload_required}"


# ─── 3. AdminInstructionSchema preserves legacy markdown ───────────


def test_admin_schema_passthrough():
    """AdminInstructionSchema is a thin wrapper — to_llm_markdown() must
    return the input markdown_fallback verbatim so the project.py admin
    block migration is a no-op for the LLM."""
    from app.core.prompt_schemas import from_admin_block, render_block

    sample = (
        "\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "⚠️ ADMIN 指令(最高优先级,必须遵守)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "  [09:30:12] @pm-小明 开始 M3\n"
        "规则:\n1. 暂停 → 立即停\n"
    )
    schema = from_admin_block(sample, pause_active=False)
    assert schema.has_pending is True
    assert schema.pause_active is False
    assert schema.markdown_fallback == sample
    assert render_block(schema) == sample, \
        "schema must passthrough legacy markdown unchanged"

    # Empty input → empty output
    empty = from_admin_block("")
    assert empty.has_pending is False
    assert render_block(empty) == ""

    # pause_active flag propagates
    paused = from_admin_block(sample, pause_active=True)
    assert paused.pause_active is True


# ─── 4. Field-level LLM visibility audit ───────────────────────────


def test_skill_schema_strips_internal_fields():
    """SkillSchema's to_llm_dict() must NOT emit version / install_dir /
    granted_at / granted_by / risk_level / manifest_raw — those are
    server-side / audit fields explicitly tagged metadata={"llm": False}."""
    from app.core.prompt_schemas import SkillSchema

    sk = SkillSchema(
        name="x", id="abc", path="/p", description="d",
        version="9.9.9", install_dir="/internal/path",
        granted_at=12345.0, granted_by="admin",
        risk_level="dangerous",
        manifest_raw={"secret": "value"},
    )
    d = sk.to_llm_dict()
    # llm-visible fields ARE present
    assert "name" in d and "id" in d and "path" in d
    # internal fields are NOT
    for forbidden in ("version", "install_dir", "granted_at",
                       "granted_by", "risk_level", "manifest_raw"):
        assert forbidden not in d, \
            f"{forbidden} leaked to LLM dict"


def test_tool_schema_strips_internal_fields():
    from app.core.prompt_schemas import ToolSchema, ParamSpec

    ts = ToolSchema(
        name="t", description="d",
        params=[ParamSpec(name="x", type="string", required=True)],
        aliases=["t_alias"], handler_name="_internal_handler",
        risk_level="risky", category="data", audit_tags=["audit"],
    )
    d = ts.to_llm_dict()
    assert "name" in d and "description" in d and "params" in d
    for forbidden in ("aliases", "handler_name", "risk_level",
                       "category", "audit_tags"):
        assert forbidden not in d, f"{forbidden} leaked"


def test_rule_schema_strips_internal_fields():
    from app.core.prompt_schemas import RuleSchema

    r = RuleSchema(
        id="r1", name="rule", description="desc",
        trigger="before_tool_call", action="deny", message="m",
        condition_summary="summary",
        full_condition={"deeply": "nested"},
        priority=10, source="migrator:foo",
        enabled=True, created_by="admin",
    )
    d = r.to_llm_dict()
    assert "id" in d and "name" in d and "description" in d
    for forbidden in ("full_condition", "priority", "source",
                       "enabled", "created_by"):
        assert forbidden not in d, f"{forbidden} leaked"


# ─── 5. PlanStateSchema renders structurally ───────────────────────


def test_plan_state_schema_renders_with_steps():
    from app.core.prompt_schemas import PlanStateSchema, PlanStepSchema, render_block

    ps = PlanStateSchema(
        task_summary="ship widget v2",
        current_steps=[
            PlanStepSchema(order=2, title="implement", status="in_progress",
                            acceptance="all tests green")],
        done_steps=[
            PlanStepSchema(order=1, title="design", status="completed",
                            result_summary="3 sketches approved")],
        pending_steps=[
            PlanStepSchema(order=3, title="release", status="pending",
                            blocked_by=[2])],
    )
    md = render_block(ps)
    assert "<plan_state>" in md
    assert "ship widget v2" in md
    assert "implement" in md
    assert "all tests green" in md
    assert "design" in md
    assert "3 sketches approved" in md
    assert "release" in md
    assert "blocked_by=[2]" in md


def test_plan_state_schema_empty_returns_empty_string():
    from app.core.prompt_schemas import PlanStateSchema, render_block

    ps = PlanStateSchema()
    assert ps.is_empty()
    assert render_block(ps) == ""


# ─── 6. WorkspaceFilesSchema renders + flags ───────────────────────


def test_workspace_files_schema_renders_with_state():
    """WorkspaceFilesSchema surfaces project shared dir state — design
    doc / plan md presence + top-level entries — so the LLM doesn't
    need to glob to discover state.
    """
    from app.core.prompt_schemas import WorkspaceFilesSchema, render_block

    ws = WorkspaceFilesSchema(
        workspace_root="/Users/me/.tudou_claw/workspaces/shared/abc",
        has_design_doc=True,
        has_plan_md=False,
        top_level_entries=["docs/", "src/", "tests/", "README.md"],
    )
    md = render_block(ws)
    assert "## Workspace State" in md
    assert "/Users/me/.tudou_claw/workspaces/shared/abc" in md
    assert "✓ has_design_doc" in md
    assert "✗ has_plan_md" in md
    assert "docs/" in md
    assert "README.md" in md


def test_workspace_files_schema_empty_returns_empty_string():
    """No workspace_root (solo agent) → render returns empty so the
    block is silently skipped from system_prompt."""
    from app.core.prompt_schemas import WorkspaceFilesSchema, render_block

    ws = WorkspaceFilesSchema()
    assert ws.is_empty()
    assert render_block(ws) == ""


def test_workspace_files_schema_caps_top_level_entries():
    """Top-level entries should cap at 30 in the rendered output to
    keep prompt budget under control."""
    from app.core.prompt_schemas import WorkspaceFilesSchema, render_block

    ws = WorkspaceFilesSchema(
        workspace_root="/x",
        top_level_entries=[f"file_{i}" for i in range(50)],
    )
    md = render_block(ws)
    # All first 30 are present
    assert "file_0" in md and "file_29" in md
    # Cap notice present
    assert "+20 more" in md


# ─── 7. Rate-limit loopback bypass ─────────────────────────────────


def test_ratelimit_loopback_bypass():
    """Same-machine traffic (127.0.0.1 / ::1 / 127.x / ::ffff:127.x)
    skips the rate limiter entirely. LAN clients still get throttled."""
    from app.api.middleware.ratelimit import RateLimitMiddleware

    rl = RateLimitMiddleware(app=None)
    # Loopback IPs
    assert rl._is_loopback("127.0.0.1") is True
    assert rl._is_loopback("::1") is True
    assert rl._is_loopback("localhost") is True
    assert rl._is_loopback("127.0.0.5") is True  # any 127.0.0.0/8
    assert rl._is_loopback("::ffff:127.0.0.1") is True  # IPv4-mapped IPv6
    assert rl._is_loopback("0.0.0.0") is True
    # Non-loopback (LAN / WAN)
    assert rl._is_loopback("192.168.1.1") is False
    assert rl._is_loopback("10.0.0.5") is False
    assert rl._is_loopback("8.8.8.8") is False
    assert rl._is_loopback("2001:db8::1") is False
    assert rl._is_loopback("") is False
    assert rl._is_loopback("unknown") is False
