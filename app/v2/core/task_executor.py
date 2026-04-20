"""
TaskExecutor — Execute-phase LLM+tool loop (PRD §6.3).

THIS is the only V2 module that talks to the LLM. All tool dispatch
(skills + MCPs) flows through the L1 bridges so V2 never imports V1's
agent runtime.

Step exit semantics (5 check kinds, PRD §8.3 / §6.1):
    tool_used         — a specific tool name was called during this step
    artifact_created  — a new artifact of ``kind`` was added during this step
    contains_section  — last assistant text contains a markdown section header
    regex             — last assistant text matches pattern (optionally N times)
    json_schema       — last assistant text parses as JSON that validates

If a step's LLM stops calling tools but the exit isn't met, we inject a
nudge system message and keep driving (fixes V1 root cause: "intent
without action").
"""
from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING

from .task import Task, PlanStep, TaskPhase, Artifact

if TYPE_CHECKING:
    from ..agent.agent_v2 import AgentV2
    from .task_events import TaskEventBus


# ── exit_check evaluators (pure functions, no side effects) ────────────

def _check_tool_used(
    spec: dict,
    tool_names_called: set[str],
    artifacts_before: int,
    artifacts_after: int,
    last_assistant_text: str,
    task: Task,
) -> bool:
    wanted = spec.get("tool")
    if not wanted:
        return False
    return wanted in tool_names_called


def _check_artifact_created(
    spec: dict,
    tool_names_called: set[str],
    artifacts_before: int,
    artifacts_after: int,
    last_assistant_text: str,
    task: Task,
) -> bool:
    kind = spec.get("kind")
    min_count = int(spec.get("min_count", 1))
    if kind:
        new = [
            a for a in task.artifacts[artifacts_before:artifacts_after]
            if a.kind == kind
        ]
        return len(new) >= min_count
    # no kind filter → just count the delta
    return (artifacts_after - artifacts_before) >= min_count


_SECTION_CACHE: dict[str, re.Pattern] = {}


def _check_contains_section(
    spec: dict,
    tool_names_called: set[str],
    artifacts_before: int,
    artifacts_after: int,
    last_assistant_text: str,
    task: Task,
) -> bool:
    section = spec.get("section", "")
    if not section or not last_assistant_text:
        return False
    # Section may be "## Summary" or just "Summary". Build a flexible match.
    key = section.strip()
    pat = _SECTION_CACHE.get(key)
    if pat is None:
        escaped = re.escape(key.lstrip("#").strip())
        pat = re.compile(
            rf"^#{{1,6}}\s*{escaped}\s*$",
            re.MULTILINE,
        )
        _SECTION_CACHE[key] = pat
    return bool(pat.search(last_assistant_text))


_REGEX_CACHE: dict[str, re.Pattern] = {}


def _check_regex(
    spec: dict,
    tool_names_called: set[str],
    artifacts_before: int,
    artifacts_after: int,
    last_assistant_text: str,
    task: Task,
) -> bool:
    pattern = spec.get("pattern")
    if not pattern or not last_assistant_text:
        return False
    min_matches = int(spec.get("min_matches", 1))
    key = pattern + "\x00" + str(spec.get("flags", ""))
    pat = _REGEX_CACHE.get(key)
    if pat is None:
        try:
            flags = 0
            if "i" in (spec.get("flags") or "").lower():
                flags |= re.IGNORECASE
            if "m" in (spec.get("flags") or "").lower():
                flags |= re.MULTILINE
            pat = re.compile(pattern, flags)
            _REGEX_CACHE[key] = pat
        except re.error:
            return False
    return len(pat.findall(last_assistant_text)) >= min_matches


