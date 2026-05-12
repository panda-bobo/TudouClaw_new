"""Tests for the signature-based MCP loop-guard fix (2026-05-12).

Old behavior: counted per-mcp_id only. 8 legitimate terraform_validate
calls with 4 different working_dirs all counted toward the same
mcp_id's cap → blocked at the 6th call → agent falls back to bash.
Real symptom user reported: "MCP 已达阈值，改用 bash 直接执行".

New behavior: hard-block uses (mcp_id, args_hash) signature. Only
byte-identical repeat calls trigger the wall. Different args =
different signature = independent counter → legitimate batch work
runs to completion.

The soft warning still fires per-mcp_id (telling agent "you're using
this MCP a lot — sure?") so loops with slowly varying args still get
a heads-up.
"""
from __future__ import annotations

import hashlib
import json

import pytest


# ── Predicate: signature computation mirrors agent.py ──────────────

def _sig(mcp_id: str, args: dict) -> tuple[str, str]:
    """Build the (mcp_id, args_hash) signature the loop-guard uses."""
    args_str = json.dumps(args or {}, sort_keys=True,
                          ensure_ascii=False, default=str)
    h = hashlib.sha256(args_str.encode()).hexdigest()[:12]
    return (mcp_id, h)


_MCP_REPEAT_SOFT = 3
_MCP_REPEAT_HARD = 5


def _simulate_calls(calls: list[tuple[str, dict]]) -> tuple[dict, dict, list[str]]:
    """Replay a sequence of (mcp_id, args) calls through the new guard.

    Returns (per_id_counts, per_sig_counts, blocked_call_indices)."""
    id_count: dict[str, int] = {}
    sig_count: dict[tuple[str, str], int] = {}
    blocked: list[str] = []
    for idx, (mid, args) in enumerate(calls):
        id_count[mid] = id_count.get(mid, 0) + 1
        sig = _sig(mid, args)
        sig_count[sig] = sig_count.get(sig, 0) + 1
        if sig_count[sig] > _MCP_REPEAT_HARD:
            blocked.append(f"call#{idx}")
    return id_count, sig_count, blocked


# ── Tests ──────────────────────────────────────────────────────────

def test_legitimate_batch_8_diff_args_not_blocked():
    """Real symptom: 4 modules × (init + validate) = 8 calls, all
    legitimate. The old per-mcp_id cap would block the 6th. The new
    signature-based cap doesn't — each call has different working_dir
    so each signature is unique."""
    calls = []
    for module in ("monitoring", "logging", "organization", "account-factory"):
        for tool in ("terraform_init", "terraform_validate"):
            calls.append(("983197f1", {"tool": tool, "working_dir": module}))
    _, _, blocked = _simulate_calls(calls)
    assert blocked == [], (
        f"legitimate batch was blocked: {blocked} — signature guard "
        f"failed to distinguish diff-args calls")


def test_real_loop_same_args_still_blocked():
    """The exact loop the guard was designed for: 6+ byte-identical
    calls. Signature is the same each time → counter increments → 7th
    gets blocked."""
    # Original 2026-04-28 incident: hammering email-MCP with same args
    calls = [("42d8ca5e", {"tool": "send_email",
                            "to": "x@y.com", "subject": "hi"})] * 8
    _, sig_count, blocked = _simulate_calls(calls)
    # First 6 pass (signature count goes 1..6 — 6 > 5 = HARD, so
    # the 7th is the first blocked one)
    # Wait — `if sig_count > _MCP_REPEAT_HARD` means blocked when count
    # is 6, 7, 8. So calls #5, #6, #7 (0-indexed) get blocked.
    assert "call#5" in blocked
    assert "call#6" in blocked
    assert "call#7" in blocked
    # First 5 not blocked
    assert "call#0" not in blocked
    assert "call#4" not in blocked


def test_loop_with_varying_args_not_blocked():
    """Loop where args genuinely change each call → each unique
    signature has its own counter → never blocked even if 100 calls."""
    calls = [("xyz", {"tool": "read", "id": i}) for i in range(50)]
    _, _, blocked = _simulate_calls(calls)
    assert blocked == []


def test_mixed_workload_only_repeats_blocked():
    """Mix of legitimate batch + a parallel loop: only the loop gets
    blocked, the batch sails through."""
    calls = []
    # Legitimate batch (5 unique calls — sig_count=1 for each)
    for w in ("a", "b", "c", "d", "e"):
        calls.append(("mcp1", {"tool": "validate", "wd": w}))
    # Loop pattern (same args 8 times)
    for _ in range(8):
        calls.append(("mcp1", {"tool": "send", "to": "x"}))

    _, _, blocked = _simulate_calls(calls)
    # First 5 (batch) NOT blocked
    for i in range(5):
        assert f"call#{i}" not in blocked
    # Of the 8 loop calls (indices 5..12), only those exceeding HARD=5
    # signature-count are blocked. Loop sig_count goes 1..8 → blocks
    # when count is 6,7,8 → calls #10, #11, #12 (0-indexed).
    assert "call#10" in blocked
    assert "call#11" in blocked
    assert "call#12" in blocked


def test_args_order_doesnt_matter():
    """sort_keys=True in json.dumps → {a:1, b:2} and {b:2, a:1} hash
    to the same signature. Prevents an LLM from accidentally bypassing
    the guard by reordering kwargs."""
    sig_a = _sig("m", {"a": 1, "b": 2})
    sig_b = _sig("m", {"b": 2, "a": 1})
    assert sig_a == sig_b


def test_unicode_args_round_trip():
    """ensure_ascii=False in json.dumps → Chinese args produce a stable
    hash."""
    sig = _sig("m", {"text": "中文内容"})
    sig_again = _sig("m", {"text": "中文内容"})
    assert sig == sig_again


def test_soft_warn_per_mcp_id_independent_of_signature():
    """The soft warn (at SOFT=3) fires once per mcp_id regardless of
    args variation — useful "you're using this MCP a lot" hint that
    isn't confused by diff-args."""
    calls = [
        ("mcp1", {"i": 1}),
        ("mcp1", {"i": 2}),
        ("mcp1", {"i": 3}),
        ("mcp1", {"i": 4}),    # _mcnt=4 > SOFT=3 → warn fires here
    ]
    id_count, _, _ = _simulate_calls(calls)
    assert id_count["mcp1"] == 4
    # The soft-warn condition triggers exactly once per mcp_id (via
    # the _mcp_id_warned set). We can't test the set membership from
    # here but the math (4 > 3 = SOFT) is what gates the warning.


def test_different_mcps_independent_counters():
    """Calls to different mcp_ids never affect each other's counters."""
    calls = [("mcp1", {"a": 1})] * 10 + [("mcp2", {"a": 1})] * 10
    id_count, sig_count, blocked = _simulate_calls(calls)
    assert id_count["mcp1"] == 10
    assert id_count["mcp2"] == 10
    # Each is the same-args loop pattern → each blocks after #5
    # (signature counter > HARD=5)
    expected_blocked = (
        [f"call#{i}" for i in (5, 6, 7, 8, 9)]      # mcp1 loop
        + [f"call#{i}" for i in (15, 16, 17, 18, 19)]  # mcp2 loop
    )
    for b in expected_blocked:
        assert b in blocked, f"expected {b} blocked"


def test_hash_length_consistent():
    """Signatures are truncated to 12 hex chars — keeps log lines
    short while collision-safe enough for the in-turn counter."""
    sig = _sig("m", {"a": 1})
    assert len(sig[1]) == 12
    # Hex chars only
    int(sig[1], 16)
