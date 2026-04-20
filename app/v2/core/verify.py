"""
Verify phase rule evaluators (PRD §8.4).

A verify_rule loaded from a template looks like::

    {"id": "has_summary_section",
     "kind": "contains_section",
     "spec": {"section": "## Summary"}}
     # optional: "when": "filled_slots.delivery == 'email'"

Supported ``kind`` values:
    regex             — pattern against last assistant text
    contains_section  — markdown header presence in last assistant text
    section_exists    — alias of contains_section
    json_schema       — last assistant text parses as JSON matching schema
                        (also accepts ``spec.path = "artifacts[*].summary"`` +
                         ``spec.min_words`` as a joined-text length check)
    tool_used         — tool name appears anywhere in task message history
    llm_judge         — call LLM judge; pass when ``pass_token`` in its output

A rule is skipped (passed=True, note="skipped") when its ``when``
conditional evaluates to False. The ``when`` grammar is deliberately
tiny: ``filled_slots.<key> == 'value'`` (or ``!=``). Anything richer
is treated as "always run" so we fail open rather than silently skip.

Return shape (ordered to match ``rules`` input)::

    [{"rule_id": str, "passed": bool, "note": str}, ...]
"""
from __future__ import annotations

import json
import re
from typing import Callable, Tuple


# ── public entry ──────────────────────────────────────────────────────


def evaluate_rules(
    rules: list[dict],
    *,
    task,
    llm_caller: Callable[..., dict] | None = None,
) -> list[dict]:
    return [_evaluate_one(r, task=task, llm_caller=llm_caller) for r in (rules or [])]


def _evaluate_one(rule: dict, *, task, llm_caller) -> dict:
    rid = str(rule.get("id") or "")
    kind = str(rule.get("kind") or "").lower()
    spec = rule.get("spec") or {}
    if not isinstance(spec, dict):
        spec = {}

    # ``when`` may live on the rule or inside spec (templates vary).
    when = rule.get("when") or spec.get("when") or ""
    if when and not _when_passes(when, task):
        return {"rule_id": rid, "passed": True, "note": f"skipped (when: {when})"}

    fn = _DISPATCH.get(kind)
    if fn is None:
        return {"rule_id": rid, "passed": False, "note": f"unknown kind={kind!r}"}
    try:
        ok, note = fn(spec, task, llm_caller)
    except Exception as e:  # noqa: BLE001
        return {"rule_id": rid, "passed": False,
                "note": f"evaluator error: {type(e).__name__}: {e}"}
    return {"rule_id": rid, "passed": bool(ok), "note": note or ""}


# ── context accessors ─────────────────────────────────────────────────


def _last_assistant_text(task) -> str:
    for m in reversed(task.context.messages or []):
        if m.get("role") == "assistant" and m.get("content"):
            return m["content"] or ""
    return ""


def _tools_called(task) -> set[str]:
    out: set[str] = set()
    for m in (task.context.messages or []):
        for tc in (m.get("tool_calls") or []):
            name = tc.get("name") or (tc.get("function") or {}).get("name", "")
            if name:
                out.add(name)
    return out


_WHEN_RE = re.compile(
    r"""^\s*filled_slots\.(\w+)\s*(==|!=)\s*['"]?([^'"]+?)['"]?\s*$""",
)


def _when_passes(when: str, task) -> bool:
    m = _WHEN_RE.match(when or "")
    if not m:
        return True  # Unrecognized → fail-open.
    key, op, val = m.group(1), m.group(2), m.group(3).strip()
    actual = str(task.context.filled_slots.get(key, ""))
    return (actual == val) if op == "==" else (actual != val)


# ── evaluators ────────────────────────────────────────────────────────


def _regex(spec: dict, task, llm_caller) -> Tuple[bool, str]:
    pattern = spec.get("pattern")
    if not pattern:
        return False, "no pattern"
    flags_str = (spec.get("flags") or "").lower()
    flags = 0
    if "i" in flags_str:
        flags |= re.IGNORECASE
    if "m" in flags_str:
        flags |= re.MULTILINE
    try:
        pat = re.compile(pattern, flags)
    except re.error as e:
        return False, f"bad regex: {e}"
    min_matches = int(spec.get("min_matches", 1))
    n = len(pat.findall(_last_assistant_text(task)))
    return (n >= min_matches), f"matches={n}/{min_matches}"


