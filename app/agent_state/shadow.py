"""
ShadowRecorder — phase-1 grey-rollout integration point.

Goal: let the new AgentState run *alongside* the existing
`Agent.messages` / `Agent.tasks` machinery without changing any
existing behaviour. Every chat turn is mirrored into a fresh
AgentState commit; failures inside the recorder are swallowed
(logged at DEBUG, never raised) so the live agent path is never
broken by phase-1 bugs.

Wiring is done by `install_into_agent(agent)` from app/agent.py
(or by Agent itself when the feature flag is on). Three hook
methods are exposed:

    record_user(text, source)        — call right after user msg appended
    record_tool_result(name, result) — call inside the tool-result loop
    record_assistant(text)           — call before chat() returns

Each call opens its own (strict=False) commit so violations show up
as warnings on `state.last_violations` but never block the agent.

Feature flag:
    TUDOU_AGENT_STATE_SHADOW=1   enable shadow recording for new Agent instances

You can also enable per-instance with `agent._shadow = ShadowRecorder(agent)`.
"""
from __future__ import annotations

import logging
import os
import threading
from typing import Any, List, Optional

from .artifact import ArtifactKind, ArtifactStore, ProducedBy
from .conversation import ConversationLog, Role
from .capability import Capability, CapabilityIndex, Availability, SideEffect
from .env import EnvState
from .extractors import extract_from_tool_result, ingest_into_store
from .invariants import Severity, Violation
from .state import AgentState, CommitError
from .task import TaskStack, Task

logger = logging.getLogger("tudou.agent_state.shadow")


ENV_FLAG = "TUDOU_AGENT_STATE_SHADOW"


def is_enabled() -> bool:
    """Shadow recording is ON by default after grey rollout.

    Set TUDOU_AGENT_STATE_SHADOW=0 (or false/no/off) to opt out.
    Any other value (including unset) means enabled.
    """
    v = os.environ.get(ENV_FLAG, "").strip().lower()
    if v in ("0", "false", "no", "off"):
        return False
    return True


