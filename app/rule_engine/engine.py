"""Rule Engine — central PDP (Policy Decision Point).

Public API:

    eng = get_engine()
    decisions = eng.evaluate("before_tool_call", context={...})

    for d in decisions:
        if d.action == "deny" and d.matched:
            return d.message       # caller short-circuits

The engine itself is purely declarative — it returns Decisions but
does NOT execute them (except logging). Callers (PEPs) decide what to
do with each Decision because the right action depends on the call
site (a tool call deny means "raise tool error"; a write_file deny
means "return error string").

What the engine DOES:
  1. Look up applicable rules from the store (scope + trigger filtered)
  2. Evaluate each rule's condition against the context
  3. For every matched rule, append its actions to the Decision list
  4. Write each Decision to the audit log
  5. Return the Decision list
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .audit import init_audit, get_audit
from .condition import evaluate as eval_condition, ConditionError
from .store import init_store, get_store
from .types import Rule, Decision

logger = logging.getLogger("tudou.rule_engine")


class Engine:
    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self._store = init_store(self.data_dir)
        self._audit = init_audit(self.data_dir)

    @property
    def store(self):
        return self._store

    @property
    def audit(self):
        return self._audit

    def evaluate(self, trigger: str, context: dict) -> list[Decision]:
        """Evaluate all rules for ``trigger`` against ``context``.

        Returns a list of Decisions, one per matched rule, ordered by
        rule priority (descending). Caller iterates and applies. Any
        Decision with action="deny" and matched=True is terminal — the
        caller should stop processing further rules + actions.

        Context shape (suggested keys, any subset depending on PEP):
          tool_name, args, agent {id, name, role}, scope {kind, project_id?,
          meeting_id?, agent_id?, workspace?}, task, milestone, target,
          counters {...}

        Failures inside a single rule's condition are isolated — they
        produce a Decision with action="log" and error="..." but do
        not break the chain. Engine never raises to the PEP.
        """
        if not trigger:
            return []
        ctx_scope = (context or {}).get("scope") or {"kind": "global"}

        rules = self._store.for_trigger(trigger, ctx_scope)
        decisions: list[Decision] = []
        eval_start = time.time()

        for rule in rules:
            try:
                matched, evidence = eval_condition(rule.condition, context)
            except ConditionError as e:
                logger.warning("rule %s condition error: %s", rule.id, e)
                d = Decision(
                    rule_id=rule.id, rule_name=rule.name,
                    action="log", matched=False,
                    error=str(e),
                )
                decisions.append(d)
                self._audit_decision(trigger, rule, d, context)
                continue
            except Exception as e:
                logger.exception("rule %s eval crashed", rule.id)
                d = Decision(
                    rule_id=rule.id, rule_name=rule.name,
                    action="log", matched=False, error=str(e),
                )
                decisions.append(d)
                self._audit_decision(trigger, rule, d, context)
                continue

            if not matched:
                # No-match doesn't audit by default (would flood). Could
                # be made opt-in via rule.audit_misses=true later.
                continue

            for action in (rule.actions or []):
                act_type = str(action.get("type") or "log")
                d = Decision(
                    rule_id=rule.id,
                    rule_name=rule.name,
                    action=act_type,
                    matched=True,
                    message=str(action.get("message") or rule.description or rule.name),
                    config=dict(action),
                    evidence=evidence,
                )
                decisions.append(d)
                self._audit_decision(trigger, rule, d, context)
                if d.is_terminal:
                    # 'deny' short-circuits the rest of THIS rule's
                    # actions AND remaining rules.
                    elapsed_ms = int((time.time() - eval_start) * 1000)
                    if elapsed_ms > 50:
                        logger.warning(
                            "rule_engine.evaluate slow: trigger=%s elapsed=%dms rules=%d",
                            trigger, elapsed_ms, len(rules))
                    return decisions

        elapsed_ms = int((time.time() - eval_start) * 1000)
        if elapsed_ms > 50:
            logger.warning(
                "rule_engine.evaluate slow: trigger=%s elapsed=%dms rules=%d",
                trigger, elapsed_ms, len(rules))
        return decisions

    def _audit_decision(self, trigger: str, rule: Rule, d: Decision,
                        context: dict) -> None:
        """Persist one decision to audit.jsonl."""
        try:
            agent = (context.get("agent") or {})
            ctx_scope = (context.get("scope") or {})
            entry = {
                "ts": time.time(),
                "trigger": trigger,
                "rule_id": rule.id,
                "rule_name": rule.name,
                "scope": ctx_scope,
                "agent": {
                    "id": agent.get("id", ""),
                    "name": agent.get("name", ""),
                    "role": agent.get("role", ""),
                },
                "decision": d.action if d.matched else "no_match",
                "matched": d.matched,
                "message": d.message,
                "evidence": d.evidence,
                "error": d.error or None,
            }
            self._audit.write(entry)
        except Exception as e:
            logger.debug("audit emit failed: %s", e)


# ── Module singleton ───────────────────────────────────────────────

_ENGINE: Engine | None = None
_ENGINE_LOCK = threading.Lock()


def init_engine(data_dir: str | Path) -> Engine:
    global _ENGINE
    with _ENGINE_LOCK:
        if _ENGINE is None:
            _ENGINE = Engine(data_dir)
    return _ENGINE


def get_engine() -> Optional[Engine]:
    return _ENGINE