def _contains_section(spec: dict, task, llm_caller) -> Tuple[bool, str]:
    section = (spec.get("section") or "").strip()
    if not section:
        return False, "no section"
    esc = re.escape(section.lstrip("#").strip())
    pat = re.compile(rf"^#{{1,6}}\s*{esc}\s*$", re.MULTILINE)
    ok = bool(pat.search(_last_assistant_text(task)))
    return ok, ("found" if ok else "section missing")


def _json_schema(spec: dict, task, llm_caller) -> Tuple[bool, str]:
    # Template-style path check: {"path": "artifacts[*].summary", "min_words": 200}
    path = spec.get("path")
    if path and "artifacts" in path:
        min_words = int(spec.get("min_words", 0))
        total = sum(len((a.summary or "").split()) for a in task.artifacts)
        return (total >= min_words), f"total_words={total}/{min_words}"

    payload = _try_parse_json(_last_assistant_text(task))
    if payload is None:
        return False, "no json in last assistant message"

    schema = spec.get("schema")
    required = spec.get("required") or (
        (schema.get("required") or []) if isinstance(schema, dict) else []
    )

    if schema is not None:
        try:
            import jsonschema  # type: ignore
            jsonschema.validate(payload, schema)
            return True, "schema ok"
        except ImportError:
            pass  # fall through to required-keys check
        except Exception as e:  # noqa: BLE001
            return False, f"schema invalid: {type(e).__name__}"

    if required:
        if not isinstance(payload, dict):
            return False, "payload not dict"
        missing = [k for k in required if k not in payload]
        return (not missing), (f"missing={missing}" if missing else "required keys ok")

    return True, "parse ok"


def _tool_used(spec: dict, task, llm_caller) -> Tuple[bool, str]:
    wanted = spec.get("tool")
    if not wanted:
        return False, "no tool"
    tools = _tools_called(task)
    return (wanted in tools), f"tools_called={sorted(tools)}"


def _llm_judge(spec: dict, task, llm_caller) -> Tuple[bool, str]:
    """Ask the LLM to pass/fail the last assistant text.

    Retries up to ``spec.max_retries`` times (default 2) on exception
    or empty verdict, since judge calls are cheap and local models
    sometimes return blank content on first try.
    """
    if llm_caller is None:
        return False, "no llm_caller available"
    prompt = spec.get("prompt") or "判断是否通过。通过返回 PASS，否则返回 FAIL 并说明原因。"
    pass_token = (spec.get("pass_token") or "PASS").upper()
    max_retries = int(spec.get("max_retries", 2))
    last_err = ""
    last = _last_assistant_text(task)
    for attempt in range(max_retries + 1):
        try:
            msg = llm_caller(
                messages=[
                    {"role": "system", "content": "你是任务评估员。严格按要求返回。"},
                    {"role": "user", "content": f"{prompt}\n\n---\n被评估内容：\n{last[:4000]}"},
                ],
                tools=None,
                max_tokens=200,
            )
        except Exception as e:  # noqa: BLE001
            last_err = f"llm error: {type(e).__name__}: {e}"
            continue
        verdict = (msg.get("content") or "").strip()
        if not verdict:
            last_err = "empty verdict"
            continue
        ok = pass_token in verdict.upper()
        return ok, verdict[:200]
    return False, last_err or "judge failed after retries"


def _try_parse_json(text: str):
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*([\{\[][\s\S]*?[\}\]])\s*```", text)
    cand = m.group(1) if m else None
    if cand is None:
        lo, hi = text.find("{"), text.rfind("}")
        if 0 <= lo < hi:
            cand = text[lo:hi + 1]
    if cand is None:
        return None
    try:
        return json.loads(cand)
    except json.JSONDecodeError:
        return None


_DISPATCH: dict[str, Callable] = {
    "regex":            _regex,
    "contains_section": _contains_section,
    "section_exists":   _contains_section,
    "json_schema":      _json_schema,
    "tool_used":        _tool_used,
    "llm_judge":        _llm_judge,
}


__all__ = ["evaluate_rules"]