# ----------------------------------------------------------------------
class ShadowRecorder:
    """Mirror of one Agent's per-turn activity into an AgentState.

    Holds its own AgentState. Thread-safety is provided by a single
    internal lock; the live agent's chat() loop is single-threaded
    per agent so contention here is essentially zero.
    """

    def __init__(self, agent: Any) -> None:
        self.agent = agent
        self.state = AgentState()
        self._lock = threading.Lock()
        self._current_task: Optional[Task] = None
        self._tool_artifacts_this_turn: List[str] = []  # ids
        self._init_env()
        self._init_capabilities()

    # ------------------------------------------------------------------
    # one-time setup
    # ------------------------------------------------------------------
    def _init_env(self) -> None:
        env: EnvState = self.state.env
        try:
            env.agent_id = getattr(self.agent, "id", "") or ""
            env.session_id = getattr(self.agent, "session_id", "") or ""
            # Best-effort deliverable_dir: the agent's working_dir is the
            # closest analogue we have right now. If empty, leave blank
            # — invariant I5 will then degrade to a warning rather than
            # blocking the commit.
            wd = getattr(self.agent, "working_dir", "") or ""
            if wd:
                env.deliverable_dir = wd
        except Exception as e:
            logger.debug("shadow: env init failed: %s", e)
        # First-pass scan: ingest anything already sitting in the
        # deliverable_dir from previous runs / side-channel drops.
        # Side-effects only — must never raise.
        try:
            self.rescan_deliverable_dir()
        except Exception as e:
            logger.debug("shadow: initial deliverable scan failed: %s", e)

    def rescan_deliverable_dir(self) -> int:
        """Public re-entry point for callers (e.g. portal endpoint) that
        want to refresh the artifact store from disk between turns.

        Returns the number of newly-ingested artifacts. Always safe to
        call repeatedly: idempotent by absolute path.
        """
        from .extractors import scan_deliverable_dir
        with self._lock:
            env = self.state.env
            if not env.deliverable_dir:
                return 0
            added = scan_deliverable_dir(
                self.state.artifacts,
                env.deliverable_dir,
                produced_by=ProducedBy(
                    agent_id=env.agent_id or None,
                    tool_id="deliverable_scan",
                ),
            )
            return len(added)

    def _init_capabilities(self) -> None:
        """Populate CapabilityIndex from whatever the live agent
        has registered as MCP/tool. Best-effort — we want presence,
        not perfect schemas, in phase 1.
        """
        caps: CapabilityIndex = self.state.capabilities
        try:
            mcp_servers = getattr(self.agent, "mcp_servers", None)
            if isinstance(mcp_servers, (list, tuple)):
                for srv in mcp_servers:
                    tool_id = getattr(srv, "id", None) or getattr(srv, "name", None)
                    if not tool_id:
                        continue
                    caps.register(Capability(
                        tool_id=str(tool_id),
                        description=getattr(srv, "description", "") or "",
                        side_effects=SideEffect.UNKNOWN,
                        availability=Availability.ONLINE,
                        source=f"mcp:{tool_id}",
                    ))
        except Exception as e:
            logger.debug("shadow: capability init failed: %s", e)

    # ------------------------------------------------------------------
    # hook 1: user message
    # ------------------------------------------------------------------
    def record_user(self, text: str, source: str = "admin") -> None:
        if text is None:
            return
        with self._lock:
            try:
                self._tool_artifacts_this_turn = []
                # Phase 1: every user turn opens a new task. The
                # IntentClassifier will replace this with proper
                # continue/push/pop logic in phase 2.
                with self.state.commit(strict=False):
                    task = self.state.tasks.push(
                        goal=text[:200],
                        metadata={"source": source},
                    )
                    self._current_task = task
                    self.state.conversation.append(
                        Role.USER, text,
                        task_id=task.id,
                        metadata={"source": source},
                    )
                self._log_violations()
            except CommitError as ce:
                logger.warning("shadow: record_user commit error: %s", ce)
            except Exception as e:
                logger.debug("shadow: record_user failed: %s", e)

    # ------------------------------------------------------------------
    # hook 2: tool result
    # ------------------------------------------------------------------
    # Tools whose results are informational references (search results,
    # fetched pages), NOT agent-produced files.  We skip ALL artifact
    # candidates from these tools — URLs, HTML docs, etc. should not
    # appear as file download cards in the chat UI.
    _REFERENCE_TOOLS = frozenset({
        "web_search", "web_fetch", "web_screenshot",
        "http_request",
        # Read-only file tools: listing/reading files should not
        # produce artifact download cards in the chat UI.
        "list_files", "read_file", "search_files",
        "list_directory", "get_file_info", "file_search",
        # RAG retrieval: result JSON contains source_file / heading_path
        # strings that point at KNOWLEDGE BASE documents, not artifacts
        # the agent produced. Without this, every RAG hit becomes a
        # spurious download card on the assistant bubble.
        "knowledge_lookup",
    })

    # Reserved workspace filenames — config files already injected into the
    # system prompt via XML blocks. Rendering them as chat attachments is
    # pure noise. Centralised here so record_tool_result,
    # build_envelope_refs, and compute_file_index_from_events all apply
    # the same rule.
    _RESERVED_WORKSPACE_NAMES = frozenset({
        "Project.md", "Tasks.md", "Skills.md",
        "MCP.md", "Scheduled.md", "ActiveThinking.md",
    })
    # Path-fragment blacklist for infra directories whose contents are
    # NEVER deliverables (spill cache, shared meeting reads, agent meta).
    _INFRA_PATH_FRAGMENTS = (
        "/tool_outputs/",
        "/.tudou_claw/",
        "/workspaces/meetings/",
    )

    @staticmethod
    def _looks_like_infra(value: str) -> bool:
        """Return True if `value` (a path/url) refers to an infra file
        that must never become a chat attachment."""
        if not value:
            return False
        val = str(value)
        base = val.rsplit("/", 1)[-1] if "/" in val else val
        if base in ShadowRecorder._RESERVED_WORKSPACE_NAMES:
            return True
        # Skill preview / draft markdowns that sometimes leak through
        if base.endswith("-skill.md") or base == "skill-full.md":
            return True
        for frag in ShadowRecorder._INFRA_PATH_FRAGMENTS:
            if frag in val:
                return True
        return False

    def record_tool_result(self, tool_name: str, result_str: str) -> None:
        if not result_str:
            return
        with self._lock:
            try:
                with self.state.commit(strict=False):
                    candidates = extract_from_tool_result(result_str)
                    # Skip ALL artifacts from reference tools (web_search,
                    # web_fetch, etc.) — their outputs are informational
                    # references, not agent-produced files.
                    if tool_name in self._REFERENCE_TOOLS:
                        candidates = []
                    # Spill-path filter: _maybe_spill_tool_result rewrites
                    # large bash / read_file results as
                    #   [spilled: /workspace/tool_outputs/xxx.md, N chars]
                    # The extractor faithfully pulls that path out, which
                    # then surfaces as a download card for the spill file
                    # itself — infrastructure masquerading as output.
                    # Drop any candidate pointing inside tool_outputs/.
                    if candidates:
                        candidates = [
                            c for c in candidates
                            if "/tool_outputs/" not in str(c.get("value", ""))
                            and not str(c.get("value", "")).endswith("tool_outputs")
                        ]
                    # URL filter — URLs found inside ANY tool result are
                    # references (web pages the LLM mentioned), never
                    # deliverable files. Keeping them would render as
                    # download cards in the chat ("regions-list", random
                    # blog URLs, etc.) polluting the agent's visible
                    # output. Only file-kind artifacts are real deliverables.
                    if candidates:
                        _NON_DELIVERABLE_KINDS = {
                            ArtifactKind.URL,
                            ArtifactKind.RECORD,
                            ArtifactKind.TEXT_BLOB,
                            ArtifactKind.EXTERNAL_ID,
                            ArtifactKind.OTHER,
                        }
                        candidates = [
                            c for c in candidates
                            if c.get("kind") not in _NON_DELIVERABLE_KINDS
                        ]
                    # Reserved-name + infra-path filter (single source
                    # of truth — see ShadowRecorder._looks_like_infra).
                    if candidates:
                        candidates = [
                            c for c in candidates
                            if not self._looks_like_infra(c.get("value", ""))
                        ]
                    if not candidates:
                        # still record a tool turn so we can audit
                        self.state.conversation.append(
                            Role.TOOL,
                            result_str[:2000],
                            task_id=self._task_id(),
                            metadata={"tool": tool_name, "extracted": 0},
                        )
                        return
                    pb = ProducedBy(
                        task_id=self._task_id(),
                        tool_id=tool_name,
                        agent_id=getattr(self.agent, "id", "") or "",
                    )
                    new_artifacts = ingest_into_store(
                        self.state.artifacts, candidates, produced_by=pb,
                    )
                    refs = [a.id for a in new_artifacts]
                    self._tool_artifacts_this_turn.extend(refs)
                    if self._current_task is not None:
                        for r in refs:
                            self.state.tasks.attach_result(
                                self._current_task.id, r,
                            )
                    self.state.conversation.append(
                        Role.TOOL,
                        result_str[:2000],
                        artifact_refs=refs,
                        task_id=self._task_id(),
                        metadata={"tool": tool_name, "extracted": len(refs)},
                    )
                self._log_violations()
            except CommitError as ce:
                logger.warning("shadow: record_tool_result commit error: %s", ce)
            except Exception as e:
                logger.debug("shadow: record_tool_result failed: %s", e)

    # ------------------------------------------------------------------
    # hook 3: assistant final reply
    # ------------------------------------------------------------------
    def record_assistant(self, text: str) -> None:
        if text is None:
            text = ""
        with self._lock:
            try:
                with self.state.commit(strict=False):
                    # Only attach artifacts that were explicitly produced
                    # by tool calls this turn.  Do NOT extract file paths
                    # from assistant prose — that causes false-positive
                    # file cards for files the assistant merely *mentions*
                    # (e.g. agent.json, MCP.md) without having created.
                    all_refs = list(self._tool_artifacts_this_turn)
                    self.state.conversation.append(
                        Role.ASSISTANT,
                        text,
                        artifact_refs=all_refs,
                        task_id=self._task_id(),
                    )
                    if self._current_task is not None:
                        self.state.tasks.mark_done(self._current_task.id)
                        self._current_task = None
                self._tool_artifacts_this_turn = []
                self._log_violations()
            except CommitError as ce:
                logger.warning("shadow: record_assistant commit error: %s", ce)
            except Exception as e:
                logger.debug("shadow: record_assistant failed: %s", e)

    # ------------------------------------------------------------------
    # hook 4: error path
    # ------------------------------------------------------------------
    def record_error(self, err: Exception) -> None:
        with self._lock:
            try:
                with self.state.commit(strict=False):
                    if self._current_task is not None:
                        self.state.tasks.mark_failed(
                            self._current_task.id,
                            reason=f"{type(err).__name__}: {err}",
                        )
                        self._current_task = None
                    self.state.conversation.append(
                        Role.SYSTEM,
                        f"[error] {type(err).__name__}: {err}",
                    )
            except Exception as e:
                logger.debug("shadow: record_error failed: %s", e)
            self._tool_artifacts_this_turn = []

    # ------------------------------------------------------------------
    # public URL builder — used by upstream message renderers to turn
    # an artifact id into a <video src="..."> URL.
    # ------------------------------------------------------------------
    def get_public_url(self, artifact_id: str) -> str:
        """Return the URL the frontend should hit to fetch the
        artifact. Returns "" if the artifact is unknown or this
        recorder has no agent_id.

        URL-valued artifacts (kind=URL or value already starting with
        http) are returned as-is — the html_tag_router would just
        302-redirect to them anyway, so we save the round-trip.
        """
        agent_id = getattr(self.agent, "id", "") or ""
        if not agent_id or not artifact_id:
            return ""
        art = self.state.artifacts.get(artifact_id)
        if art is None:
            return ""
        v = art.value or ""
        if v.startswith(("http://", "https://")):
            return v
        try:
            from app.server.html_tag_router import build_artifact_url
        except Exception:
            return ""
        return build_artifact_url(agent_id, artifact_id)

    # ------------------------------------------------------------------
    # phase-2 envelope injection
    # ------------------------------------------------------------------
    def _artifact_to_ref(self, aid: str) -> Optional[dict]:
        """Render a single artifact id into a frontend-ready FileCard
        dict, or return None if the artifact is missing / empty /
        no-longer-on-disk.

        Pure function on `self.state.artifacts` — does not take the lock,
        so callers MUST hold `self._lock` for the duration of any
        multi-artifact loop that needs a consistent view.
        """
        art = self.state.artifacts.get(aid)
        if art is None:
            return None
        md = art.metadata or {}
        v = art.value or ""
        is_http = v.startswith(("http://", "https://"))
        # ── Existence check ──
        # Artifacts can become "ghosts" when the underlying file gets
        # deleted (spill-cleanup, user `rm`, workspace wipe, etc.) but
        # the in-memory artifact entry survives. Showing a FileCard
        # that 404s on click is worse than hiding it. Skip any local
        # artifact whose value points at a path that no longer exists.
        if v and not is_http:
            try:
                import os as _os
                if not _os.path.exists(v):
                    return None
            except Exception:
                pass
        if is_http:
            url = v
        else:
            try:
                from app.server.html_tag_router import build_artifact_url
                url = build_artifact_url(
                    getattr(self.agent, "id", "") or "", aid,
                )
            except Exception:
                url = ""
        return {
            "id": aid,
            "url": url,
            # `path` exposed for portal UI features that need to pass the
            # raw filesystem path back to the backend (e.g. the /pptx-preview
            # endpoint that converts .pptx → slide thumbnails). Only set for
            # local files; remote URLs have no path.
            "path": "" if is_http else v,
            "filename": md.get("filename") or art.label or aid,
            "label": art.label or md.get("filename") or aid,
            "kind": art.kind.value,
            "mime": art.mime,
            "render_hint": md.get("render_hint") or "card",
            "category": md.get("category") or "other",
            "size": art.size,
            "produced_at": art.produced_at,
        }

    def list_all_file_refs(self) -> List[dict]:
        """All file-kind artifacts currently in the store, newest first.

        Used by the GET /api/portal/agent/<id>/files endpoint so the
        chat panel can render the persistent file list independent of
        the SSE turn-end envelope. Never raises.
        """
        try:
            with self._lock:
                file_kinds = {
                    ArtifactKind.FILE,
                    ArtifactKind.IMAGE,
                    ArtifactKind.VIDEO,
                    ArtifactKind.AUDIO,
                    ArtifactKind.DOCUMENT,
                    ArtifactKind.ARCHIVE,
                }
                out: List[dict] = []
                for art in self.state.artifacts.all():
                    if art.kind not in file_kinds:
                        continue
                    ref = self._artifact_to_ref(art.id)
                    if ref is not None:
                        out.append(ref)
                # newest first — produced_at is monotonic per session
                out.sort(key=lambda r: r.get("produced_at") or 0.0, reverse=True)
                return out
        except Exception as e:
            logger.debug("shadow: list_all_file_refs failed: %s", e)
            return []

    def compute_file_index_from_events(self) -> dict:
        """Deterministic file → assistant-turn-index mapping built by
        walking ``self.agent.events`` in document order. NO TIMESTAMPS.

        Algorithm
        ---------
        Walk every event in order. Maintain:

          - ``pending``: artifact ids extracted from tool_result events
                         that have not yet been "claimed" by an assistant
                         message
          - ``turn_idx``: counter that mirrors the frontend's assistant-
                         bubble indexing rule (kind=message, role=assistant,
                         content non-empty)

        On each ``tool_result`` event we run the same extractors used by
        live recording, ingest into ``self.state.artifacts`` (idempotent
        thanks to dedup-by-value), and append new ids to ``pending``.

        On each non-empty assistant message we:
          1. snapshot ``pending`` into ``turn_buckets[turn_idx]``
          2. also extract URLs/paths from the assistant's prose
          3. clear ``pending`` and bump ``turn_idx``

        Files left in ``pending`` after the loop attach to the last
        assistant turn. Files in the artifact store that never got
        bucketed (e.g. files dropped via ``scan_deliverable_dir``)
        are returned in ``orphans``.

        Returns:
            {
              "turns": [{"index": int, "refs": [<file_ref>, ...]}, ...],
              "orphans": [<file_ref>, ...],
              "total_assistant_turns": int,
            }

        Never raises.
        """
        out: dict = {"turns": [], "orphans": [], "total_assistant_turns": 0}
        try:
            from .extractors import (
                extract_from_tool_result,
                ingest_into_store,
                normalize_path_candidates,
            )
        except Exception as e:
            logger.debug("shadow: file_index extractors import failed: %s", e)
            return out
        try:
            with self._lock:
                events = list(getattr(self.agent, "events", []) or [])
                pending_ids: List[str] = []
                turn_buckets: dict = {}  # int -> List[str]
                turn_idx = -1
                # Once an artifact has been attributed to a turn, it must
                # NEVER be re-attributed to a later one. Without this set,
                # if a later tool_result (e.g. a knowledge query or file
                # search) mentions the same path, ingest_into_store with
                # return_existing=True hands back the previously-stored
                # artifact — and appending its id to pending_ids again
                # would glue the same file card onto every following
                # assistant bubble. Track first-seen ids here and skip.
                claimed_ids: set = set()
                aid_self = getattr(self.agent, "id", "") or ""
                base_dir = getattr(self.state.env, "deliverable_dir", "") or ""

                # Read-only tools whose results should NEVER produce
                # file cards — they merely inspect existing files.
                _READONLY_TOOLS = frozenset({
                    "list_files", "read_file", "search_files",
                    "list_directory", "get_file_info", "file_search",
                    "web_search", "web_fetch", "web_browse",
                    # RAG retrieval: citations ≠ produced files.
                    "knowledge_lookup",
                })

                for ev in events:
                    kind = getattr(ev, "kind", "")
                    data = getattr(ev, "data", {}) or {}
                    if kind == "tool_result":
                        tool_name = str(data.get("name") or "")
                        # Skip read-only tools — listing/reading files
                        # should not produce artifact cards.
                        if tool_name in _READONLY_TOOLS:
                            continue
                        result = data.get("result", "")
                        if not isinstance(result, str):
                            try:
                                import json as _json
                                result = _json.dumps(result, ensure_ascii=False)
                            except Exception:
                                result = str(result)
                        try:
                            cands = extract_from_tool_result(result)
                            if cands:
                                # Drop non-deliverable kinds (URL /
                                # RECORD / TEXT_BLOB / EXTERNAL_ID / OTHER)
                                # — only files the agent actually produced
                                # should surface as download cards.
                                _NON_DELIV = {
                                    ArtifactKind.URL,
                                    ArtifactKind.RECORD,
                                    ArtifactKind.TEXT_BLOB,
                                    ArtifactKind.EXTERNAL_ID,
                                    ArtifactKind.OTHER,
                                }
                                cands = [c for c in cands
                                         if c.get("kind") not in _NON_DELIV]
                            # Reserved-name + infra-path filter (same
                            # rule as live record_tool_result — MUST
                            # match or replay diverges from live state).
                            if cands:
                                cands = [c for c in cands
                                         if not self._looks_like_infra(c.get("value", ""))]
                            if cands:
                                normalize_path_candidates(cands, base_dir)
                                pb = ProducedBy(
                                    tool_id=tool_name or "tool",
                                    agent_id=aid_self,
                                )
                                new_arts = ingest_into_store(
                                    self.state.artifacts, cands, produced_by=pb,
                                    return_existing=True,
                                )
                                for a in new_arts:
                                    # Only the FIRST tool_result that
                                    # produces a given artifact gets to
                                    # attach it to the current turn.
                                    if a.id in claimed_ids:
                                        continue
                                    claimed_ids.add(a.id)
                                    pending_ids.append(a.id)
                        except Exception as e:
                            logger.debug("shadow: replay tool_result failed: %s", e)
                    elif kind == "message":
                        role = data.get("role") or ""
                        content = data.get("content") or ""
                        if role == "system":
                            continue
                        if role == "assistant":
                            if not str(content).strip():
                                continue
                            turn_idx += 1
                            if pending_ids:
                                turn_buckets.setdefault(turn_idx, []).extend(pending_ids)
                                pending_ids = []
                            # NOTE: We intentionally do NOT extract file
                            # references from assistant prose. The agent
                            # mentioning a filename ("I found agent.json")
                            # does not mean it created it. Only tool_result
                            # events from write/creation tools produce
                            # artifact cards — this avoids false positives
                            # where read-only file mentions generate
                            # irrelevant download cards.
                        # user role: skip (only counting assistant turns)

                # Any leftover pending → attach to the last assistant turn
                if pending_ids and turn_idx >= 0:
                    turn_buckets.setdefault(turn_idx, []).extend(pending_ids)

                # Build the response
                file_kinds = {
                    ArtifactKind.FILE,
                    ArtifactKind.IMAGE,
                    ArtifactKind.VIDEO,
                    ArtifactKind.AUDIO,
                    ArtifactKind.DOCUMENT,
                    ArtifactKind.ARCHIVE,
                }
                bucketed_ids: set = set()
                turns_out: List[dict] = []
                for idx in sorted(turn_buckets.keys()):
                    refs: List[dict] = []
                    seen: set = set()
                    for aid in turn_buckets[idx]:
                        if aid in seen:
                            continue
                        seen.add(aid)
                        art = self.state.artifacts.get(aid)
                        if art is None or art.kind not in file_kinds:
                            continue
                        ref = self._artifact_to_ref(aid)
                        if ref is None:
                            continue
                        refs.append(ref)
                        bucketed_ids.add(aid)
                    if refs:
                        turns_out.append({"index": idx, "refs": refs})

                orphans: List[dict] = []
                for art in self.state.artifacts.all():
                    if art.id in bucketed_ids:
                        continue
                    if art.kind not in file_kinds:
                        continue
                    ref = self._artifact_to_ref(art.id)
                    if ref is not None:
                        orphans.append(ref)

                out = {
                    "turns": turns_out,
                    "orphans": orphans,
                    "total_assistant_turns": turn_idx + 1,
                }
                return out
        except Exception as e:
            logger.debug("shadow: compute_file_index_from_events failed: %s", e)
            return out

    def build_envelope_refs(self) -> List[dict]:
        """Return artifact_refs for the most-recent assistant turn.

        Each ref is a small JSON-friendly dict the frontend can render
        directly into a FileCard, no further lookups required:

            {
                "id":          "art_xxxx",
                "url":         "/api/agent_state/artifact/<agent>/<id>"
                                  or original https URL,
                "filename":    "report.docx",
                "label":       "report.docx",
                "kind":        "document",
                "mime":        "application/vnd...",
                "render_hint": "card",
                "category":    "office",
                "size":        12345,        # may be None
            }

        Reads from `conversation.last assistant turn` so timing is
        independent of the per-turn scratch state — even if a future
        refactor moves the reset, this still works.

        Returns [] if no assistant turn yet, or the latest turn has
        no refs. Never raises.
        """
        try:
            with self._lock:
                turns = self.state.conversation.all()
                last_assistant = None
                for t in reversed(turns):
                    if t.role == Role.ASSISTANT:
                        last_assistant = t
                        break
                if last_assistant is None or not last_assistant.artifact_refs:
                    return []
                # Only emit DELIVERABLE file-kind refs. URL / RECORD /
                # TEXT_BLOB / EXTERNAL_ID are references the agent
                # touched, not files it produced, and rendering them as
                # download cards pollutes the chat bubble. Legacy
                # artifacts from before this filter also get suppressed.
                file_kinds = {
                    ArtifactKind.FILE,
                    ArtifactKind.IMAGE,
                    ArtifactKind.VIDEO,
                    ArtifactKind.AUDIO,
                    ArtifactKind.DOCUMENT,
                    ArtifactKind.ARCHIVE,
                }
                # Reserved / infra filter is centralised in
                # _looks_like_infra (match live record_tool_result +
                # compute_file_index_from_events exactly).
                out: List[dict] = []
                for aid in last_assistant.artifact_refs:
                    art = self.state.artifacts.get(aid)
                    if art is None or art.kind not in file_kinds:
                        continue
                    # Match against label (filename) and value (path/url).
                    label = (getattr(art, "label", "") or "").strip()
                    value = str(getattr(art, "value", "") or "")
                    if (self._looks_like_infra(value)
                            or self._looks_like_infra(label)):
                        logger.debug(
                            "shadow: skip reserved ref %s (%s)",
                            aid, label or value)
                        continue
                    ref = self._artifact_to_ref(aid)
                    if ref is not None:
                        out.append(ref)
                return out
        except Exception as e:
            logger.debug("shadow: build_envelope_refs failed: %s", e)
            return []

    # ------------------------------------------------------------------
    # introspection — for /api/agent_state/shadow or REPL
    # ------------------------------------------------------------------
    def summary(self) -> dict:
        return self.state.summary()

    def recent_artifacts(self, n: int = 10) -> list:
        items = self.state.artifacts.all()[-n:]
        return [
            {
                "id": a.id,
                "kind": a.kind.value,
                "label": a.label,
                "value": a.value,
                "produced_by": {
                    "task_id": a.produced_by.task_id,
                    "tool_id": a.produced_by.tool_id,
                },
                "produced_at": a.produced_at,
                "mime": a.mime,
            }
            for a in items
        ]

    # ------------------------------------------------------------------
    def _task_id(self) -> Optional[str]:
        return self._current_task.id if self._current_task else None

    def _log_violations(self) -> None:
        vs = self.state.last_violations
        if not vs:
            return
        for v in vs:
            if v.severity == Severity.ERROR:
                logger.warning("shadow invariant: %s", v)
            else:
                logger.debug("shadow invariant: %s", v)


# ----------------------------------------------------------------------
def install_into_agent(agent: Any, *, force: bool = False) -> Optional[ShadowRecorder]:
    """Attach a ShadowRecorder to `agent` if the feature flag is on,
    or if `force=True`. Idempotent — calling twice returns the existing
    recorder. Returns None when disabled.
    """
    if not force and not is_enabled():
        return None
    existing = getattr(agent, "_shadow", None)
    if isinstance(existing, ShadowRecorder):
        return existing
    rec = ShadowRecorder(agent)
    try:
        setattr(agent, "_shadow", rec)
    except Exception as e:
        logger.debug("shadow: setattr failed: %s", e)
        return None
    logger.info("shadow recorder installed for agent %s", getattr(agent, "id", "?"))
    return rec
