"""Per-agent Markdown session log — human-readable chat transcript.

This is **distinct** from ``agent.json`` (LLM-facing context with
system prompts, tool args, intermediate state, summarisation). The
session log is what a *human* opens later to recall what was said
or to grep across sessions.

Layout
------

::

    ~/.tudou_claw/workspaces/{agent_id}/sessions/YYYY-MM-DD.md

One file per UTC-local calendar day per agent. Append-only — every
new message is added; the file is never rewritten. A SIGKILL during
write at worst loses the in-flight line, never prior content.

Format
------

Each turn is a level-2 header followed by the body. Sequence numbers
are global per agent (not per file) so cross-day grepping reads
naturally::

    ## [HH:MM:SS] #42 🧑 user

    actual user text...

    ## [HH:MM:SS] #43 🤖 agent_name

    assistant reply text...

    ## [HH:MM:SS] #44 📞 tool call — `Bash`

    ```json
    {
      "command": "ls /tmp"
    }
    ```

    ## [HH:MM:SS] #45 🔧 tool result — `Bash`

    ```
    /tmp/foo
    /tmp/bar
    ```

A horizontal rule + "Session resumed after N min idle" marker is
inserted automatically when a user message arrives more than 30 min
after the previous user message — handy for skim-reading.

Threading
---------

One ``threading.Lock`` per agent serialises file writes. The chat
loop never waits more than the time of one short ``write()`` call,
which is well below 1 ms for the line sizes involved.
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime
from typing import Any


# Per-message body cap in markdown — long bodies (e.g. read_file of
# a 5000-line file) get head+tail with a truncation marker. We DO want
# more here than in the LLM transcript because humans can scroll.
_BODY_CAP = 8000
_HEADER_RE = re.compile(r'^## \[\d+:\d+:\d+\] #(\d+) ', re.MULTILINE)


class SessionMarkdownLog:
    """Append-only Markdown session log scoped to a single agent."""

    def __init__(self, agent_id: str, agent_name: str, data_dir: str,
                 *, session_break_minutes: int = 30) -> None:
        self.agent_id = agent_id
        self.agent_name = (agent_name or "agent").strip() or "agent"
        self._dir = os.path.join(data_dir, "workspaces", agent_id, "sessions")
        try:
            os.makedirs(self._dir, exist_ok=True)
        except Exception:
            # Read-only filesystem etc. — degrade silently; chat must continue.
            pass
        self._lock = threading.Lock()
        # Global sequence counter — persists across days, restarts.
        # On init, scan the most recent day's file (and one prior in
        # case we just crossed midnight) to recover the max #.
        self._seq = self._recover_max_seq() + 1
        # Tracks last user-message wall-clock for "session resumed"
        # marker detection. Initialized 0 so the first message of any
        # day never triggers the marker.
        self._last_user_at: float = 0.0
        self._session_break_secs = session_break_minutes * 60

    # ── public API ────────────────────────────────────────────────────

    def append_message(self, role: str, content: str,
                       *, source: str = "", tool_name: str = "") -> None:
        """Append a chat-style message (user / assistant / tool / system).

        ``content`` is written as-is (Markdown body). For ``tool`` role
        the body is wrapped in a triple-backtick fence so JSON / shell
        output renders as code.
        """
        if not content and role != "tool":
            return
        body = self._truncate_body(str(content or ""))
        with self._lock:
            now = datetime.now()
            seq = self._seq
            self._seq += 1
            ts = now.strftime("%H:%M:%S")
            emoji = {
                "user": "🧑",
                "assistant": "🤖",
                "tool": "🔧",
                "system": "⚙️",
            }.get(role, "•")
            label = (self.agent_name if role == "assistant" else role)
            header = f"\n## [{ts}] #{seq} {emoji} {label}"
            if tool_name:
                header += f" — `{tool_name}`"
            header += "\n\n"

            # Session-resumed marker — only fires for user messages
            # that arrive after a long quiet stretch. Helps the human
            # eye find natural conversation boundaries.
            preamble = ""
            if role == "user" and self._last_user_at > 0:
                gap = now.timestamp() - self._last_user_at
                if gap > self._session_break_secs:
                    minutes = int(gap // 60)
                    preamble = (
                        f"\n---\n\n*Session resumed after "
                        f"{minutes} min idle.*\n")

            if role == "tool":
                fenced = f"```\n{body}\n```\n"
                self._write_raw(preamble + header + fenced)
            else:
                self._write_raw(preamble + header + body + "\n")

            if role == "user":
                self._last_user_at = now.timestamp()

    def append_tool_call(self, tool_name: str, arguments: Any) -> None:
        """Append a tool_call event (separate kind so it's visually
        distinct from results in the transcript)."""
        if not tool_name:
            return
        try:
            args_str = json.dumps(arguments, ensure_ascii=False, indent=2)
        except Exception:
            args_str = repr(arguments)[:1000]
        args_str = self._truncate_body(args_str)
        with self._lock:
            now = datetime.now()
            seq = self._seq
            self._seq += 1
            ts = now.strftime("%H:%M:%S")
            md = (f"\n## [{ts}] #{seq} 📞 tool call — `{tool_name}`\n\n"
                  f"```json\n{args_str}\n```\n")
            self._write_raw(md)

    def session_marker(self, label: str) -> None:
        """Manually insert a horizontal-rule + label marker (for
        explicit session boundaries: agent restart, mode switch, etc.)."""
        if not label:
            return
        with self._lock:
            now = datetime.now()
            ts = now.strftime("%Y-%m-%d %H:%M:%S")
            self._write_raw(f"\n---\n\n**[{ts}] {label}**\n\n")

    # ── internals ─────────────────────────────────────────────────────

    def _path_for_today(self) -> str:
        d = datetime.now().strftime("%Y-%m-%d")
        return os.path.join(self._dir, f"{d}.md")

    def _truncate_body(self, body: str) -> str:
        if len(body) <= _BODY_CAP:
            return body
        head = _BODY_CAP // 2
        return (body[:head]
                + f"\n\n…[truncated {len(body) - _BODY_CAP}c]…\n\n"
                + body[-head:])

    def _write_raw(self, text: str) -> None:
        path = self._path_for_today()
        try:
            # Standard "a" mode: each write() is atomic on POSIX for
            # sub-PIPE_BUF writes, and we never rewrite — partial-write
            # corruption is impossible for our line sizes.
            with open(path, "a", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            # Best-effort: never break the chat loop on disk failure.
            pass

    def _recover_max_seq(self) -> int:
        """Read the two most-recent .md files and return the highest
        sequence number found. Used on init so a process restart picks
        up the counter where it left off (no overlap with prior runs).
        """
        if not os.path.isdir(self._dir):
            return 0
        try:
            files = sorted(f for f in os.listdir(self._dir)
                           if f.endswith(".md"))
        except Exception:
            return 0
        if not files:
            return 0
        # Most-recent + previous (handles midnight crossover)
        check = files[-2:] if len(files) >= 2 else files[-1:]
        max_seq = 0
        for f in check:
            try:
                with open(os.path.join(self._dir, f),
                          "r", encoding="utf-8") as fp:
                    for line in fp:
                        m = _HEADER_RE.match(line)
                        if m:
                            n = int(m.group(1))
                            if n > max_seq:
                                max_seq = n
            except Exception:
                continue
        return max_seq