def _check_json_schema(
    spec: dict,
    tool_names_called: set[str],
    artifacts_before: int,
    artifacts_after: int,
    last_assistant_text: str,
    task: Task,
) -> bool:
    """Parse last assistant text as JSON, validate against schema.

    If the ``jsonschema`` package isn't available, fall back to a
    required-keys check (``spec.required = [...]``).
    """
    if not last_assistant_text:
        return False
    schema = spec.get("schema")
    # Try fenced block first, then bare object.
    payload = _try_parse_json(last_assistant_text)
    if payload is None:
        return False
    if schema is None:
        # Back-compat form: {"required": ["k1","k2"]}
        required = spec.get("required") or []
        if not isinstance(payload, dict):
            return False
        return all(k in payload for k in required)
    try:
        import jsonschema  # type: ignore
        jsonschema.validate(payload, schema)
        return True
    except ImportError:
        # Degraded mode: check top-level required keys only.
        required = (schema.get("required") or []) if isinstance(schema, dict) else []
        if not isinstance(payload, dict):
            return False
        return all(k in payload for k in required)
    except Exception:
        return False


_CHECK_DISPATCH = {
    "tool_used":        _check_tool_used,
    "artifact_created": _check_artifact_created,
    "contains_section": _check_contains_section,
    "regex":            _check_regex,
    "json_schema":      _check_json_schema,
}


def _try_parse_json(text: str):
    """Best-effort JSON extraction from LLM text."""
    m = re.search(r"```(?:json)?\s*([\{\[][\s\S]*?[\}\]])\s*```", text)
    candidate = m.group(1) if m else None
    if candidate is None:
        lo = text.find("{"); hi = text.rfind("}")
        if 0 <= lo < hi:
            candidate = text[lo:hi + 1]
    if candidate is None:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


# ── TaskExecutor ───────────────────────────────────────────────────────

