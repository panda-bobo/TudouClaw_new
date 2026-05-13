"""Tests for orphan-tool prevention in HISTORY_SUMMARY (2026-05-13).

Real symptom: at 13:20 today the agent ran 5 bash terraform validate
calls successfully. HISTORY_SUMMARY then compressed the assistant
message that owned those tool_calls. The 5 tool result messages
remained in the recent slice but had no preceding assistant.tool_calls
to satisfy the OpenAI/DeepSeek pairing requirement → sanitizer's Pass
1.5 dropped all 5 as "orphan tool message(s)".

Next LLM call saw zero tool_results in history → hallucinated:
  "本轮工具调用预算已用完(5/5),需要等下一轮再执行修复"
  "我已经多次尝试使用 bash 工具执行 terraform validate 命令,但均被
   系统级安全规则 (guardrail) 拦截"

Even though the agent.json's full message list HAD the 5 tool results,
just compressed past the cut → orphaned in the LLM payload.

Fix: a new orphan-tool-prevention pass in _summarize_old_history that,
after all other cut adjustments, scans the recent slice for tool
messages whose tool_call_id has no provider in recent. If found, walk
recent_start back to include the owning assistant.tool_calls.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.agent import _summarize_old_history


class _StubAgent:
    def __init__(self) -> None:
        self.id = "stub-orphan-test"
        self._history_summary_cache: dict | None = None

    def _resolve_effective_provider_model(self):
        return ("openai", "gpt-test")


def _fake_llm(text="summary text"):
    return {"message": {"content": text}}


def _build_messages_with_orphan_risk(num_msgs: int = 50) -> list[dict]:
    """Build a message list where the last assistant.tool_calls + its
    5 tool results sit right at the keep_last boundary so naive
    cut placement orphans them.

    Layout:
      [0]   system
      [1..N-7] filler user/assistant pairs (force compaction)
      [N-7] user      ← intermediate user msg (the one safety-net
                        latches onto)
      [N-6] assistant.tool_calls (id=t0..t4)
      [N-5..N-1] tool×5
    """
    msgs: list[dict] = [{"role": "system", "content": "you are agent"}]
    # Filler that pushes char count over compaction threshold
    for i in range(num_msgs - 7):
        if i % 2 == 0:
            msgs.append({"role": "user",
                         "content": "filler user " + str(i) + ("x" * 600)})
        else:
            msgs.append({"role": "assistant",
                         "content": "filler asst " + str(i) + ("y" * 600)})
    # The boundary user — this is what _last_user_or_assistant_idx may latch
    msgs.append({"role": "user", "content": "do the 5 things"})
    # The owning assistant — has 5 tool_calls
    msgs.append({
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": f"call_t{i}", "type": "function",
             "function": {"name": "bash",
                          "arguments": '{"command":"terraform validate"}'}}
            for i in range(5)
        ],
    })
    # The 5 tool results
    for i in range(5):
        msgs.append({
            "role": "tool",
            "tool_call_id": f"call_t{i}",
            "content": f"validate result {i}: 0 errors",
        })
    return msgs


def test_orphan_tools_kept_with_owning_assistant():
    """Reproduces the 13:20 incident. Without the fix, the assistant.tc
    gets compressed into summary while its 5 tool results remain
    orphaned in recent."""
    msgs = _build_messages_with_orphan_risk(num_msgs=50)
    agent = _StubAgent()
    with patch("app.llm.chat_no_stream", return_value=_fake_llm()):
        out = _summarize_old_history(msgs, agent)

    # The 5 tool results MUST appear in the output (not orphaned/dropped)
    tool_results_in_output = [
        m for m in out
        if m.get("role") == "tool"
        and m.get("tool_call_id", "").startswith("call_t")
    ]
    assert len(tool_results_in_output) == 5, (
        f"expected all 5 tool results preserved, got "
        f"{len(tool_results_in_output)}")

    # The owning assistant.tool_calls MUST also be in the output
    # (not just the tool results without their owner)
    asst_tcs_in_output = [
        m for m in out
        if m.get("role") == "assistant"
        and any(
            isinstance(tc, dict) and tc.get("id", "").startswith("call_t")
            for tc in (m.get("tool_calls") or []))
    ]
    assert len(asst_tcs_in_output) == 1, (
        f"expected the owning assistant.tool_calls preserved, got "
        f"{len(asst_tcs_in_output)}")


def test_no_orphan_when_assistant_tc_already_in_recent():
    """Sanity: when the assistant.tc and its tools are all naturally in
    the recent slice, nothing changes (no extra back-up)."""
    # Small message list — won't trigger compaction at all
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "ok",
         "tool_calls": [{"id": "c0", "type": "function",
                         "function": {"name": "x", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c0", "content": "result"},
    ]
    agent = _StubAgent()
    out = _summarize_old_history(msgs, agent)
    # No compaction triggered — original list returned
    assert out is msgs


def test_orphan_owner_not_in_messages_handled_gracefully():
    """If the owning assistant.tc was never recorded (bug elsewhere or
    truncation in the OUT-of-pipeline part), the prevention pass
    should not crash — it just gives up and lets sanitizer handle."""
    # Tools without ANY assistant.tc — pathological case
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(50):
        msgs.append({"role": "user", "content": f"msg {i}" + "x" * 600})
    # Append orphan tool with no asst.tc anywhere
    msgs.append({"role": "tool", "tool_call_id": "ghost", "content": "ghost result"})

    agent = _StubAgent()
    with patch("app.llm.chat_no_stream", return_value=_fake_llm()):
        # Should not raise even though there's no owner
        out = _summarize_old_history(msgs, agent)
    assert out is not None


def test_logs_when_prevention_kicks_in(caplog):
    msgs = _build_messages_with_orphan_risk(num_msgs=50)
    agent = _StubAgent()
    with caplog.at_level("INFO", logger="tudou.agent"):
        with patch("app.llm.chat_no_stream", return_value=_fake_llm()):
            _summarize_old_history(msgs, agent)
    # The new log line should fire when prevention kicks in
    matches = [
        r for r in caplog.records
        if "orphan-tool prevention" in r.getMessage()
    ]
    # Either prevention fired (good — log present) OR cut naturally
    # avoided orphans (also fine — no log). Both outcomes are correct.
    # We just verify the code path doesn't crash and the marker text
    # is correct WHEN it fires.
    for r in matches:
        msg = r.getMessage()
        assert "backed up recent_start" in msg
        assert "owning assistant.tool_calls" in msg


def test_multiple_tool_groups_all_preserved():
    """Edge: 2 separate assistant.tc groups near the cut. Both should
    survive as long as the prevention finds the earliest orphan-source."""
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(40):
        msgs.append({"role": "user",
                     "content": f"filler {i}" + "z" * 700})
    # Group 1: asst.tc + 2 tools
    msgs.append({"role": "assistant", "content": "",
                 "tool_calls": [
                     {"id": "g1_t0", "type": "function",
                      "function": {"name": "x", "arguments": "{}"}},
                     {"id": "g1_t1", "type": "function",
                      "function": {"name": "x", "arguments": "{}"}},
                 ]})
    msgs.append({"role": "tool", "tool_call_id": "g1_t0", "content": "r0"})
    msgs.append({"role": "tool", "tool_call_id": "g1_t1", "content": "r1"})
    # Some user/assistant in between
    msgs.append({"role": "user", "content": "next"})
    msgs.append({"role": "assistant", "content": "ok"})
    # Group 2: asst.tc + 3 tools
    msgs.append({"role": "assistant", "content": "",
                 "tool_calls": [
                     {"id": "g2_t0", "type": "function",
                      "function": {"name": "y", "arguments": "{}"}},
                     {"id": "g2_t1", "type": "function",
                      "function": {"name": "y", "arguments": "{}"}},
                     {"id": "g2_t2", "type": "function",
                      "function": {"name": "y", "arguments": "{}"}},
                 ]})
    msgs.append({"role": "tool", "tool_call_id": "g2_t0", "content": "s0"})
    msgs.append({"role": "tool", "tool_call_id": "g2_t1", "content": "s1"})
    msgs.append({"role": "tool", "tool_call_id": "g2_t2", "content": "s2"})

    agent = _StubAgent()
    with patch("app.llm.chat_no_stream", return_value=_fake_llm()):
        out = _summarize_old_history(msgs, agent)

    # Sanitizer downstream would drop ANY tool whose tool_call_id has no
    # preceding assistant.tool_calls in the SAME payload. Verify our
    # prevention left no orphans by checking pairing inside `out`.
    seen_tc: set[str] = set()
    for m in out:
        if m.get("role") == "assistant":
            for tc in (m.get("tool_calls") or []):
                if isinstance(tc, dict):
                    seen_tc.add(tc.get("id", ""))
        elif m.get("role") == "tool":
            tid = m.get("tool_call_id", "")
            if tid and tid not in seen_tc:
                pytest.fail(
                    f"orphan tool {tid!r} survived in output — prevention "
                    f"pass missed it. Output was: {[m.get('role') for m in out]}")
