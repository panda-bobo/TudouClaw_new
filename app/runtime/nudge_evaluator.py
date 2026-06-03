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
    "task_continuation",
]


# Phrases that signal the agent is LEGITIMATELY asking the user for
# input — in which case task_continuation must NOT fire (the agent is
# correctly stopping to wait, not narrate-and-stopping). Checked
# against the tail of the reply (questions usually come at the end).
_USER_QUESTION_MARKERS = (
    "请问", "需要你", "你想", "要不要", "是否需要", "请确认",
    "请提供", "请告诉我", "等你", "你希望", "你倾向", "请选择",
    "需要我", "你看", "可以吗", "好吗", "如何", "怎么处理",
    "which would you", "do you want", "should i", "would you like",
    "please confirm", "please provide", "let me know", "?",
    "？",
)


def _reply_asks_user_question(reply: str) -> bool:
    """Heuristic: does the reply end by asking the user something?
    If so, the agent is correctly waiting for input — don't fire
    task_continuation. Look at the last ~200 chars (questions land
    at the end of a reply)."""
    if not reply:
        return False
    tail = reply.strip()[-200:].lower()
    return any(m.lower() in tail for m in _USER_QUESTION_MARKERS)


# Phrases in the USER's latest message that signal explicit intent to
# PAUSE / DROP / SUPERSEDE existing open work. When detected, the
# task_continuation nudge must NOT fire — even though open work
# technically remains (tasks/plan steps that the model didn't get a
# chance to mark as paused). Without this guard, the nudge pushes the
# agent right back into the old work the user just told it to stop.
# @user 2026-06-03: "我说历史任务先挂起,先做新的任务...但他又跑回去
# 做历史任务." The nudge ignored the user's directive because tasks
# remained TODO/IN_PROGRESS — small models often forget to mark them.
_USER_PAUSE_INTENT_MARKERS = (
    # Chinese
    "挂起", "暂停", "先别做", "别做了", "停一下", "停下来",
    "先做新的", "先做这个", "先做另", "做新的", "改做",
    "丢掉", "不管", "搁置", "放下",
    "忘了", "忘记", "无视", "忽略", "跳过",
    # English
    "switch to", "stop the", "pause", "drop the",
    "forget the", "ignore the", "skip the",
    "new task", "instead",
)


