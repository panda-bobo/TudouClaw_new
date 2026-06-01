"""One-shot recovery: convert agent.transcript_entries (which captured
user-side messages even when chat events were lost) back into
agent.events as kind='message', role='user' entries — so the chat UI
can render them as bubbles after a "history disappeared" incident.

Background:
  - Before 2026-05-16 evening's mid-run persist fix (commit 50403f2),
    SDK-runtime chats only persisted at end-of-run. Backend restarts
    mid-flight lost agent.events for those sessions.
  - User-side instructions to the LLM were ALSO captured in a
    SEPARATE buffer ``agent.transcript_entries`` (the src-memory
    transcript stream, populated by inject_user_text and similar
    paths). Those survived because they were saved on a different
    persist cadence.
  - Result: today an affected agent has 0 message-kind events but
    has N user instructions in transcript_entries. UI chat panel
    appears empty.

This script:
  1. Reads each target agent's transcript_entries.
  2. Strips the "⚠️ ADMIN 指令 (最高优先级…)" wrapper boilerplate so
     bubbles render the actual user text only.
  3. Parses [HH:MM:SS] timestamps embedded in the entry when possible,
     so the restored bubbles sit at roughly correct times in the log.
  4. Appends each as an AgentEvent dict (kind='message', data={role:
     user, content: text, source: 'transcript_recovery'}) to
     agent.events.
  5. Writes back to SQLite + agents.json.

ONE-SHOT: idempotent guard via ``_source: 'transcript_recovery'``
marker — re-running won't double-insert.

Limitations (NOT recoverable from this script):
  - Agent's replies (those were in stream events that got lost).
  - Tool calls / results (same).
  - Sub-second timestamps.
  - Original context_id binding (everything goes into the global
    events list; the messages_by_context buckets stay as they were).

Usage:
  python scripts/recover_transcript_to_events.py             # all agents
  python scripts/recover_transcript_to_events.py f8bc9bf4    # just 小新

Always restart the backend after running so it reloads from disk.
"""
from __future__ import annotations
import json
import os
import re
import sqlite3
import sys
import time
from typing import Any


_DB_PATH = os.path.expanduser("~/.tudou_claw/tudou_claw.db")
_JSON_PATH = os.path.expanduser("~/.tudou_claw/agents.json")
_RECOVERY_MARKER = "transcript_recovery"

# The ADMIN-instruction wrapper transcripts get wrapped in. We strip
# the boundary lines + the "⚠️ ADMIN 指令" header so the bubble shows
# just the user's actual text. Regex aims to be tolerant of width
# variations (━ bars come in 30-char and longer flavors).
_ADMIN_WRAPPER = re.compile(
    r"━+\s*\n⚠️ ADMIN 指令[^\n]*\n━+\s*\n",
    re.MULTILINE,
)
# Tail boilerplate ("规则：1. 如果上面有「暂停/停止/取消…") that the
# wrapper injects after the user text. We chop everything from the
# first "规则：" onwards to keep the bubble focused on the actual ask.
_RULES_TAIL = re.compile(r"\n*规则[:：][\s\S]*$")


def _clean_entry(text: str) -> str:
    """Strip ADMIN-instruction boilerplate so the bubble shows the
    bare user text."""
    text = _ADMIN_WRAPPER.sub("", text)
    text = _RULES_TAIL.sub("", text)
    text = text.strip()
    return text


def _extract_embedded_timestamp(text: str) -> float:
    """Try to find a ``[HH:MM:SS]`` in the text and return a
    today-relative epoch. Returns 0.0 if not found."""
    m = re.search(r"\[(\d{2}):(\d{2}):(\d{2})\]", text)
    if not m:
        return 0.0
    h, mn, s = (int(g) for g in m.groups())
    today = time.localtime()
    return time.mktime((
        today.tm_year, today.tm_mon, today.tm_mday,
        h, mn, s, 0, 0, today.tm_isdst,
    ))


def _build_event_dicts(transcript: list, base_ts: float) -> list[dict]:
    """Turn each transcript_entry into an event dict ready to append
    to agent.events. base_ts is used as the fallback when no embedded
    timestamp is found; each subsequent fallback bumps by 60s."""
    out = []
    fallback_ts = base_ts
    for raw in transcript:
        if not isinstance(raw, str):
            continue
        cleaned = _clean_entry(raw)
        if not cleaned:
            continue
        ts = _extract_embedded_timestamp(raw)
        if ts <= 0:
            ts = fallback_ts
            fallback_ts += 60
        out.append({
            "timestamp": ts,
            "kind": "message",
            "data": {
                "role": "user",
                "content": cleaned,
                "source": _RECOVERY_MARKER,
            },
        })
    return out


