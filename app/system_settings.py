"""
SystemSettingsStore — admin-editable runtime configuration.

JSON-backed (one file under <data_dir>/system_settings.json), thread-
safe, with dotted-path access and deep-merge updates. Modeled on
``app/branding.py`` pattern.

DEFAULTS is the source of truth for keys + fallback values.
``get()`` walks the persisted dict first; absent keys (at any depth)
return either the matching default or the caller-supplied override.

This module does NOT validate values at write time — that's the API
layer's job. Store-level operations are unconditional.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("tudou.system_settings")


# Source of truth for defaults. Any new system-level knob lives here.
DEFAULTS: dict[str, Any] = {
    "canvas": {
        # Per-run cap on concurrent canvas nodes. ThreadPoolExecutor
        # size when _drive_loop spawns ready nodes in parallel.
        "max_parallel_nodes": 6,
    },
    "delegate": {
        # Per-call cap on concurrent children spawned by
        # Agent.delegate_parallel.
        "max_parallel_children": 6,
    },
    # ── API rate limiting (RateLimitMiddleware reads here at request time) ──
    "rate_limit": {
        "enabled": True,
        # Sliding-window cap per (client_ip, path).
        # Bumped 10 → 30 (2026-05-06): SPA components legitimately
        # poll some read-heavy endpoints (e.g. /api/portal/projects has
        # 7+ call sites — dashboard tiles, project list, view rerenders)
        # and 10/5s = 2 req/s tripped 429 in normal navigation. The
        # frontend now also coalesces same-URL GETs via _apiShortGet,
        # so this bump is mostly headroom against bursts.
        "max_requests": 30,
        "window_seconds": 5.0,
    },
    # ── Agent runtime guardrails (read by tools_split + agent.py) ──
    "agent_guardrails": {
        # Soft warn / hard deny thresholds for project-scoped glob_files
        # usage per (agent, project) per hour. Soft = inject system
        # message but allow; hard = refuse the call, return error.
        "glob_soft_warn_per_hour": 5,
        "glob_hard_deny_per_hour": 15,
        # Per-response tool budget — agent must finalize after N tool
        # calls in one assistant turn (prevents runaway loops).
        # Bumped 5 → 12 (2026-05-06): PM was hitting cap on legitimate
        # multi-step turns (read 5+ deliverables → write report → update
        # milestones → QA gate). 12 allows orchestrator-level tasks to
        # complete; runaway-loop detection is layered on top via
        # agent_guardrails (signature_count + 3-signal Hermes detector).
        "tool_budget_per_turn": 12,
        # Per-role overrides on tool_budget_per_turn. Falls back to the
        # global value above if a role isn't listed. coder/researcher
        # legitimately do exploration-heavy turns (ls + cat + grep +
        # project_state + read_file + write_file in one breath) and
        # need higher headroom than orchestrator roles like pm.
        "role_overrides": {
            "coder":      {"tool_budget_per_turn": 20},
            "researcher": {"tool_budget_per_turn": 18},
        },
        # bash soft cap — was hardcoded inside agent.py's
        # _PER_TOOL_SOFT_CAP at 8. Promoted here so admin can tune via
        # Settings UI without restart. Soft = system message warning,
        # NOT a hard block (LLM can keep going by stating reason).
        "bash_soft_cap": 8,
        # Read-valve cross-tool hard cap — was hardcoded as
        # HARD_CAP_DEFAULT=5 in tools_split/_read_counter.py. Counts
        # read_file + bash cat/head/tail/less/more on the same path
        # within one turn. At cap+1 the read is BLOCKED. Bump if your
        # agents legitimately need to re-read large config files.
        "read_valve_hard_cap": 5,
        # "strict" → all deliverable contract failures block DONE.
        # "lenient" → only output_files presence checked, content rules
        # demoted to system warnings.
        "deliverable_strictness": "strict",
    },
    # ── UI polling cadence (read by portal_bundle.js via /system-settings) ──
    "ui_polling": {
        "heartbeat_seconds": 15,
        "plans_busy_seconds": 3,
        "runtime_stats_busy_seconds": 8,
    },
    # ── Auto-wakeup master switches (read by every auto-trigger entry) ──
    # When `enabled` is False, EVERY automatic agent-trigger path is
    # disabled — agents only act when the user explicitly sends a
    # message / clicks Wake. This is the kill switch for "DELEGATE
    # storm" scenarios where @-mention propagation + workflow advance
    # + watchdog wakeups feed each other in a loop and burn token
    # budget overnight.
    #
    # Sub-flags let admins keep some automation on while disabling
    # specific dangerous ones. All gated by the master `enabled` —
    # if master is False, all sub-flags are ignored (treated False).
    #
    # 2026-05-08: defaulting to FALSE because @-mention propagation
    # crossed with workflow auto-advance produced a 30-min, 50-spawn,
    # multi-million-token loop in production. Admin must explicitly
    # opt back in per project after manually tuning loop-detection.
    "auto_wakeup": {
        "enabled": False,
        # @-mention in agent reply auto-triggers mentioned agent (was
        # the loop trigger in the 2026-05-08 incident — kept off until
        # a multi-hop A→B→C→A detector is added).
        "mention_propagation": False,
        # Workflow engine advances to next step on completion.
        "workflow_advance": False,
        # Stuck-agent watchdog wakes idle agents with open plans.
        "watchdog_wake_stuck": False,
        # On step completion, auto-trigger the next-step responsible.
        "step_completion_advance": False,
        # On user message into a paused project's queue, resume on
        # unpause (this one is benign; default True even when master False).
        "resume_on_unpause": True,
    },
    # ── Tool _reason injection (read by ToolSchema.to_openai_payload) ──
    # When enabled, every tool's OpenAI schema gets a required "_reason"
    # string param (≤max_chars). Forces the LLM to articulate WHY before
    # each call — strong self-check against repeat-read loops and
    # wandering tool calls. Stripped server-side before dispatch (never
    # reaches the underlying tool function); logged to agent events for
    # debugging. Token cost: ~50 tok/tool in tools[] payload, ~30 tok per
    # actual tool call. Disable if your model handles wandering well.
    "tool_reason": {
        "enabled": True,
        "max_chars": 100,
    },
}


def _deep_merge(base: dict, patch: dict) -> dict:
    """Return a new dict = base recursively merged with patch."""
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class SystemSettingsStore:
    """Read-mostly JSON file with single-write lock."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self._path = self.data_dir / "system_settings.json"
        self._lock = threading.Lock()
        self._cache: dict | None = None

    def _load_unlocked(self) -> dict:
        if self._cache is not None:
            return self._cache
        if not self._path.exists():
            self._cache = {}
            return self._cache
        try:
            d = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                d = {}
        except Exception as e:
            logger.warning("system_settings.json read failed: %s — using defaults", e)
            d = {}
        self._cache = d
        return d

    def get(self, path: str, default: Any = None) -> Any:
        """Dotted-path lookup. Falls back to DEFAULTS at the same path,
        then to the caller-supplied ``default``."""
        if not path:
            raise ValueError("empty path")
        with self._lock:
            current = self._load_unlocked()
            walk_persisted = current
            walk_defaults = DEFAULTS
            for part in path.split("."):
                if isinstance(walk_persisted, dict) and part in walk_persisted:
                    walk_persisted = walk_persisted[part]
                else:
                    walk_persisted = _MISSING
                if isinstance(walk_defaults, dict) and part in walk_defaults:
                    walk_defaults = walk_defaults[part]
                else:
                    walk_defaults = _MISSING
            if walk_persisted is not _MISSING:
                return walk_persisted
            if walk_defaults is not _MISSING:
                return walk_defaults
            return default

    def set(self, path: str, value: Any) -> dict:
        """Dotted-path write. Atomic file replace. Returns full state."""
        if not path:
            raise ValueError("empty path")
        with self._lock:
            current = dict(self._load_unlocked())
            parts = path.split(".")
            cursor = current
            for part in parts[:-1]:
                if not isinstance(cursor.get(part), dict):
                    cursor[part] = {}
                cursor = cursor[part]
            cursor[parts[-1]] = value
            self._write_unlocked(current)
            self._cache = current
            return dict(current)

    def update(self, patch: dict) -> dict:
        """Deep-merge patch into current state. Atomic write."""
        if not isinstance(patch, dict):
            raise ValueError("patch must be a dict")
        with self._lock:
            current = self._load_unlocked()
            merged = _deep_merge(current, patch)
            self._write_unlocked(merged)
            self._cache = merged
            return dict(merged)

    def all(self) -> dict:
        """Snapshot — defaults filled in for unset keys, persisted
        values overlaid on top. Useful for the Settings UI."""
        with self._lock:
            persisted = self._load_unlocked()
            return _deep_merge(DEFAULTS, persisted)

    def _write_unlocked(self, data: dict) -> None:
        """Atomic tmp+replace. Caller holds self._lock."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, self._path)


# Sentinel for missing keys during dotted-path walk
_MISSING = object()


# ── Module-level singleton ──────────────────────────────────────────────

_STORE: SystemSettingsStore | None = None
_STORE_LOCK = threading.Lock()


def init_store(data_dir: str | Path) -> SystemSettingsStore:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            _STORE = SystemSettingsStore(data_dir)
    return _STORE


def get_store() -> SystemSettingsStore | None:
    return _STORE


# ── Auto-wakeup helper ─────────────────────────────────────────────
# Single point all auto-trigger entries call to decide "should I
# fire". Master switch + per-flag check; master False ⇒ everything
# False regardless of sub-flag value.
def auto_wakeup_allowed(kind: str = "") -> bool:
    """Return True iff the named auto-trigger path is allowed to fire.

    kind values (free-form, but these match DEFAULTS):
      "mention_propagation" — agent reply @-mention auto-trigger
      "workflow_advance"    — workflow step advance
      "watchdog_wake_stuck" — stuck-agent watchdog wake
      "step_completion_advance" — workflow step completion → next agent
      "resume_on_unpause"   — project unpause queue resume

    Caller convention: returning False means SKIP the auto-trigger
    silently (don't error). Manual user actions (Wake button,
    explicit chat send) bypass this entirely — they don't even call
    this helper.
    """
    try:
        store = get_store()
        if store is None:
            return False  # fail-safe: closed
        if not bool(store.get("auto_wakeup.enabled", False)):
            return False  # master kill
        if not kind:
            return True  # master is on; caller didn't name a sub-flag
        return bool(store.get(f"auto_wakeup.{kind}", False))
    except Exception:
        return False  # any error → fail closed