class TaskExecutor:
    """Drives a single PlanStep to completion via LLM + tool calls."""

    MAX_TOOL_TURNS_PER_STEP = 12
    CONTEXT_MESSAGES_SOFT_LIMIT = 120
    CONTEXT_MESSAGES_HARD_CUT   = 200   # hard keep-tail count

    def __init__(
        self,
        task: Task,
        agent: "AgentV2",
        bus: "TaskEventBus",
        llm_bridge=None,
        skill_bridge=None,
        mcp_bridge=None,
    ):
        self.task = task
        self.agent = agent
        self.bus = bus
        # Allow injection for tests; fall back to module imports.
        from ..bridges import llm_bridge as _llm
        from ..bridges import skill_bridge as _sk
        from ..bridges import mcp_bridge as _mcp
        self.llm_bridge = llm_bridge or _llm
        self.skill_bridge = skill_bridge or _sk
        self.mcp_bridge = mcp_bridge or _mcp

        # Cached per-step bookkeeping.
        self._tools_for_agent: list[dict] | None = None
        self._tool_source: dict[str, str] = {}   # tool_name → "skill" | "mcp"

    # ── public contract ────────────────────────────────────────────────

    def run_step(self, step: PlanStep) -> bool:
        """Advance a single step. Returns whether ``step.exit_check`` is met."""
        self._emit("step_enter", {"step_id": step.id, "goal": step.goal})

        tool_names_called: set[str] = set()
        artifacts_before = len(self.task.artifacts)
        last_assistant_text = ""

        # Seed context with a step-framing system message so the LLM knows
        # what it's being asked to do (separate from the overall intent).
        self.task.context.messages.append({
            "role": "system",
            "content": (
                f"当前 step：{step.id} — {step.goal}\n"
                f"可用工具提示：{step.tools_hint or '按你的判断选择'}\n"
                f"完成条件：exit_check={json.dumps(step.exit_check, ensure_ascii=False)}\n"
                "请立刻调用所需工具推进；只输出文字而不调用工具无法满足 exit 条件。"
            ),
        })

        for turn in range(self.MAX_TOOL_TURNS_PER_STEP):
            if len(self.task.context.messages) > self.CONTEXT_MESSAGES_SOFT_LIMIT:
                self.on_context_pressure()

            try:
                msg = self._call_llm()
            except Exception as e:  # noqa: BLE001
                self._emit("step_exit", {
                    "step_id": step.id,
                    "ok": False,
                    "reason": f"llm_error: {type(e).__name__}: {e}",
                })
                return False

            self.task.context.messages.append(msg)
            if msg.get("content"):
                last_assistant_text = msg["content"]

            tcalls = msg.get("tool_calls") or []
            if tcalls:
                for tc in tcalls:
                    tname = _tool_call_name(tc)
                    targs = _tool_call_args(tc)
                    tool_names_called.add(tname)
                    self._emit("tool_call", {
                        "step_id": step.id,
                        "tool": tname,
                        "args": targs,
                    })
                    result_text, artifact = self._invoke_tool(tname, targs)
                    if artifact is not None:
                        self.task.add_artifact(artifact)
                        self._emit("artifact_created", {
                            "artifact": {
                                "id": artifact.id,
                                "kind": artifact.kind,
                                "handle": artifact.handle,
                                "summary": artifact.summary,
                                "produced_by_tool": artifact.produced_by_tool,
                            },
                        })
                    self._emit("tool_result", {
                        "step_id": step.id,
                        "tool_call_id": tc.get("id", ""),
                        "tool": tname,
                        "summary": _truncate(result_text, 400),
                    })
                    # Append tool result into the conversation so the LLM
                    # can reason over it on the next turn (V1 pattern).
                    self.task.context.messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", ""),
                        "name": tname,
                        "content": result_text,
                    })
                # After all tool calls on this turn, check exit.
                if self._step_exit_met(
                    step, tool_names_called, artifacts_before,
                    len(self.task.artifacts), last_assistant_text,
                ):
                    step.completed = True
                    step.result_summary = _truncate(last_assistant_text, 200)
                    self._emit("step_exit", {"step_id": step.id, "ok": True})
                    return True
                continue  # let LLM chain tools

            # No tool_calls this turn — treat content as progress narrative.
            if msg.get("content"):
                self._emit("progress", {
                    "step_id": step.id,
                    "text": _truncate(msg["content"], 500),
                })

            if self._step_exit_met(
                step, tool_names_called, artifacts_before,
                len(self.task.artifacts), last_assistant_text,
            ):
                step.completed = True
                step.result_summary = _truncate(last_assistant_text, 200)
                self._emit("step_exit", {"step_id": step.id, "ok": True})
                return True

            # LLM stopped, exit not met → nudge & retry.
            self._inject_nudge(step)

        self._emit("step_exit", {
            "step_id": step.id,
            "ok": False,
            "reason": "max_tool_turns",
        })
        return False

    def on_context_pressure(self) -> None:
        """Compact ``task.context.messages`` preserving tool-call adjacency.

        Hard invariant (OpenAI function-calling spec): a message carrying
        ``tool_calls`` MUST be immediately followed by the matching
        ``role="tool"`` messages (one per tool_call_id). Any compaction
        that drops narrative between them would break the API contract.

        Algorithm:
          1. Group the message list into ATOMIC UNITS, where each unit is
             either a single plain message or a ``[tool_call_msg, *tool_results]``
             cluster (the results are consumed until the next non-tool role).
          2. Always preserve: the initial system message (if any), every
             atomic unit that contains tool_calls, and the last ~20 units.
          3. Collapse the remaining older narrative units into one
             ``[compacted]`` system summary inserted right after the
             initial system message.
        """
        msgs = self.task.context.messages
        if len(msgs) <= self.CONTEXT_MESSAGES_HARD_CUT:
            return

        # ── step 1: group into atomic units ─────────────────────────────
        # Each unit is a list[dict]; tool_call units carry the assistant
        # tool_calls message followed by every contiguous role=tool result.
        units: list[list[dict]] = []
        i = 0
        n = len(msgs)
        while i < n:
            m = msgs[i]
            if m.get("tool_calls"):
                # Consume this assistant message + immediately-following tool results.
                unit = [m]
                j = i + 1
                while j < n and msgs[j].get("role") == "tool":
                    unit.append(msgs[j])
                    j += 1
                units.append(unit)
                i = j
                continue
            if m.get("role") == "tool":
                # Orphan tool result (no preceding tool_call in scope) —
                # still keep it attached to whatever came before rather
                # than stranding it. Prepend to the last unit if one exists;
                # otherwise keep as its own unit.
                if units:
                    units[-1].append(m)
                else:
                    units.append([m])
                i += 1
                continue
            units.append([m])
            i += 1

        # ── step 2: identify which units to keep verbatim ───────────────
        initial_system: list[dict] | None = None
        body_units: list[list[dict]] = units
        if body_units and len(body_units[0]) == 1 and \
           body_units[0][0].get("role") == "system" and \
           not body_units[0][0].get("tool_calls"):
            initial_system = body_units[0]
            body_units = body_units[1:]

        # Tool units always survive; narrative units are compacted.
        tool_units:      list[list[dict]] = []
        narrative_units: list[list[dict]] = []
        for u in body_units:
            if any(msg.get("tool_calls") or msg.get("role") == "tool" for msg in u):
                tool_units.append(u)
            else:
                narrative_units.append(u)

        # Always keep last 20 narrative units verbatim as a rolling tail.
        tail = narrative_units[-20:]
        older = narrative_units[:-20]

        # ── step 3: rebuild with compaction ─────────────────────────────
        rebuilt: list[dict] = []
        if initial_system is not None:
            rebuilt.extend(initial_system)

        if older:
            collapsed = "[compacted] " + " | ".join(
                _truncate((m.get("content") or ""), 120)
                for u in older for m in u
                if m.get("content")
            )
            if collapsed.strip() != "[compacted]":
                rebuilt.append({"role": "system", "content": collapsed})

        # Keep tool units + recent narrative tail. We don't attempt to
        # restore exact chronological interleaving (the LLM doesn't need
        # ordering between tool blocks and older narrative) — but within
        # each unit the tool_call → tool_result adjacency is preserved
        # because units are flushed atomically.
        for u in tool_units:
            rebuilt.extend(u)
        for u in tail:
            rebuilt.extend(u)

        self.task.context.messages = rebuilt

    # ── internals ─────────────────────────────────────────────────────

    def _build_tools_for_agent(self) -> list[dict]:
        """Ask both bridges for tool schemas; remember which bridge owns each."""
        if self._tools_for_agent is not None:
            return self._tools_for_agent
        tools: list[dict] = []
        source: dict[str, str] = {}

        try:
            skill_tools = self.skill_bridge.get_skill_tools_for_agent(self.agent.id) if self.agent else []
        except Exception:
            skill_tools = []
        for t in skill_tools:
            name = (t.get("function") or {}).get("name", "")
            if name:
                tools.append(t); source[name] = "skill"

        try:
            mcp_tools = self.mcp_bridge.get_mcp_tools_for_agent(self.agent.id) if self.agent else []
        except Exception:
            mcp_tools = []
        for t in mcp_tools:
            name = (t.get("function") or {}).get("name", "")
            if not name:
                continue
            if name in source:
                # Name collision: skill wins (skills are explicitly granted).
                continue
            tools.append(t); source[name] = "mcp"

        self._tools_for_agent = tools
        self._tool_source = source
        return tools

    def _call_llm(self) -> dict:
        tools = self._build_tools_for_agent()
        tier = (self.agent.capabilities.llm_tier if self.agent else "default")
        return self.llm_bridge.call_llm(
            messages=list(self.task.context.messages),
            tools=tools or None,
            tier=tier,
            max_tokens=4096,
        )

    def _invoke_tool(self, tool_name: str, args: dict) -> tuple[str, Artifact | None]:
        """Dispatch the call; return (result_text, optional_artifact)."""
        # Capability snapshot enforcement: if caller provided a snapshot
        # with denied_tools, honor it. (Skill/MCP grant-level check lives
        # in each bridge itself.)
        snap = self.task.context.capabilities_snapshot or {}
        denied = set(snap.get("denied_tools") or [])
        if tool_name in denied:
            return (
                f"[executor] tool {tool_name!r} is on the denied list for this task",
                None,
            )

        source = self._tool_source.get(tool_name, "")
        agent_id = self.agent.id if self.agent else ""

        if source == "skill":
            try:
                out = self.skill_bridge.invoke_skill(agent_id, tool_name, args)
            except PermissionError as e:
                return f"[executor] skill not granted: {e}", None
            except Exception as e:  # noqa: BLE001
                return f"[executor] skill error: {type(e).__name__}: {e}", None
        elif source == "mcp":
            try:
                out = self.mcp_bridge.invoke_mcp(agent_id, tool_name, args)
            except Exception as e:  # noqa: BLE001
                return f"[executor] mcp error: {type(e).__name__}: {e}", None
        else:
            return (
                f"[executor] unknown tool {tool_name!r} "
                f"(not in agent {agent_id}'s skills or MCPs)"
            ), None

        # Heuristic: if the tool returned a path-looking string and the file
        # exists on disk, record an "artifact". This is deliberately
        # conservative so we don't spam artifacts for every string-returning
        # tool; more sophisticated detection can live in each bridge.
        artifact = None
        if isinstance(out, str):
            candidate = out.strip()
            if 1 < len(candidate) < 500 and (
                candidate.startswith("/") or candidate.startswith("./")
            ):
                import os.path
                if os.path.exists(candidate):
                    artifact = Artifact(
                        id=f"A-{len(self.task.artifacts)+1}",
                        kind="file",
                        handle=candidate,
                        summary=f"produced by {tool_name}",
                        produced_by_tool=tool_name,
                    )
        return (out if isinstance(out, str) else str(out)), artifact

    def _step_exit_met(
        self,
        step: PlanStep,
        tool_names_called: set[str],
        artifacts_before: int,
        artifacts_after: int,
        last_assistant_text: str,
    ) -> bool:
        ec = step.exit_check or {}
        kind = (ec.get("type") or "").lower()
        spec = ec.get("spec") or {}

        fn = _CHECK_DISPATCH.get(kind)
        if fn is None:
            # No (or unknown) exit_check: consider the step done once the
            # LLM has replied at least once without calling tools. That
            # keeps pure-reasoning steps workable.
            return bool(last_assistant_text)

        try:
            return bool(fn(
                spec, tool_names_called, artifacts_before, artifacts_after,
                last_assistant_text, self.task,
            ))
        except Exception:
            # A broken exit_check shouldn't hang the task; treat as unmet
            # and let the outer retry logic handle it.
            return False

    def _inject_nudge(self, step: PlanStep) -> None:
        self.task.context.messages.append({
            "role": "system",
            "content": (
                f"你刚才没有调用任何工具，但 step 『{step.goal}』的 exit 条件未满足。"
                f"请立刻调用所需工具推进本 step；不要仅仅声明意图。"
            ),
        })

    # ── emit ──────────────────────────────────────────────────────────

    def _emit(self, event_type: str, payload: dict) -> None:
        self.bus.publish(self.task.id, TaskPhase.EXECUTE, event_type, payload)


# ── module helpers ────────────────────────────────────────────────────

def _truncate(s: str, n: int) -> str:
    if not s:
        return ""
    return s if len(s) <= n else s[: n - 1] + "…"


def _tool_call_name(tc: dict) -> str:
    """Support both OpenAI and Anthropic tool_call shapes."""
    if "name" in tc:
        return tc["name"] or ""
    fn = tc.get("function") or {}
    return fn.get("name") or ""


def _tool_call_args(tc: dict) -> dict:
    """Extract tool arguments; JSON-decode if needed."""
    if "input" in tc:          # Anthropic
        v = tc["input"]
        return v if isinstance(v, dict) else {}
    if "arguments" in tc:      # some OpenAI variants put it here
        v = tc["arguments"]
    else:
        v = (tc.get("function") or {}).get("arguments")
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except json.JSONDecodeError:
            return {"_raw": v}
    return {}


__all__ = ["TaskExecutor"]