def _already_recovered(events: list) -> int:
    """Count how many events look like they came from a previous
    run of this script (for idempotency)."""
    n = 0
    for e in events or []:
        d = e.get("data") if isinstance(e, dict) else None
        if isinstance(d, dict) and d.get("source") == _RECOVERY_MARKER:
            n += 1
    return n


def _recover_one_agent(agent_dict: dict, dry_run: bool = False) -> dict:
    """Mutate agent_dict in place by appending recovered events.
    Returns stats dict."""
    name = agent_dict.get("name", "?")
    agent_id = (agent_dict.get("id") or "")[:8]
    transcript = agent_dict.get("transcript_entries") or []
    events = agent_dict.get("events") or []

    already = _already_recovered(events)
    if already > 0:
        return {
            "agent": f"{name} ({agent_id})",
            "skipped": True,
            "reason": f"already has {already} recovered events",
        }

    if not transcript:
        return {
            "agent": f"{name} ({agent_id})",
            "skipped": True,
            "reason": "no transcript_entries",
        }

    # Base ts: 24 hours ago (so recovered events show "yesterday" range
    # rather than overlapping today's real activity if any)
    base_ts = time.time() - 86400
    new_events = _build_event_dicts(transcript, base_ts)
    if not new_events:
        return {
            "agent": f"{name} ({agent_id})",
            "skipped": True,
            "reason": "no usable transcript entries (all empty after cleanup)",
        }

    # Re-sort all events (existing + new) by timestamp so the chat UI
    # renders chronologically. Existing events keep their original ts.
    combined = list(events) + new_events
    combined.sort(key=lambda e: e.get("timestamp", 0.0))
    if not dry_run:
        agent_dict["events"] = combined

    return {
        "agent": f"{name} ({agent_id})",
        "skipped": False,
        "transcript_count": len(transcript),
        "recovered_count": len(new_events),
        "events_before": len(events),
        "events_after": len(combined),
    }


def main():
    args = sys.argv[1:]
    target_prefix = args[0] if args else ""
    dry_run = "--dry" in args

    # 1. Load both stores
    with open(_JSON_PATH, "r", encoding="utf-8") as f:
        json_data = json.load(f)

    con = sqlite3.connect(_DB_PATH)
    con.row_factory = sqlite3.Row

    # SQLite holds the canonical agent state as a JSON blob in 'data'.
    db_agents = {}
    for r in con.execute("SELECT agent_id, data FROM agents"):
        db_agents[r["agent_id"]] = json.loads(r["data"])

    # 2. For each target agent, recover both copies (keep them in sync)
    print(f"recover_transcript_to_events.py — target={target_prefix or 'ALL'} "
          f"{'(DRY RUN)' if dry_run else ''}")
    print()

    stats_list = []
    for json_agent in json_data.get("agents", []):
        aid = json_agent.get("id", "")
        if target_prefix and not aid.startswith(target_prefix):
            continue
        # Recover JSON copy
        stats = _recover_one_agent(json_agent, dry_run=dry_run)
        stats_list.append(stats)
        # Sync SQLite copy if present
        if aid in db_agents and not stats.get("skipped"):
            _recover_one_agent(db_agents[aid], dry_run=dry_run)

    # 3. Print summary
    for s in stats_list:
        if s.get("skipped"):
            print(f"  [SKIP] {s['agent']}: {s['reason']}")
        else:
            print(f"  [OK]   {s['agent']}: "
                  f"transcript={s['transcript_count']} → "
                  f"recovered={s['recovered_count']} events "
                  f"(total {s['events_before']} → {s['events_after']})")

    if dry_run:
        print()
        print("dry run — nothing written. Re-run without --dry to apply.")
        return 0

    # 4. Write back
    print()
    print("writing back to disk…")
    with open(_JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)
    print(f"  agents.json updated")

    for aid, agent_dict in db_agents.items():
        if target_prefix and not aid.startswith(target_prefix):
            continue
        con.execute("UPDATE agents SET data=? WHERE agent_id=?",
                    (json.dumps(agent_dict, ensure_ascii=False), aid))
    con.commit()
    con.close()
    print(f"  SQLite updated")

    print()
    print("DONE. Restart the backend to pick up the recovered events:")
    print("  kill $(lsof -iTCP:9090 -sTCP:LISTEN -P -t) && "
          "nohup python -m app portal --port 9090 --secret admin123 "
          "> /tmp/tudou_portal.log 2>&1 &")
    return 0


if __name__ == "__main__":
    sys.exit(main())
