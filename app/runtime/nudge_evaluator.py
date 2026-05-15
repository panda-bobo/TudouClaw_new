"""Single-entry nudge evaluator — used by BOTH A (legacy chat loop)
and C (SDK adapter).

A nudge is "framework injects a corrective system message after the
agent's reply, before next iteration". Today the legacy chat loop has
4 separate nudge branches inline (stall / plan-pending / tool-error /
must-verify), each with its own condition + injected text. This
module collapses the conditions + texts into one ``evaluate()``
function returning a single ``Nudge | None``.

Why centralize:
  1. Both runtimes share the same nudge policy → no per-runtime drift
  2. Adding a 5th nudge requires editing ONE function, not 4 code paths
  3. Tests can lock the (input → which nudge fires) mapping
  4. The chat loop / SDK RunHooks just call evaluate() and act on
     the result — no detection logic in the loop body

Out of scope (kept in caller — needs Agent state):
  - Plan-pending nudge (needs self._current_plan)
  - Persisting agent reply into self.messages
  - The actual continue/break dispatch
  - _nudge_count cap

Caller responsibility:
  - Decide WHEN to call evaluate() (typically: after LLM responds,
    before next iteration)
  - Apply env-var overrides (TUDOU_VERIFY_NUDGE / TUDOU_NUDGE_WEAK_MODELS)
  - Persist reply + inject the returned nudge text as a user message
  - Bump _nudge_count
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from .intent import user_asked_for_verification
from .narrator import looks_like_narrator_stall
from .nudges import (
    agent_claimed_completion,
    agent_ran_verification_this_turn,
    detect_recent_tool_error,
)


NudgeKind = Literal[
    "narrator_stall",
    "tool_error_no_continuation",
    "must_verify",
]


@dataclass(frozen=True)
class Nudge:
    """A nudge decision. Caller injects ``text`` as a user-role
    message into the conversation, then re-runs the agent."""
    kind: NudgeKind
    text: str
    # Diagnostic — included in events / logs so admins can see WHY
    # the framework intervened.
    reason_detail: str = ""


def evaluate(
    *,
    user_text: str,
    agent_reply: str,
    messages: list[dict],
    has_tools: bool,
    iteration: int,
    max_iterations: int,
    nudge_count: int,
    max_nudges_per_turn: int,
    stop_reason: str = "",
) -> Optional[Nudge]:
    """Evaluate whether a nudge should fire AFTER the agent emitted
    ``agent_reply`` in iteration ``iteration``.

    Returns:
      Nudge if a corrective action is needed, else None.

    Order of checks matters: most specific (must_verify) first, then
    error-continuation, then the catch-all narrator-stall. First match
    wins; we never inject more than one nudge per evaluation.

    Universal gates (apply to ALL nudge kinds):
      - has_tools must be True (no tools = nothing for agent to do)
      - iteration < max_iterations - 1 (don't nudge on the last turn)
      - nudge_count < max_nudges_per_turn (avoid infinite nudge loops)
      - stop_reason not in {"length", "content_filter"} (those are
        provider-side terminations, not stalls)
    """
    # Universal gates
    if not has_tools:
        return None
    if iteration >= max_iterations - 1:
        return None
    if nudge_count >= max_nudges_per_turn:
        return None
    if stop_reason in ("length", "content_filter"):
        return None

    # ── 1. Must-verify (most specific) ──────────────────────────────
    # User asked for verification + agent claimed done + agent didn't
    # actually run a verify tool this turn → must-verify nudge.
    try:
        if (user_asked_for_verification(user_text)
                and agent_claimed_completion(agent_reply)
                and not agent_ran_verification_this_turn(messages)):
            return Nudge(
                kind="must_verify",
                text=(
                    "[system nudge] 用户要求里包含『验证』动作 "
                    "(validate / test / 检查 / 确认 / 跑通...), "
                    "但你这一轮**没真正跑过验证命令**就声明"
                    "『修复完成』。\n\n"
                    "不允许只声明完成。下一步必须做下面之一:\n"
                    "  (a) 立即调验证工具 (bash terraform "
                    "validate / npm test / pytest / 对应的 "
                    "lint / verify 命令), 拿到 0 errors 才能"
                    "声明完成,\n"
                    "  (b) 或在回复里**明确告诉用户**还有"
                    "什么没修好、为什么暂时没法跑验证 "
                    "(具体到模块/文件/行)。\n"
                    "禁止笼统'修复完成'/'已完成'而不出示"
                    "验证证据。"
                ),
                reason_detail=(
                    "user asked for verify + agent claimed done + "
                    "no verify call this turn"
                ),
            )
    except Exception:
        pass

    # ── 2. Tool-error continuation ──────────────────────────────────
    # Last tool result has an error marker AND agent didn't follow up
    # with another tool call → "fix or explain, don't just describe".
    try:
        last_err = detect_recent_tool_error(messages)
    except Exception:
        last_err = None

    if last_err:
        return Nudge(
            kind="tool_error_no_continuation",
            text=(
                "[system nudge] 上一个工具结果含错误:\n"
                f"  {last_err[:200]}\n\n"
                "你不能在这停下只输出文字描述错误。下一步必须做下面之一:\n"
                "  (a) 直接调工具修复 — read_file 看具体行 / "
                "edit_file 改 / bash 重试 / 调对应诊断工具,\n"
                "  (b) 或在回复里**明确告诉用户**'我无法继续因为 "
                "X' 并停止 (不是含糊带过)。\n"
                "禁止只描述错误而不动作。"
            ),
            reason_detail=f"last tool errored: {last_err[:80]}",
        )

    # ── 3. Narrator stall (broadest, last) ──────────────────────────
    # "Let me X:" / "让我看一下：" with no tool call → nudge to act.
    # Also covers "essentially empty" replies (< 20 chars) — most
    # common DeepSeek stall mode (thinking succeeded but output dropped).
    is_stall = looks_like_narrator_stall(agent_reply)
    is_empty = len((agent_reply or "").strip()) < 20
    if is_stall or is_empty:
        return Nudge(
            kind="narrator_stall",
            text=(
                "[system nudge] 你上一条消息以 \"让我…：\" / "
                "\"Let me …:\" 结尾，但没有调用任何工具。"
                "请立即调用相应工具完成你承诺的动作 —— "
                "不要重复宣告意图。Call the tool now; "
                "do not re-narrate."
            ),
            reason_detail="stall" if is_stall else "empty_reply",
        )

    return None