def _user_signaled_pause(user_text: str) -> bool:
    """Did the user explicitly tell the agent to pause / drop / pivot
    away from the currently-open work? If yes, task_continuation must
    not fire this turn — the model hasn't yet marked the old tasks as
    paused (small models often forget to), so the nudge would push it
    right back into the work the user just told it to stop."""
    if not user_text:
        return False
    text = user_text.lower()
    return any(m.lower() in text for m in _USER_PAUSE_INTENT_MARKERS)


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
    # 2026-05-16: per-kind kill switches (defaults match the env vars
    # the legacy chat loop checks: TUDOU_NUDGE_WEAK_MODELS /
    # TUDOU_TOOL_ERROR_NUDGE / TUDOU_VERIFY_NUDGE). Caller supplies
    # the env-var values; evaluate_nudge stays env-free + pure.
    enable_narrator: bool = True,
    enable_tool_error: bool = True,
    enable_must_verify: bool = True,
    # ── Task-continuation (2026-05-28, continuity fix A) ──
    # The caller computes how much work is still open (open plan
    # steps + open agent.tasks) and passes a one-line summary. When
    # non-empty AND the agent's reply isn't a question to the user,
    # we fire a task_continuation nudge so the agent keeps working
    # through the whole task in the same turn (Claude-Code-style)
    # instead of stopping after one sub-task. evaluate() stays pure —
    # caller owns the agent-state lookup.
    open_work_summary: str = "",
    enable_task_continuation: bool = True,
    continuation_count: int = 0,
    max_continuations: int = 25,
) -> Optional[Nudge]:
    """Evaluate whether a nudge should fire AFTER the agent emitted
    ``agent_reply`` in iteration ``iteration``.

    Returns:
      Nudge if a corrective action is needed, else None.

    Order of checks (matches the legacy A chat loop's ordering for
    byte-identical behavior on the runtime mode toggle):
      1. narrator_stall (broadest — catches stall pattern OR
         essentially-empty reply)
      2. tool_error_no_continuation (last tool errored + no follow-up)
      3. must_verify (user asked verify + agent claimed done + no
         verify call this turn)
    First match wins; we never inject more than one nudge per
    evaluation.

    Universal gates (apply to ALL nudge kinds):
      - has_tools must be True (no tools = nothing for agent to do)
      - iteration < max_iterations - 1 (don't nudge on the last turn)
      - nudge_count < max_nudges_per_turn (avoid infinite nudge loops)
      - stop_reason not in {"length", "content_filter"} (those are
        provider-side terminations, not stalls)

    Per-kind kill switches (enable_narrator / enable_tool_error /
    enable_must_verify) let callers disable specific nudge kinds
    via env vars without short-circuiting the entire evaluator.
    """
    # Universal gates (apply to ALL nudge kinds)
    if not has_tools:
        return None
    if stop_reason in ("length", "content_filter"):
        return None

    # Corrective-nudge budget gate (narrator / tool_error / must_verify).
    # task_continuation has its OWN budget (max_continuations) checked
    # below — it's a different concern ("keep working" vs "you went
    # wrong, retry"), so a multi-step task isn't capped at 3.
    _corrective_ok = (
        iteration < max_iterations - 1
        and nudge_count < max_nudges_per_turn
    )

    # ── 1. Narrator stall (broadest — checked first to match
    #       legacy chat loop ordering) ────────────────────────────
    # "Let me X:" / "让我看一下：" with no tool call → nudge to act.
    # Also covers "essentially empty" replies (< 20 chars) — most
    # common DeepSeek stall mode (thinking succeeded but output
    # dropped).
    if enable_narrator and _corrective_ok:
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

    # ── 2. Tool-error continuation ──────────────────────────────────
    # Last tool result has an error marker AND agent didn't follow up
    # with another tool call → "fix or explain, don't just describe".
    if enable_tool_error and _corrective_ok:
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

    # ── 3. Must-verify ──────────────────────────────────────────────
    # User asked for verification + agent claimed done + agent didn't
    # actually run a verify tool this turn → must-verify nudge.
    if enable_must_verify and _corrective_ok:
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

    # ── 4. Task continuation (2026-05-28, continuity fix A) ─────────
    # Lowest priority — only fires when nothing more specific matched.
    # The agent produced a clean-looking reply (no stall pattern, no
    # tool error, no unverified claim), BUT there's still open work
    # (plan steps / tasks). Weak models (mimo/deepseek) tend to finish
    # ONE sub-task with a tidy "✓ done, starting next" message then
    # STOP — this catches that and re-prompts to keep going in the
    # SAME turn, approximating Claude Code's autonomous task drive.
    #
    # Guard: don't fire if the agent is legitimately asking the user
    # a question (then it's correctly waiting for input, not stalling).
    if (enable_task_continuation and open_work_summary
            and continuation_count < max_continuations):
        # Guard 1: agent is asking the user a question (legit stop).
        # Guard 2 (2026-06-03): user EXPLICITLY signaled pause/drop —
        # never push the agent back into work the user just told it to
        # stop. The model should have marked the old tasks as paused
        # via task_update; if it forgot (small models often do), this
        # is the safety net so the framework doesn't override the user.
        if (not _reply_asks_user_question(agent_reply)
                and not _user_signaled_pause(user_text)):
            return Nudge(
                kind="task_continuation",
                text=(
                    "[system nudge] 你完成了一步,但任务还没全做完。"
                    "当前还有未完成的工作:\n"
                    f"{open_work_summary}\n\n"
                    "请**立即继续执行下一个未完成的步骤/任务**,"
                    "不要停下来等用户确认 —— 直接调用相应工具开始下一步。"
                    "只有在以下情况才停: (1) 所有步骤/任务全部完成, "
                    "(2) 你确实需要用户提供信息/做决策才能继续(这时"
                    "明确说出你需要什么)。现在请继续。"
                ),
                reason_detail=f"open work remains: {open_work_summary[:120]}",
            )

    return None
