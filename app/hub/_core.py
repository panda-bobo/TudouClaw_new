"""
Hub — multi-agent registry, coordination, and cross-machine networking.

The Hub is the central brain that:
- Manages all local agents
- Accepts remote agent-server connections
- Proxies chat/delegate to remote agents
- Routes inter-agent messages
- Orchestrates multi-agent workflows
"""
import atexit
import json
import logging
import os
import signal
import threading
import time
from typing import Any

import requests as http_requests

logger = logging.getLogger("tudou.hub")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-5s %(name)s | %(message)s",
        datefmt="%H:%M:%S"))
    logger.addHandler(_h)
    logger.setLevel(logging.DEBUG)

from ..agent import Agent, AgentStatus, AgentEvent, create_agent, ROLE_PRESETS
from ..persona import (PERSONA_TEMPLATES, list_personas, get_persona,
                       apply_persona_to_agent)
from ..project import (Project, ProjectChatEngine, ProjectTask,
                       ProjectTaskStatus, ProjectMessage)
from ..workflow import (Workflow, WorkflowEngine, WorkflowStep, WorkflowStatus,
                        WORKFLOW_TEMPLATES, list_workflow_templates,
                        get_workflow_template)
from ..src_bridge import get_bridge, SrcBridge

# Domain types — canonical definitions live in hub/types.py.
# Re-imported here so existing code that references these names from
# this module (e.g. ``_core.RemoteNode``) continues to work.
from .types import (  # noqa: F401
    RemoteNode,
    NodeConfigItem,
    NodeConfig,
    AgentConfigPayload,
    ConfigDeployment,
    AgentMessage,
)


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------

class Hub:
    def __init__(self, node_id: str = "local", node_name: str = "",
                 data_dir: str = ""):
        import os, socket
        self.node_id = node_id
        self.node_name = node_name or socket.gethostname()
        self.agents: dict[str, Agent] = {}
        self.remote_nodes: dict[str, RemoteNode] = {}
        self.messages: list[AgentMessage] = []
        self.config_deployments: dict[str, ConfigDeployment] = {}
        self._lock = threading.Lock()
        from .. import DEFAULT_DATA_DIR
        self._data_dir = data_dir or DEFAULT_DATA_DIR
        # Upstream hub (only set on remote-node portals): node will push
        # config changes back here so the admin hub stays in sync.
        self.upstream_hub_url: str = os.environ.get("TUDOU_UPSTREAM_HUB", "").rstrip("/")
        self.upstream_hub_secret: str = os.environ.get("TUDOU_UPSTREAM_SECRET", "")
        self._agents_file = os.path.join(self._data_dir, "agents.json")
        self._nodes_file = os.path.join(self._data_dir, "nodes.json")
        # SQLite database (primary store)
        try:
            from ..infra.database import init_database
            self._db = init_database(self._data_dir)
            self._db.migrate_from_json()  # 首次运行自动从 JSON 导入
        except Exception as _e:
            logger.warning("SQLite init failed, falling back to JSON: %s", _e)
            self._db = None
        self._load_agents()
        # ── V2 shadow backfill ────────────────────────────────────────
        # 2026-04-27: TudouClaw retired the "普通 vs 状态机 agent" UX
        # split. New agents always get a V2 shadow. Existing agents
        # created before this commit may have no V2 record — backfill
        # one for each so capability surfaces are uniform.
        try:
            self._backfill_v2_shadows()
        except Exception as _bf_err:
            logger.warning("V2 shadow backfill failed (non-fatal): %s", _bf_err)
        self._load_remote_nodes()
        # Node-scoped configurations
        self.node_configs: dict[str, NodeConfig] = {}
        self._node_configs_file = os.path.join(self._data_dir, "node_configs.json")
        self._load_node_configs()
        # Projects
        self.projects: dict[str, Project] = {}
        self._projects_file = os.path.join(self._data_dir, "projects.json")
        self._load_projects()
        # Sync project working directories to member agents (in case of stale state)
        self._sync_all_project_dirs()
        # Agent messages (inter-agent)
        self._load_messages()
        # ── Agent Supervisor (process isolation) ──
        from ..supervisor import AgentSupervisor
        self.supervisor = AgentSupervisor(
            data_dir=self._data_dir,
            get_agent_fn=lambda aid: self.agents.get(aid),
            save_fn=self._save_agents,
        )
        # Project / meeting chat uses _direct_chat (preserves agent
        # identity for MCP / skill bindings). Workflow engine sticks
        # with _workflow_chat because its "delegate" semantic is what
        # the workflow step orchestrator actually needs (spawn a
        # worker, run, discard).
        #
        # Design principle (stated by user 2026-04-21): "Agent作为一
        # 个对象，能力包装是固定的，meeting/project/channel 只是外部
        # 接入方式不同". So accessing the same agent via meeting or
        # project MUST use the same agent.id — delegate() forking a
        # child with a fresh uuid breaks MCP bindings and is wrong
        # for these surfaces.
        self.project_chat_engine = ProjectChatEngine(
            agent_chat_fn=self._direct_chat,
            agent_lookup_fn=lambda aid: self.agents.get(aid),
            save_fn=self._save_projects,
        )
        # Workflow engine — uses _workflow_chat (delegate) because it
        # specifically wants worker forking for each step.
        self.workflow_engine = WorkflowEngine(
            agent_chat_fn=self._workflow_chat
        )
        self.workflow_engine.set_data_dir(self._data_dir)
        self.workflow_engine.load()
        # 注册 step 完成回调 → 同步到 Project Task
        self.workflow_engine._on_step_complete = self._on_workflow_step_complete
        # Resume interrupted tasks from previous run
        self._resume_interrupted_tasks()
        # Expose this hub as the active hub so llm.py can route token usage
        # back to per-agent stats via the thread-local context.
        try:
            import sys as _sys
            _llm = _sys.modules.get("app.llm") or _sys.modules.get("..llm")
            if _llm is None:
                from .. import llm as _llm  # noqa: F401
            # __package__ is "app.hub"; llm lives one level up in "app"
            _parent_pkg = __package__.rsplit(".", 1)[0]
            _sys.modules[_parent_pkg + ".llm"]._active_hub = self
        except Exception as _e:
            logger.debug("set _active_hub failed: %s", _e)

        # ── Skill Registry ──
        try:
            from .. import skills as _skills_mod
            self.skill_registry = _skills_mod.init_registry(
                install_root=os.path.join(self._data_dir, "skills_installed"),
                persist_path=os.path.join(self._data_dir, "skills.json"),
                mcp_check=self._skill_mcp_check,
                mcp_invoker=self._skill_mcp_invoke,
                llm_invoker=self._skill_llm_invoke,
                escalation_check=self._skill_escalation_check,
                logger_fn=lambda m: logger.info("[skill] %s", m),
            )
            logger.info("Skill registry initialized: %d installed",
                         len(self.skill_registry.list_all()))
            # Auto-install builtin skills.
            #
            # Policy (see SkillRegistry.install_or_upgrade_from_directory):
            #   - same id already installed → idempotent no-op
            #   - older version installed   → upgrade to bundled
            #   - newer version installed   → keep user's copy
            #   - uncomparable versions     → keep existing, log warning
            #
            # This is the single place where the "bundled code wins on
            # upgrade but never clobbers a user's newer copy" decision
            # lives. Individual skill directories no longer need any
            # version-conflict handling.
            try:
                builtin_dir = os.path.join(os.path.dirname(__file__), "skills", "builtin")
                if os.path.isdir(builtin_dir):
                    # Recursive walk — a skill is any directory that
                    # contains a manifest.yaml. Lets us nest bundles
                    # under tudou-builtin/tool-bundles/<name>/ while
                    # still auto-installing them.
                    for root, dirs, files in os.walk(builtin_dir):
                        if "manifest.yaml" not in files:
                            continue
                        try:
                            self.skill_registry.install_or_upgrade_from_directory(
                                root, installed_by="builtin", policy="upgrade")
                        except Exception as _ie:
                            logger.warning(
                                "Auto-install skill %s failed: %s",
                                os.path.basename(root), _ie)
            except Exception as _ae:
                logger.debug("Builtin skill auto-install failed: %s", _ae)
        except Exception as _e:
            logger.warning("Skill registry init failed: %s", _e)
            self.skill_registry = None

        # ── Skill Store (Hub-level marketplace on top of registry) ──
        try:
            from .. import skill_store as _store_mod
            catalog_dirs = []
            # (1) builtin catalog ships with the code base (app/skills/builtin/)
            _app_dir = os.path.dirname(os.path.dirname(__file__))  # -> app/
            _builtin = os.path.join(_app_dir, "skills", "builtin")
            if os.path.isdir(_builtin):
                catalog_dirs.append(_builtin)
            # (1b) project-level data/skill_catalog (user-imported skills)
            _project_root = os.path.dirname(_app_dir)  # -> project root
            _project_catalog = os.path.join(_project_root, "data", "skill_catalog")
            if os.path.isdir(_project_catalog):
                catalog_dirs.append(_project_catalog)
            # (2) user-space catalog for third-party SKILL.md / manifest.yaml drops
            _user_catalog = os.path.join(self._data_dir, "skill_catalog")
            os.makedirs(_user_catalog, exist_ok=True)
            catalog_dirs.append(_user_catalog)
            # (3) SkillForge pending_skills directory (approved drafts become catalog entries)
            _pending = os.path.join(self._data_dir, "pending_skills")
            os.makedirs(_pending, exist_ok=True)
            catalog_dirs.append(_pending)
            # (4) extra catalogs from env (comma-separated)
            for extra in (os.environ.get("TUDOU_SKILL_CATALOG_DIRS", "") or "").split(","):
                extra = extra.strip()
                if extra and os.path.isdir(extra):
                    catalog_dirs.append(extra)

            allowed = [s.strip() for s in
                       (os.environ.get("TUDOU_SKILL_ALLOWED_SOURCES",
                                       "official,maintainer,community,agent,local") or "")
                       .split(",") if s.strip()]

            self.skill_store = _store_mod.init_store(
                catalog_dirs=catalog_dirs,
                annotations_dir=os.path.join(self._data_dir, "skill_annotations"),
                registry=self.skill_registry,
                allowed_sources=allowed,
            )
            logger.info("Skill store initialized: %d catalog entries across %d dirs",
                        len(self.skill_store.list_catalog(include_disallowed=True)),
                        len(self.skill_store.catalog_dirs))
        except Exception as _se:
            logger.warning("Skill store init failed: %s", _se)

        # ── Meetings + Standalone Tasks ──
        try:
            from .. import meeting as _meeting_mod
            self.meeting_registry = _meeting_mod.MeetingRegistry(
                persist_path=os.path.join(self._data_dir, "meetings.json"),
                data_dir=self._data_dir,
            )
            self.standalone_task_registry = _meeting_mod.StandaloneTaskRegistry(
                persist_path=os.path.join(self._data_dir, "standalone_tasks.json"),
            )
            logger.info("Meeting registry initialized: %d meetings, %d standalone tasks",
                        len(self.meeting_registry.list()),
                        len(self.standalone_task_registry.list()))
        except Exception as _me:
            logger.warning("Meeting registry init failed: %s", _me)
            self.meeting_registry = None
            self.standalone_task_registry = None

        # ── Heartbeat watchdog + downstream node ping ──
        # ── ConversationTask crash recovery ────────────────────────
        # Any task left in RUNNING at startup belongs to a previous
        # process that's now gone. Flip them to PAUSED so the UI shows
        # a "continue" affordance instead of a ghost "running" chip.
        try:
            from ..conversation_task import get_store as _get_ct_store
            flipped = _get_ct_store().mark_paused_if_running()
            if flipped:
                logger.info(
                    "ConversationTask recovery: %d running tasks flipped "
                    "to PAUSED (resumable)", flipped,
                )
        except Exception as _cre:
            logger.warning("ConversationTask recovery skipped: %s", _cre)

        self._heartbeat_stop = threading.Event()
        self._heartbeat_interval = float(
            os.environ.get("TUDOU_HEARTBEAT_INTERVAL", "15") or 15
        )
        self._heartbeat_timeout = float(
            os.environ.get("TUDOU_HEARTBEAT_TIMEOUT", "60") or 60
        )
        try:
            threading.Thread(target=self._heartbeat_loop,
                             name="hub-heartbeat", daemon=True).start()
            logger.info("Heartbeat loop started (interval=%.1fs, timeout=%.1fs)",
                        self._heartbeat_interval, self._heartbeat_timeout)
        except Exception as _he:
            logger.warning("Failed to start heartbeat loop: %s", _he)

        # ── Memory dream daemon (P0) ──────────────────────────────────
        # Daily memory maintenance at 03:00 local time. Keeps L3 facts
        # tidy: prunes "never accessed + low confidence", decays stale,
        # audits KB references. See app/core/memory_dream.py.
        # Disabled when TUDOU_DISABLE_DREAM=1 (test env).
        if os.environ.get("TUDOU_DISABLE_DREAM", "") not in ("1", "true", "yes"):
            try:
                threading.Thread(
                    target=self._memory_dream_loop,
                    name="hub-memory-dream", daemon=True,
                ).start()
                logger.info("Memory dream daemon started (daily at 03:00)")
            except Exception as _de:
                logger.warning("Failed to start dream daemon: %s", _de)

        # ── Preprocessor pre-warm ─────────────────────────────────────
        # Async fire-and-forget: for every agent that has a preprocessor
        # configured, trigger _build_static_system_prompt() in the
        # background so the LRU cache is hot before the user's first
        # chat. Without this, the first chat with any preprocessor-
        # enabled agent eats a 10-25s latency hit (small model on a 14K
        # prompt). Failures are silent; they're logged at DEBUG and
        # don't block hub startup.
        try:
            threading.Thread(
                target=self._preprocessor_prewarm,
                name="hub-preprocessor-prewarm",
                daemon=True,
            ).start()
        except Exception as _pwe:
            logger.debug("preprocessor pre-warm skip: %s", _pwe)

        # ── Shutdown hook: persist agent state on exit ──
        # Chat tasks run in daemon threads whose finally blocks are NOT
        # guaranteed to execute when the process exits.  This atexit
        # handler ensures the latest in-memory events/messages are
        # flushed to disk so chat history survives a restart.
        self._shutdown_done = False

        def _shutdown_save():
            if self._shutdown_done:
                return
            self._shutdown_done = True
            try:
                self.supervisor.shutdown()
            except Exception as _e:
                logger.debug("Supervisor shutdown: %s", _e)
            try:
                self._save_agents()
                logger.info("Shutdown: saved %d agents", len(self.agents))
            except Exception as _e:
                logger.warning("Shutdown save failed: %s", _e)

        atexit.register(_shutdown_save)

        # SIGTERM (docker stop, systemd, etc.) doesn't trigger atexit
        # by default — convert it to SystemExit so atexit handlers run.
        _prev_sigterm = signal.getsignal(signal.SIGTERM)

        def _sigterm_handler(signum, frame):
            _shutdown_save()
            # Re-raise so the process actually exits
            if callable(_prev_sigterm) and _prev_sigterm not in (
                    signal.SIG_DFL, signal.SIG_IGN):
                _prev_sigterm(signum, frame)
            raise SystemExit(0)

        try:
            signal.signal(signal.SIGTERM, _sigterm_handler)
        except (OSError, ValueError):
            pass  # Not main thread or signal not supported

        # ── Skill resync on startup ──
        # Every time the hub boots, replay all existing (skill, agent)
        # grants to re-copy the latest skill files into each agent's
        # workspace. This is how upstream changes to builtin skills
        # (e.g. new _pptx_helpers.py APIs) propagate to agents that
        # were granted the skill on a previous boot — users don't need
        # to manually "reinstall" via the UI.
        try:
            if self.skill_registry is not None:
                report = self.skill_registry.resync_all_granted_skills()
                logger.info(
                    "Startup skill resync: %d/%d grants synced to "
                    "agent workspaces (%d errors)",
                    report.get("synced", 0), report.get("total", 0),
                    len(report.get("errors", [])))
        except Exception as _re:
            logger.warning("Startup skill resync failed: %s", _re)

    def _preprocessor_prewarm(self) -> None:
        """Background pre-warm of preprocessor LRU cache for every agent
        that opted in. Runs once at hub startup. Each agent runs serially
        (small models on Mac saturate one core anyway; parallelism just
        thrashes the GPU). Best-effort: timeouts, network errors, etc.
        all log at DEBUG and continue with the next agent.
        """
        try:
            from ..preprocessing import bridge as _prep_bridge
        except Exception as _ie:
            logger.debug("preprocessor pre-warm: bridge import failed: %s", _ie)
            return
        # Collect agents needing pre-warm (snapshot to avoid race during scan)
        targets = []
        for aid, agent in (self.agents or {}).items():
            try:
                if not _prep_bridge.is_enabled(agent):
                    continue
                if "optimize_prompt" not in _prep_bridge.get_modes(agent):
                    continue
                targets.append((aid, agent))
            except Exception:
                continue
        if not targets:
            return
        logger.info(
            "preprocessor pre-warm: %d agent(s) eligible (will run serially)",
            len(targets),
        )
        for aid, agent in targets:
            try:
                t0 = time.time()
                # Force a rebuild that runs the preprocessor + caches result.
                # _build_static_system_prompt has its own internal cache check
                # so we don't re-invoke if hash already matches.
                _ = agent._build_static_system_prompt()
                elapsed = (time.time() - t0) * 1000
                logger.info(
                    "preprocessor pre-warm: agent=%s done in %.0fms",
                    aid, elapsed,
                )
            except Exception as _e:
                logger.debug(
                    "preprocessor pre-warm: agent=%s failed (non-fatal): %s",
                    aid, _e,
                )

    def _memory_dream_loop(self):
        """Daily L3 memory maintenance at 03:00 local time.

        Sleeps in chunks (max 1h per chunk) so a process exit doesn't
        leave a 23-hour-deep blocking sleep. Uses ``_heartbeat_stop``
        as the global "shutdown" signal — same convention as the
        heartbeat loop.

        Body: ``MemoryDream.dream_all()`` — runs consolidator on every
        agent, prunes orphaned L3 facts, audits KB. See app/core/memory_dream.py.
        """
        import datetime as _dt
        from ..core.memory_dream import get_memory_dream
        dream = get_memory_dream()
        # Bind hub for cleaner agent enumeration.
        try:
            if dream._hub is None:
                dream._hub = self
        except Exception:
            pass

        def _seconds_until_3am():
            now = _dt.datetime.now()
            target = now.replace(hour=3, minute=0, second=0, microsecond=0)
            if now >= target:
                target = target + _dt.timedelta(days=1)
            return max(60.0, (target - now).total_seconds())

        while not self._heartbeat_stop.is_set():
            wait = _seconds_until_3am()
            # Chunked wait so shutdown is responsive.
            while wait > 0 and not self._heartbeat_stop.is_set():
                step = min(3600.0, wait)
                if self._heartbeat_stop.wait(timeout=step):
                    return
                wait -= step
            if self._heartbeat_stop.is_set():
                return
            try:
                logger.info("Memory dream: starting nightly run")
                report = dream.dream_all(llm_call=None)
                logger.info(
                    "Memory dream: done — %d agents, %d facts deleted, "
                    "%d decayed, %d KB orphan candidates",
                    report.agents_processed, report.orphans_deleted,
                    report.orphans_decayed, len(report.kb_orphan_candidates),
                )
            except Exception as e:
                logger.warning("Memory dream nightly run failed: %s", e)

    def _heartbeat_loop(self):
        """Periodic loop:
        1) If running as a downstream node (TUDOU_UPSTREAM_HUB set), send a
           heartbeat POST to the upstream hub.
        2) If running as the hub, scan node_manager for stale nodes and fail
           them via handle_node_failure().
        """
        import urllib.request as _ur
        while not self._heartbeat_stop.is_set():
            try:
                # Outbound heartbeat from this node to upstream hub
                if self.upstream_hub_url:
                    try:
                        payload = json.dumps({
                            "node_id": self.node_id,
                            "name": self.node_name,
                            "ts": time.time(),
                            "agent_count": len(self.agents),
                        }).encode("utf-8")
                        req = _ur.Request(
                            f"{self.upstream_hub_url}/api/hub/heartbeat",
                            data=payload,
                            headers={"Content-Type": "application/json",
                                     "X-Hub-Secret": self.upstream_hub_secret},
                            method="POST",
                        )
                        _ur.urlopen(req, timeout=5)
                    except Exception as _ue:
                        logger.debug("upstream heartbeat failed: %s", _ue)

                # Inbound watchdog: detect stale downstream nodes
                try:
                    from ..infra.node_manager import get_node_manager
                    nm = get_node_manager()
                    if nm is not None and hasattr(nm, "check_health"):
                        unhealthy = nm.check_health(timeout=self._heartbeat_timeout)
                        for nid in unhealthy:
                            try:
                                orphaned = nm.handle_node_failure(nid)
                                logger.warning(
                                    "Heartbeat watchdog: node %s offline, %d orphaned agents",
                                    nid, len(orphaned))
                            except Exception as _fe:
                                logger.debug("handle_node_failure(%s) failed: %s", nid, _fe)
                except Exception as _ne:
                    logger.debug("watchdog check_health failed: %s", _ne)

                # Also age out remote_nodes table (hub.py-local)
                try:
                    now = time.time()
                    for rn in list(self.remote_nodes.values()):
                        if now - rn.last_seen > self._heartbeat_timeout * 2:
                            logger.info("Remote node %s last_seen %.0fs ago — flagging stale",
                                        rn.node_id, now - rn.last_seen)
                except Exception:
                    pass
                # ── Workflow scheduler tick (P2 #4) ──
                try:
                    eng = getattr(self, "workflow_engine", None)
                    if eng is not None and hasattr(eng, "tick_scheduler"):
                        fired = eng.tick_scheduler()
                        if fired:
                            logger.info("scheduler: fired %d workflow instance(s): %s",
                                        len(fired), fired)
                except Exception as _se:
                    logger.debug("workflow scheduler tick failed: %s", _se)

                # ── Self-growth tick: idle agents pick up background tasks ──
                try:
                    for ag in list(self.agents.values()):
                        try:
                            ag.tick_growth(min_interval=120.0)
                        except Exception as _ge:
                            logger.debug("tick_growth(%s) failed: %s",
                                         getattr(ag, "id", "?"), _ge)
                except Exception as _ge:
                    logger.debug("growth tick loop failed: %s", _ge)

                # ── Stuck-agent watchdog ──
                # If an agent is IDLE but has an ACTIVE ExecutionPlan with
                # pending/in-progress steps AND hasn't seen step-level
                # activity for a while, send it a "continue" nudge so it
                # doesn't sit on an unfinished task silently. Capped at 3
                # wakes per plan to avoid infinite loops.
                try:
                    for ag in list(self.agents.values()):
                        try:
                            self._maybe_wake_stuck_agent(ag)
                        except Exception as _we:
                            logger.debug(
                                "stuck-agent watchdog for %s failed: %s",
                                getattr(ag, "id", "?"), _we)
                        try:
                            self._maybe_reset_busy_stalled_agent(ag)
                        except Exception as _we:
                            logger.debug(
                                "busy-stall watchdog for %s failed: %s",
                                getattr(ag, "id", "?"), _we)
                except Exception as _we:
                    logger.debug("stuck-agent watchdog loop error: %s", _we)

                # ── Long-task auto-assignment tick ──
                # For each project, find sub-tasks whose deps are
                # satisfied + assigned_to is empty, and dispatch them
                # to idle agents matching role_hint. Cheap to run on
                # every heartbeat (skips projects with no sub-tasks).
                try:
                    from ..long_task import auto_assign_tick
                    auto_assign_tick(self)
                except Exception as _ae:
                    logger.debug("long_task auto_assign tick error: %s", _ae)
                # ── Long-task aggregator tick (P0 closure) ──
                # When all children of a parent task reach DONE,
                # combine their results onto the parent so the user
                # gets a complete deliverable without manual stitching.
                # Idempotent — skips already-aggregated parents.
                try:
                    from ..long_task import tick_aggregate
                    tick_aggregate(self)
                except Exception as _age:
                    logger.debug("long_task aggregate tick error: %s", _age)
            except Exception as _le:
                logger.debug("heartbeat loop iter error: %s", _le)
            self._heartbeat_stop.wait(self._heartbeat_interval)

    # ─────────── Skill 系统的 hub 侧 invoker / 检查器 ───────────

    def _skill_mcp_check(self, mcp_id: str, tools: list) -> tuple:
        """检查 MCP 是否可用且包含所需工具。供 SkillRegistry 在依赖检查时调用。"""
        try:
            from ..mcp.manager import get_mcp_manager
            mgr = get_mcp_manager()
            if mgr is None:
                return False, "mcp_manager not available"
            servers = mgr.list_servers() if hasattr(mgr, "list_servers") else []
            srv = None
            for s in servers:
                sid = s.get("id") if isinstance(s, dict) else getattr(s, "id", "")
                if sid == mcp_id:
                    srv = s
                    break
            if srv is None:
                return False, f"mcp not configured: {mcp_id}"
            return True, ""
        except Exception as e:
            return False, str(e)

    def _skill_mcp_invoke(self, mcp_id: str, tool: str, args: dict):
        """实际调用 MCP 工具。"""
        try:
            from ..mcp.manager import get_mcp_manager
            mgr = get_mcp_manager()
            if mgr and hasattr(mgr, "call_tool"):
                return mgr.call_tool(mcp_id, tool, args)
        except Exception as e:
            logger.error("MCP invoke failed for skill: %s.%s: %s",
                         mcp_id, tool, e)
            raise
        # 兜底：mcp_manager 还没接入则返回 stub
        logger.info("[skill->mcp stub] %s.%s(%s)", mcp_id, tool, args)
        return {"ok": True, "stub": True}

    def _skill_llm_invoke(self, prompt: str, model: str = "", **kw):
        """供 skill 内部调用 LLM 生成。"""
        try:
            from .. import llm as _llm
            return _llm.chat(prompt=prompt, model=model or None)
        except Exception as e:
            logger.error("Skill LLM invoke failed: %s", e)
            return ""

    def _skill_escalation_check(self, skill_id: str, agent_id: str,
                                  mcp_id: str, tool: str, args: dict) -> str:
        """
        运行时审批策略检查。返回: "allow" | "deny" | "pending"。
        默认放行；admin 可在 auth.tool_policy.skill_escalation 配置升级规则。
        命中规则后会创建一条 PendingApproval 并阻塞等待管理员决策。
        """
        try:
            from ..auth import get_auth
            auth = get_auth()
            if not hasattr(auth.tool_policy, "check_skill_call"):
                return "allow"
            # 解析 skill_name + agent_role
            skill_name = skill_id
            try:
                inst = self.skill_registry.get(skill_id) if self.skill_registry else None
                if inst is not None:
                    skill_name = inst.manifest.name
            except Exception:
                pass
            agent_role = ""
            try:
                ag = self.get_agent(agent_id)
                if ag is not None:
                    agent_role = getattr(ag, "role", "") or ""
            except Exception:
                pass

            decision, reason = auth.tool_policy.check_skill_call(
                skill_id=skill_id, skill_name=skill_name,
                agent_id=agent_id, agent_role=agent_role,
                mcp_id=mcp_id, tool_name=tool,
            )
            if decision == "allow":
                return "allow"
            # needs_approval → 创建审批请求并阻塞等待
            approval = auth.tool_policy.request_approval(
                tool_name=f"skill:{skill_name}::{mcp_id}.{tool}",
                arguments=dict(args or {}),
                agent_id=agent_id,
                agent_name=agent_role or agent_id,
                reason=reason,
            )
            result = auth.tool_policy.wait_for_approval(approval)
            return "allow" if result == "approved" else "deny"
        except Exception as e:
            logger.debug("escalation_check failed (allowing): %s", e)
            return "allow"

    def _resume_interrupted_tasks(self):
        """Reset in_progress tasks to todo and log recovery info.

        After a restart, any task that was in_progress is stale (the thread
        executing it is gone). We reset them to TODO so the task system can
        re-queue them on the next tick.
        """
        from ..agent import TaskStatus
        resumed_count = 0
        for agent in self.agents.values():
            for task in agent.tasks:
                if task.status == TaskStatus.IN_PROGRESS:
                    task.status = TaskStatus.TODO
                    task.notified = False  # Allow re-notification
                    task.updated_at = time.time()
                    resumed_count += 1
        if resumed_count > 0:
            logger.info("Resumed %d interrupted tasks (reset to TODO)",
                        resumed_count)
            self._save_agents()

        # ── Reset orphaned step-level checkpoints on project tasks ──
        # Steps left in `in_progress` after a crash should be re-run; mark them
        # back to `pending` so next_pending_step() picks them up.
        try:
            stale_steps = 0
            for proj in self.projects.values():
                for ptask in proj.tasks:
                    if not getattr(ptask, "steps", None):
                        continue
                    for s in ptask.steps:
                        if getattr(s, "status", "") == "in_progress":
                            s.status = "pending"
                            s.started_at = 0.0
                            stale_steps += 1
                    # If task itself was IN_PROGRESS but has unfinished steps,
                    # bump it back to TODO so the chat engine reschedules it —
                    # UNLESS it's parked at a manual_review step. Those should
                    # remain IN_PROGRESS so the UI shows "等待人工审核" and the
                    # /task-step-review endpoint resumes them on approval.
                    from ..project import ProjectTaskStatus
                    if (ptask.status == ProjectTaskStatus.IN_PROGRESS
                            and not ptask.all_steps_done()
                            and not ptask.has_awaiting_review()):
                        ptask.status = ProjectTaskStatus.TODO
                        ptask.updated_at = time.time()
            if stale_steps > 0:
                logger.info("Reset %d in-progress task steps to pending",
                            stale_steps)
                self._save_projects()
        except Exception as _se:
            logger.debug("step checkpoint reset failed: %s", _se)

        # Also reset pending/delivered messages that were never completed
        stale_msg_count = 0
        for msg in self.messages:
            if msg.status in ("pending", "delivered"):
                stale_msg_count += 1
        if stale_msg_count > 0:
            logger.info("Found %d stale messages from previous run "
                        "(status=pending/delivered)", stale_msg_count)

    def _workflow_chat(self, agent_id: str, message) -> str:
        """Workflow engine 调用的 agent chat 接口。支持 str 或 list[dict] (multimodal)。

        When agent isolation is enabled (TUDOU_AGENT_ISOLATION=1), the call
        is routed to an isolated worker subprocess via the Supervisor.

        Uses delegate() on purpose — workflow steps are short-lived
        worker invocations where a fresh child context is appropriate.
        For meeting / project chat use _direct_chat instead, which
        preserves the agent's primary identity.
        """
        # Local agent — route through supervisor (handles both isolated
        # and in-process paths transparently)
        if agent_id in self.agents:
            return self.supervisor.delegate(agent_id, message,
                                            from_agent="workflow")
        # Try remote (remote only supports string)
        node = self.find_agent_node(agent_id)
        if node:
            _msg_str = message if isinstance(message, str) else (
                " ".join(p.get("text", "") for p in message
                         if isinstance(p, dict) and p.get("type") == "text")
                or str(message)
            )
            return self.proxy_chat_sync(agent_id, node, _msg_str)
        raise ValueError(f"Agent not found: {agent_id}")

    def _direct_chat(self, agent_id: str, message,
                     context_id: str = "solo",
                     source: str = "admin") -> str:
        """Call an agent's native chat() path directly, preserving identity.

        Why this exists (contrast with _workflow_chat):

            _workflow_chat → supervisor.delegate(agent_id, ...)
                           → agent.delegate(content, from_agent=...)
                           → spawns a CHILD Agent with a fresh uuid
                           → child_agent.chat(prompt)
                           → tools see _caller_agent_id = <child_uuid>

            MCP router then looks up
            get_agent_effective_mcps(node_id, <child_uuid>) which
            returns nothing because the child was never registered in
            the MCP manager's binding table. Net effect: in meeting /
            project chat the agent hallucinates "not bound to agent"
            errors even when the primary agent clearly has the MCP
            bound. Same story for skill grants, experience library,
            etc. — anything keyed by agent_id.

        Meeting / project / channel are just different WAYS the user
        talks to the SAME agent. The agent's capability bundle is
        stable; only the input/output plumbing changes. So those
        surfaces MUST call the agent's own chat() and keep the
        original agent_id all the way through.

        ``context_id`` scopes the agent's message history to one of:
        ``solo`` (default — direct admin/user chat), ``project:{pid}``,
        or ``meeting:{mid}``. Each scope has its own conversation; an
        upload in solo chat NEVER leaks into a new project's first turn.

        Supports str or list[dict] (multimodal) messages, same as
        _workflow_chat. Remote path unchanged.
        """
        agent = self.agents.get(agent_id)
        if agent is not None:
            return agent.chat(message, context_id=context_id, source=source)
        # Remote — same as _workflow_chat's remote branch.
        node = self.find_agent_node(agent_id)
        if node:
            _msg_str = message if isinstance(message, str) else (
                " ".join(p.get("text", "") for p in message
                         if isinstance(p, dict) and p.get("type") == "text")
                or str(message)
            )
            return self.proxy_chat_sync(agent_id, node, _msg_str)
        raise ValueError(f"Agent not found: {agent_id}")

    # ---- Agent persistence ----

    def _load_agents(self):
        """Load agents from SQLite (primary) or JSON (fallback)."""
        import os
        # 优先从 SQLite 加载
        if self._db and self._db.count("agents") > 0:
            try:
                for d in self._db.load_agents():
                    agent = Agent.from_persist_dict(d)
                    self.agents[agent.id] = agent
                logger.info("Loaded %d agents from SQLite", len(self.agents))
                self._auto_migrate_role_defaults()
                return
            except Exception as e:
                logger.warning("SQLite agent load failed, trying JSON: %s", e)
        # JSON fallback
        if not os.path.exists(self._agents_file):
            return
        try:
            with open(self._agents_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for d in data.get("agents", []):
                agent = Agent.from_persist_dict(d)
                self.agents[agent.id] = agent
        except Exception as e:
            import traceback
            traceback.print_exc()
        self._auto_migrate_role_defaults()

    def _auto_migrate_role_defaults(self) -> None:
        """Back-fill role_defaults onto existing agents at load time.

        When a new prompt pack is added to ``ROLE_DEFAULTS`` (e.g.
        ``action-first``), agents that were created BEFORE the addition
        won't have it in ``bound_prompt_packs`` — ``create_agent`` only
        resolves role defaults at creation time, not retroactively.

        This performs a union merge: for every loaded agent, resolve what
        its role SHOULD have today and append any missing IDs to the
        persisted ``granted_skills`` / ``bound_prompt_packs``.  User
        customizations are preserved — we never REMOVE anything.

        Runs once per server start after all agents are loaded.  Failures
        are non-fatal (logged).
        """
        try:
            from ..core.role_defaults import resolve_role_default_ids
            from ..core.prompt_enhancer import get_prompt_pack_registry
        except Exception as _imp:
            logger.debug("auto-migrate: imports failed: %s", _imp)
            return

        try:
            pp_reg = get_prompt_pack_registry()
        except Exception:
            pp_reg = None
        skill_registry = getattr(self, "skill_registry", None)

        changed = 0
        added_skills_total = 0
        added_packs_total = 0
        for agent in list(self.agents.values()):
            try:
                want_skills, want_packs = resolve_role_default_ids(
                    agent.role or "", skill_registry, pp_reg)
            except Exception as _re:
                logger.debug("resolve role_defaults failed for %s: %s",
                             agent.name, _re)
                continue

            added_here = False
            cur_skills = list(getattr(agent, "granted_skills", []) or [])
            for sid in want_skills:
                if sid not in cur_skills:
                    cur_skills.append(sid)
                    added_skills_total += 1
                    added_here = True
                    if skill_registry is not None:
                        try:
                            skill_registry.grant(sid, agent.id)
                        except Exception:
                            pass
            if added_here:
                agent.granted_skills = cur_skills

            cur_packs = list(getattr(agent, "bound_prompt_packs", []) or [])
            packs_changed = False
            for pid in want_packs:
                if pid not in cur_packs:
                    cur_packs.append(pid)
                    added_packs_total += 1
                    packs_changed = True
            if packs_changed:
                agent.bound_prompt_packs = cur_packs
                added_here = True

            if added_here:
                changed += 1

        if changed:
            logger.info(
                "Role-defaults auto-migrate: updated %d agents "
                "(+%d skills, +%d packs)",
                changed, added_skills_total, added_packs_total,
            )
            # Persist so the next restart doesn't redo the work.
            try:
                self._save_agents()
            except Exception as _se:
                logger.debug("auto-migrate save failed: %s", _se)

    def _backfill_v2_shadows(self) -> None:
        """Ensure every loaded V1 agent has a corresponding V2 store
        record. One-time on startup; idempotent (skips agents whose
        shadow already exists). Mirrors the inline registration block
        in ``app.api.routers.agents.create_agent``.

        Failure for one agent is logged + skipped — the rest still get
        backfilled.
        """
        if not self.agents:
            return
        try:
            from ..v2.agent.agent_v2 import AgentV2, Capabilities
            from ..v2.agent.llm_slots import slots_from_v1_agent
            from ..v2.core.task_store import get_store as _v2_store
        except Exception as _ie:
            logger.debug("V2 modules unavailable, skipping backfill: %s", _ie)
            return
        store = _v2_store()
        backfilled = 0
        for agent in self.agents.values():
            try:
                if store.get_agent(agent.id) is not None:
                    continue  # already has shadow
                _slots = slots_from_v1_agent(agent).to_dict()
                v2_caps = Capabilities(
                    skills=list(getattr(agent, "granted_skills", []) or []),
                    mcps=[],
                    tools=[],
                    llm_tier=str(getattr(agent.profile, "llm_tier", "") or "default"),
                    denied_tools=[],
                    llm_slots=_slots,
                )
                v2_agent = AgentV2.create(
                    id=agent.id,
                    name=agent.name,
                    role=agent.role,
                    v1_agent_id=agent.id,
                    capabilities=v2_caps,
                    task_template_ids=[],
                    working_directory=getattr(agent, "working_dir", "") or "",
                )
                store.save_agent(v2_agent)
                backfilled += 1
            except Exception as _e:
                logger.warning(
                    "V2 shadow backfill failed for agent %s: %s",
                    agent.id[:8], _e,
                )
        if backfilled > 0:
            logger.info(
                "V2 shadow backfill: registered %d legacy agent(s)",
                backfilled,
            )

    def _save_agents(self):
        """Persist agents to SQLite (primary) + JSON (backup) + workspace."""
        import os
        os.makedirs(self._data_dir, exist_ok=True)
        # SQLite — 逐个 upsert
        if self._db:
            try:
                for a in self.agents.values():
                    self._db.save_agent(a.to_persist_dict())
            except Exception as e:
                logger.warning("SQLite agent save failed: %s", e)
        # JSON backup (保留兼容性)
        data = {"agents": [a.to_persist_dict()
                           for a in self.agents.values()]}
        try:
            with open(self._agents_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("Failed to save agents to %s: %s", self._agents_file, e)
        # Also save each agent to its own workspace
        for agent in self.agents.values():
            self._save_agent_workspace(agent)

    def sync_all_agent_mcps(self):
        """Sync MCP bindings from MCPManager into all loaded agents.

        Must be called AFTER MCPManager is initialized (run_portal startup).
        Fixes the issue where agents loaded from disk have stale/empty MCP lists.
        """
        try:
            from ..mcp.manager import get_mcp_manager
            mcp_mgr = get_mcp_manager()
            for agent in self.agents.values():
                try:
                    mcp_mgr.sync_agent_mcps(agent)
                except Exception as e:
                    logger.debug("Failed to sync MCP for agent %s: %s", agent.id, e)
        except Exception as e:
            logger.warning("Failed to get MCP manager for sync: %s", e)

    def _save_agent_workspace(self, agent):
        """Save individual agent to ~/.tudou_claw/workspaces/{id}/agent.json"""
        import os
        ws = os.path.join(self._data_dir, "workspaces", agent.id)
        os.makedirs(ws, exist_ok=True)
        try:
            af = os.path.join(ws, "agent.json")
            tmp = af + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(agent.to_persist_dict(), f,
                          ensure_ascii=False, indent=2)
            os.replace(tmp, af)
        except Exception:
            pass

    # ---- Remote node persistence ----

    def _load_remote_nodes(self):
        """Load remote nodes from SQLite (primary) or JSON (fallback)."""
        import os
        if self._db and self._db.count("nodes") > 0:
            try:
                for d in self._db.load_nodes():
                    node = RemoteNode(
                        node_id=d.get("node_id", ""),
                        name=d.get("name", ""),
                        url=d.get("url", ""),
                        agents=d.get("agents", []),
                        last_seen=d.get("last_seen", 0),
                        status=d.get("status", "offline"),
                        secret=d.get("secret", ""),
                    )
                    if node.node_id:
                        self.remote_nodes[node.node_id] = node
                logger.info("Loaded %d remote nodes from SQLite",
                            len(self.remote_nodes))
                return
            except Exception as e:
                logger.warning("SQLite node load failed, trying JSON: %s", e)
        if not os.path.exists(self._nodes_file):
            return
        try:
            with open(self._nodes_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for d in data.get("nodes", []):
                node = RemoteNode(
                    node_id=d.get("node_id", ""),
                    name=d.get("name", ""),
                    url=d.get("url", ""),
                    agents=d.get("agents", []),
                    last_seen=d.get("last_seen", 0),
                    status=d.get("status", "offline"),
                    secret=d.get("secret", ""),
                )
                if node.node_id:
                    self.remote_nodes[node.node_id] = node
        except Exception:
            pass

    def _save_remote_nodes(self):
        """Persist remote node configs to SQLite + JSON backup + node.md."""
        import os
        os.makedirs(self._data_dir, exist_ok=True)
        data = {"nodes": [
            {
                "node_id": n.node_id,
                "name": n.name,
                "url": n.url,
                "agents": n.agents,
                "last_seen": n.last_seen,
                "status": n.status,
                "secret": n.secret,
            }
            for n in self.remote_nodes.values()
        ]}
        # SQLite primary
        if self._db:
            try:
                for nd in data["nodes"]:
                    self._db.save_node(nd)
            except Exception as e:
                logger.warning("SQLite node save failed: %s", e)
        # Also export Node.md
        try:
            import datetime
            lines = ["# Tudou Claw — Nodes", "",
                     f"Updated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
            lines += [f"## Local Node: {self.node_id}", f"Name: {self.node_name}", ""]
            if self.agents:
                lines.append("### Local Agents")
                for a in self.agents.values():
                    lines.append(f"- **{a.name}** ({a.role}) id={a.id}")
                lines.append("")
            for n in self.remote_nodes.values():
                lines += [f"## Remote Node: {n.name}",
                          f"- ID: {n.node_id}", f"- URL: {n.url}",
                          f"- Status: {n.status}", ""]
                if n.agents:
                    lines.append("### Agents")
                    for ag in n.agents:
                        name = ag.get("name", "?")
                        role = ag.get("role", "?")
                        lines.append(f"- **{name}** ({role})")
                    lines.append("")
            with open(os.path.join(self._data_dir, "Node.md"), "w",
                      encoding="utf-8") as f:
                f.write("\n".join(lines))
        except Exception:
            pass
        try:
            with open(self._nodes_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---- Node config persistence ----

    def _load_node_configs(self):
        """Load per-node configurations from SQLite (primary) or JSON."""
        import os
        if self._db and self._db.count("node_configs") > 0:
            try:
                rows = self._db.load_node_configs()
                # 按 node_id 聚合
                by_node: dict[str, list[dict]] = {}
                for r in rows:
                    by_node.setdefault(r.get("node_id", ""), []).append(r)
                for nid, items in by_node.items():
                    nc = NodeConfig(node_id=nid)
                    for it in items:
                        nc.set_item(
                            key=it.get("key", ""),
                            value=it.get("value", ""),
                            description=it.get("description", ""),
                            category=it.get("category", "general"),
                            is_secret=bool(it.get("is_secret", False)),
                            created_by=it.get("created_by", "system"),
                        )
                    self.node_configs[nid] = nc
                logger.info("Loaded node configs for %d nodes from SQLite",
                            len(self.node_configs))
                return
            except Exception as e:
                logger.warning("SQLite node_config load failed: %s", e)
        if not os.path.exists(self._node_configs_file):
            return
        try:
            with open(self._node_configs_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for d in data.get("configs", []):
                nc = NodeConfig.from_dict(d)
                if nc.node_id:
                    self.node_configs[nc.node_id] = nc
            logger.info("Loaded node configs for %d nodes", len(self.node_configs))
        except Exception:
            import traceback
            traceback.print_exc()

    def _save_node_configs(self):
        """Persist per-node configurations to SQLite + JSON backup."""
        import os
        os.makedirs(self._data_dir, exist_ok=True)
        # SQLite primary
        if self._db:
            try:
                for nc in self.node_configs.values():
                    for item in nc.items.values():
                        self._db.save_node_config(
                            nc.node_id, item.key, item.value,
                            item.category, item.is_secret,
                            item.to_dict() if hasattr(item, 'to_dict') else {},
                        )
            except Exception as e:
                logger.warning("SQLite node_config save failed: %s", e)
        # JSON backup
        data = {"configs": [nc.to_dict(mask=False)
                            for nc in self.node_configs.values()]}
        try:
            with open(self._node_configs_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            import traceback
            traceback.print_exc()

    # ---- Node config management ----

    def set_node_config_item(self, node_id: str, key: str, value: str,
                             description: str = "", category: str = "general",
                             is_secret: bool = False,
                             created_by: str = "admin") -> NodeConfigItem:
        """Set or update a config item for a specific node."""
        if node_id not in self.node_configs:
            self.node_configs[node_id] = NodeConfig(node_id=node_id)
        item = self.node_configs[node_id].set_item(
            key=key, value=value, description=description,
            category=category, is_secret=is_secret, created_by=created_by)
        self._save_node_configs()
        logger.info("Set node config [%s] %s (secret=%s) by %s",
                     node_id, key, is_secret, created_by)
        # Push upstream if configured (remote-node mode)
        self._push_node_config_upstream(node_id)
        return item

    def _push_node_config_upstream(self, node_id: str) -> None:
        """If an upstream hub is configured, push this node's config to it."""
        if not self.upstream_hub_url:
            return
        nc = self.node_configs.get(node_id)
        if not nc:
            return
        try:
            headers = {"Content-Type": "application/json"}
            if self.upstream_hub_secret:
                headers["X-Claw-Secret"] = self.upstream_hub_secret
            payload = {
                "node_id": node_id,
                "items": {k: v.to_dict(mask=False) for k, v in nc.items.items()},
            }
            # Fire-and-forget in background so callers aren't blocked
            def _push():
                try:
                    http_requests.post(
                        f"{self.upstream_hub_url}/api/hub/apply-node-config",
                        headers=headers, json=payload, timeout=10)
                    logger.info("Pushed node [%s] config upstream to %s",
                                node_id, self.upstream_hub_url)
                except Exception as ex:
                    logger.warning("Upstream push failed: %s", ex)
            threading.Thread(target=_push, daemon=True).start()
        except Exception as e:
            logger.warning("Upstream push prep failed: %s", e)

    def get_node_config(self, node_id: str, mask: bool = True) -> dict:
        """Get all config items for a node. Returns dict with masked values by default."""
        nc = self.node_configs.get(node_id)
        if not nc:
            return {"node_id": node_id, "items": {}}
        return nc.to_dict(mask=mask)

    def get_node_config_item(self, node_id: str, key: str,
                              mask: bool = True) -> dict | None:
        """Get a single config item for a node."""
        nc = self.node_configs.get(node_id)
        if not nc:
            return None
        item = nc.get_item(key)
        if not item:
            return None
        return item.to_dict(mask=mask)

    def delete_node_config_item(self, node_id: str, key: str) -> bool:
        """Delete a config item from a node."""
        nc = self.node_configs.get(node_id)
        if not nc:
            return False
        result = nc.delete_item(key)
        if result:
            self._save_node_configs()
            logger.info("Deleted node config [%s] %s", node_id, key)
            self._push_node_config_upstream(node_id)
        return result

    def list_all_node_configs(self, mask: bool = True) -> list[dict]:
        """List configs for all nodes (admin overview)."""
        result = []
        # Include all known nodes (remote + local)
        all_node_ids = set(self.node_configs.keys())
        all_node_ids.add("local")
        for nid in self.remote_nodes:
            all_node_ids.add(nid)
        for nid in sorted(all_node_ids):
            nc = self.node_configs.get(nid)
            if nc:
                d = nc.to_dict(mask=mask)
            else:
                d = {"node_id": nid, "items": {}}
            # Add node name for display
            if nid == "local":
                d["node_name"] = self.node_name
            else:
                rn = self.remote_nodes.get(nid)
                d["node_name"] = rn.name if rn else nid
            d["item_count"] = len(d.get("items", {}))
            result.append(d)
        return result

    def sync_node_config(self, node_id: str) -> dict:
        """Push all config items to a remote node. Returns sync result."""
        nc = self.node_configs.get(node_id)
        if not nc:
            return {"ok": False, "error": "No config for this node"}
        node = self.remote_nodes.get(node_id)
        if not node:
            if node_id == "local":
                # Local node: just mark as synced
                for item in nc.items.values():
                    item.synced = True
                    item.synced_at = time.time()
                self._save_node_configs()
                return {"ok": True, "synced": len(nc.items)}
            return {"ok": False, "error": "Node not found"}
        # Push to remote node via HTTP
        try:
            headers = {"Content-Type": "application/json"}
            if node.secret:
                headers["X-Claw-Secret"] = node.secret
            payload = {
                "node_id": node_id,
                "items": {k: v.to_dict(mask=False) for k, v in nc.items.items()},
            }
            resp = http_requests.post(
                f"{node.url}/api/hub/apply-node-config",
                headers=headers,
                json=payload,
                timeout=15,
            )
            if resp.status_code == 200:
                now = time.time()
                for item in nc.items.values():
                    item.synced = True
                    item.synced_at = now
                self._save_node_configs()
                logger.info("Synced %d config items to node [%s]",
                            len(nc.items), node_id)
                return {"ok": True, "synced": len(nc.items)}
            else:
                logger.error("Sync to node [%s] failed: %s %s",
                             node_id, resp.status_code, resp.text[:200])
                return {"ok": False, "error": f"HTTP {resp.status_code}"}
        except Exception as e:
            logger.error("Sync to node [%s] exception: %s", node_id, e)
            return {"ok": False, "error": str(e)}

    def apply_received_node_config(self, payload: dict) -> dict:
        """Called on a node to apply config pushed from Hub."""
        node_id = payload.get("node_id", "local")
        items = payload.get("items", {})
        if node_id not in self.node_configs:
            self.node_configs[node_id] = NodeConfig(node_id=node_id)
        nc = self.node_configs[node_id]
        applied = 0
        for key, item_dict in items.items():
            nc.items[key] = NodeConfigItem.from_dict(item_dict)
            nc.items[key].synced = True
            nc.items[key].synced_at = time.time()
            applied += 1
        self._save_node_configs()
        logger.info("Applied %d config items from Hub for node [%s]",
                     applied, node_id)
        return {"ok": True, "applied": applied}

    # ---- Project persistence ----

    def _load_projects(self):
        import os
        if self._db and self._db.count("projects") > 0:
            try:
                for d in self._db.load_projects():
                    proj = Project.from_persist_dict(d)
                    self.projects[proj.id] = proj
                logger.info("Loaded %d projects from SQLite",
                            len(self.projects))
                return
            except Exception as e:
                logger.warning("SQLite project load failed: %s", e)
        if not os.path.exists(self._projects_file):
            return
        try:
            with open(self._projects_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for d in data.get("projects", []):
                proj = Project.from_persist_dict(d)
                self.projects[proj.id] = proj
        except Exception:
            import traceback
            traceback.print_exc()

    def _save_projects(self):
        import os
        os.makedirs(self._data_dir, exist_ok=True)
        # SQLite primary
        if self._db:
            try:
                for p in self.projects.values():
                    self._db.save_project(p.to_persist_dict())
            except Exception as e:
                logger.warning("SQLite project save failed: %s", e)
        # JSON backup
        data = {"projects": [p.to_persist_dict()
                              for p in self.projects.values()]}
        try:
            with open(self._projects_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        # Also export each project as Markdown + per-agent context
        agent_lookup = lambda aid: self.agents.get(aid)
        for proj in self.projects.values():
            try:
                proj.save_markdown(self._data_dir,
                                   agent_lookup=agent_lookup)
            except Exception:
                pass

    # ---- Project management ----

    def create_project(self, name: str, description: str = "",
                        member_configs: list[dict] | None = None,
                        working_directory: str = "",
                        node_id: str = "local",
                        workflow_id: str = "",
                        step_assignments: list[dict] | None = None) -> Project:
        import os as _os
        from ..agent import Agent as _Agent
        proj = Project(name=name, description=description,
                      working_directory=working_directory, node_id=node_id)

        # Canonical project working directory = the project's shared workspace
        # under ~/.tudou_claw/workspaces/shared/<project_id>/. When the caller
        # didn't specify one, we fill it in so the UI always shows a real path
        # and every member knows where deliverables live.
        if not proj.working_directory:
            proj.working_directory = _Agent.get_shared_workspace_path(proj.id)

        # Create the shared directory on disk.
        try:
            _os.makedirs(_Agent.get_shared_workspace_path(proj.id),
                         exist_ok=True)
        except OSError:
            pass

        if member_configs:
            for mc in member_configs:
                agent_id = mc.get("agent_id", "")
                proj.add_member(agent_id,
                                mc.get("responsibility", ""))
                # Hook every member's shared_workspace up at creation time
                # (was previously conditional on user-supplied working_directory).
                if agent_id:
                    self._sync_agent_to_project_dir(
                        agent_id, proj.working_directory,
                        project_id=proj.id, project_name=proj.name)

        # Workflow 绑定
        if workflow_id:
            self._bind_workflow_to_project(proj, workflow_id,
                                           step_assignments or [])

        with self._lock:
            self.projects[proj.id] = proj
        self._save_projects()
        self._save_agents()
        return proj

    def _bind_workflow_to_project(self, proj: Project, workflow_id: str,
                                   step_assignments: list[dict]):
        """查找 WorkflowTemplate 并绑定到项目，自动生成任务。"""
        tmpl = self.workflow_engine.get_template(workflow_id)
        if not tmpl:
            log.warning("bind_workflow: template %s not found", workflow_id)
            return
        proj.bind_workflow(tmpl.to_dict(), step_assignments)
        # 同步分配的 Agent 到 project workspace
        if proj.working_directory:
            for sa in step_assignments:
                aid = sa.get("agent_id", "")
                if aid:
                    self._sync_agent_to_project_dir(
                        aid, proj.working_directory,
                        project_id=proj.id, project_name=proj.name)

    def _on_workflow_step_complete(self, template_id: str, step_index: int,
                                    step_id: str, status: str):
        """Workflow step 完成/失败回调 → 同步到绑定了该 workflow 的 Project Task + 记忆整理。"""
        from ..project import ProjectTaskStatus
        status_map = {"done": ProjectTaskStatus.DONE,
                      "blocked": ProjectTaskStatus.BLOCKED}
        new_status = status_map.get(status)
        if not new_status:
            return
        agent_id_for_consolidate = ""
        with self._lock:
            for proj in self.projects.values():
                if proj.workflow_binding.workflow_id != template_id:
                    continue
                # 查找对应的 [WF Step N] 任务
                step_num = step_index + 1
                prefix = f"[WF Step {step_num}]"
                for task in proj.tasks:
                    if task.title.startswith(prefix):
                        task.status = new_status
                        proj.updated_at = __import__('time').time()
                        agent_id_for_consolidate = task.assigned_to
                        break
        self._save_projects()

        # Step 完成后触发该 Agent 的记忆整理（归并 plan→done）
        if status == "done" and agent_id_for_consolidate:
            agent = self.get_agent(agent_id_for_consolidate)
            if agent:
                try:
                    consolidator = agent._get_memory_consolidator()
                    if consolidator:
                        report = consolidator.consolidate(
                            agent_id=agent_id_for_consolidate, force=True)
                        if not report.get("skipped") and report.get("plans_resolved", 0) > 0:
                            agent.history_log.add(
                                "consolidate",
                                f"[Consolidate] Workflow step 完成后记忆整理: "
                                f"plan→done={report['plans_resolved']}")
                except Exception as e:
                    log.debug("Post-workflow consolidate failed: %s", e)

            # Auto-progress: 触发下一个步骤的 Agent
            for proj in self.projects.values():
                if proj.workflow_binding.workflow_id != template_id:
                    continue
                step_num = step_index + 1
                prefix = f"[WF Step {step_num}]"
                completed_task = None
                for task in proj.tasks:
                    if task.title.startswith(prefix):
                        completed_task = task
                        break
                if completed_task:
                    try:
                        self.project_chat_engine._auto_progress_next_step(
                            proj, completed_task)
                    except Exception as e:
                        logger.warning("WF auto-progress failed: %s", e)

    def _sync_agent_to_project_dir(self, agent_id: str, project_dir: str,
                                    project_id: str = "", project_name: str = ""):
        """Sync a project's shared workspace to an agent.

        Directory layout:
          ~/.tudou_claw/workspaces/agents/{agent_id}/   ← private (working_dir)
          ~/.tudou_claw/workspaces/shared/{project_id}/ ← shared (shared_workspace)

        Does NOT overwrite working_dir — that stays as the agent's private
        workspace. The agent's system prompt will tell it which directory to
        use for project tasks vs personal tasks.
        """
        agent = self.get_agent(agent_id)
        if not agent:
            return
        import os
        from ..agent import Agent

        # Compute the canonical shared workspace path under the standard root
        if project_id:
            shared_dir = Agent.get_shared_workspace_path(project_id)
        else:
            shared_dir = project_dir  # fallback if no project_id

        # ── DEPRECATED: don't write to agent.shared_workspace anymore
        # (2026-04-29). The shared workspace now derives from the active
        # context per turn — see Agent.get_active_shared_workspace().
        # An agent in N projects + M meetings can no longer be
        # represented by a single field. The path for THIS project
        # is still computed (shared_dir above) and the canonical
        # convention is in get_shared_workspace_path(project_id), so
        # nothing downstream loses data. Field write removed.
        # Set project identity so prompt context is aware (still
        # needed for the legacy `project_id` agent field — but
        # multi-project agents will ALSO need a list eventually).
        if project_id:
            agent.project_id = project_id
        if project_name:
            agent.project_name = project_name
        # Mark routing context: all deliverables must go to shared_dir.
        agent.context_type = "project"
        # Ensure the shared directory exists (skip if path is invalid)
        try:
            os.makedirs(shared_dir, exist_ok=True)
        except OSError:
            pass
        # Re-create workspace symlink (workspace/shared → shared_dir)
        try:
            agent._ensure_workspace_layout()
        except Exception:
            pass
        # Invalidate cached system prompt so workspace context is refreshed
        agent._cached_static_prompt = ""
        agent._static_prompt_hash = ""

    def _sync_all_project_dirs(self):
        """On startup, ensure every project member's shared_workspace is set correctly.

        The shared workspace is always at ~/.tudou_claw/workspaces/shared/{project_id}/
        regardless of what the project's working_directory field says.
        """
        synced = 0
        for proj in self.projects.values():
            for member in proj.members:
                agent = self.agents.get(member.agent_id)
                # Shared workspace now flows from active context, not from
                # agent.shared_workspace. Only re-sync when project identity
                # diverges (project_id mismatch or context_type stale).
                if agent and (
                    getattr(agent, "project_id", "") != proj.id
                    or getattr(agent, "context_type", "") != "project"
                ):
                    self._sync_agent_to_project_dir(
                        member.agent_id, proj.working_directory or "",
                        project_id=proj.id, project_name=proj.name)
                    synced += 1
        if synced:
            logger.info("Synced %d agents to their project shared workspaces", synced)
            self._save_agents()

    def get_project(self, project_id: str) -> Project | None:
        return self.projects.get(project_id)

    def remove_project(self, project_id: str) -> bool:
        with self._lock:
            if project_id in self.projects:
                del self.projects[project_id]
                self._save_projects()
                return True
        return False

    def list_projects(self) -> list[dict]:
        return [p.to_dict() for p in self.projects.values()]

    def project_chat(self, project_id: str, content: str,
                      target_agents: list[str] | None = None,
                      user_id: str = "") -> list[str]:
        """用户在项目群聊中发消息，返回会回复的 agent 列表。

        ``user_id`` flows into the source metadata so the agent's per-
        context message bucket records ``source="user:<user_id>"`` —
        this lets downstream UI / audit logs distinguish which human
        sent which message in multi-admin deployments.
        """
        proj = self.projects.get(project_id)
        if not proj:
            return []
        respondents = self.project_chat_engine.handle_user_message(
            proj, content, target_agents, user_id=user_id)
        # Persist project (including new chat messages) to disk
        self._save_projects()
        return respondents

    def project_assign_task(self, project_id: str, title: str,
                             description: str = "", assigned_to: str = "",
                             priority: int = 0) -> ProjectTask | None:
        proj = self.projects.get(project_id)
        if not proj:
            return None
        task = proj.add_task(title, description, assigned_to,
                              created_by="user", priority=priority)
        self._save_projects()
        # 如果分配了 agent，自动驱动执行
        if assigned_to:
            self.project_chat_engine.handle_task_assignment(proj, task)
        return task

    # ---- Local agent management ----

    def create_agent(self, **kwargs) -> Agent:
        logger.info("HUB create_agent: kwargs=%s node_id=%s",
                     {k: v for k, v in kwargs.items() if k != "system_prompt"},
                     self.node_id)
        persona_id = kwargs.pop("persona_id", None)
        # Enforce unique agent names (case-insensitive) across this hub
        requested_name = (kwargs.get("name") or "").strip()
        if requested_name:
            with self._lock:
                for existing in self.agents.values():
                    if (existing.name or "").strip().lower() == requested_name.lower():
                        raise ValueError(
                            f"Agent name '{requested_name}' already exists. "
                            f"Names must be unique."
                        )
            # Also check remote node agents (best-effort)
            for node in self.remote_nodes.values():
                for ra in (node.agents or []):
                    if (ra.get("name") or "").strip().lower() == requested_name.lower():
                        raise ValueError(
                            f"Agent name '{requested_name}' already exists on node "
                            f"'{node.name or node.node_id}'. Names must be unique."
                        )
        agent = create_agent(**kwargs, node_id=self.node_id)
        if persona_id:
            apply_persona_to_agent(agent, persona_id)

        # Auto-bind role defaults: granted_skills + bound_prompt_packs
        # Only apply when caller didn't already populate these fields.
        try:
            from ..core.role_defaults import resolve_role_default_ids
            from ..core.prompt_enhancer import get_prompt_pack_registry
            pp_reg = None
            try:
                pp_reg = get_prompt_pack_registry()
            except Exception:
                pp_reg = None
            default_skills, default_packs = resolve_role_default_ids(
                agent.role, getattr(self, "skill_registry", None), pp_reg
            )
            if default_skills and not agent.granted_skills:
                agent.granted_skills = list(default_skills)
                # Also grant at registry level so the skill is actually accessible
                if getattr(self, "skill_registry", None) is not None:
                    for sid in default_skills:
                        try:
                            self.skill_registry.grant(sid, agent.id)
                        except Exception as _ge:
                            logger.debug("grant default skill %s failed: %s", sid, _ge)
            if default_packs and not agent.bound_prompt_packs:
                agent.bound_prompt_packs = list(default_packs)
            if default_skills or default_packs:
                logger.info(
                    "Role defaults applied: role=%s skills=%s packs=%s",
                    agent.role, default_skills, default_packs,
                )
        except Exception as _rde:
            logger.debug("role defaults auto-bind failed: %s", _rde)

        with self._lock:
            self.agents[agent.id] = agent
        self._save_agents()
        logger.info("HUB create_agent OK: id=%s name=%s role=%s", agent.id, agent.name, agent.role)
        return agent

    def apply_persona(self, agent_id: str, persona_id: str) -> bool:
        """给已有 Agent 应用人设。"""
        agent = self.agents.get(agent_id)
        if not agent:
            return False
        ok = apply_persona_to_agent(agent, persona_id)
        if ok:
            self._save_agents()
        return ok

    def get_agent(self, agent_id: str) -> Agent | None:
        return self.agents.get(agent_id)

    # ── Agent 唤醒：扫描所有项目里分配给该 agent 的未完成任务 ──

    def list_agent_pending_tasks(self, agent_id: str) -> list[dict]:
        """
        汇总指定 agent 在所有 project 中的待办任务（todo + in_progress）。
        返回按 (project_paused, priority desc, created_at asc) 排序的字典列表。
        """
        from ..project import ProjectTaskStatus
        out = []
        for proj in self.projects.values():
            for t in proj.tasks:
                if t.assigned_to != agent_id:
                    continue
                if t.status not in (ProjectTaskStatus.TODO,
                                    ProjectTaskStatus.IN_PROGRESS):
                    continue
                out.append({
                    "project_id": proj.id,
                    "project_name": proj.name,
                    "project_paused": proj.paused,
                    "task_id": t.id,
                    "title": t.title,
                    "description": t.description,
                    "status": t.status.value,
                    "priority": t.priority,
                    "created_at": t.created_at,
                    "updated_at": t.updated_at,
                })
        out.sort(key=lambda x: (x["project_paused"],
                                -x["priority"],
                                x["created_at"]))
        return out

    # ──────────────────────────────────────────────────────────────
    # Stuck-agent watchdog — fired from _heartbeat_loop
    # ──────────────────────────────────────────────────────────────

    # Seconds an agent can be IDLE-with-open-plan before we nudge it.
    # Read from env so operators can tune without a code change.
    _STUCK_WATCHDOG_IDLE_SEC = float(
        os.environ.get("TUDOU_STUCK_WATCHDOG_IDLE_SEC", "300") or 300)
    _STUCK_WATCHDOG_MAX_WAKES = int(
        os.environ.get("TUDOU_STUCK_WATCHDOG_MAX_WAKES", "3") or 3)
    # Seconds an agent can be BUSY with NO events before we force-reset.
    # Default 300s — longer than the LLM read timeout (180s) so a genuine
    # long tool or slow LLM call finishes first; only true deadlocks trip.
    _BUSY_STALL_SEC = float(
        os.environ.get("TUDOU_BUSY_STALL_SEC", "300") or 300)

    def _maybe_wake_stuck_agent(self, agent) -> None:
        """Nudge an idle agent with an unfinished ExecutionPlan.

        Fires only when ALL of the following hold:
          1. Agent has an `_current_plan` with status == 'active'.
          2. That plan has at least one step in PENDING or IN_PROGRESS.
          3. Agent status == IDLE (no chat loop currently running).
          4. No step has seen an update (started_at / completed_at)
             in the last ``_STUCK_WATCHDOG_IDLE_SEC`` seconds.
          5. Agent has been woken fewer than
             ``_STUCK_WATCHDOG_MAX_WAKES`` times for this plan already.

        When all pass, injects a synthetic user message from
        source="system:watchdog" pointing at the next unfinished step.
        """
        from ..agent_types import AgentStatus, StepStatus
        plan = getattr(agent, "_current_plan", None)
        if plan is None or getattr(plan, "status", "") != "active":
            return
        open_steps = [s for s in plan.steps
                      if s.status in (StepStatus.PENDING,
                                      StepStatus.IN_PROGRESS)]
        if not open_steps:
            return
        if getattr(agent, "status", None) != AgentStatus.IDLE:
            return
        # Most-recent activity timestamp.
        # Includes:
        #   * plan step started_at / completed_at — direct progress signal
        #   * latest agent event (chat turn / tool call / message)
        #     — catches the "agent was just chatting but didn't move the
        #     plan forward" case. Without this signal the watchdog would
        #     fire 5 min after the LAST STEP transition even if the user
        #     was chatting with the agent 30s ago.
        ts_values = []
        for s in plan.steps:
            for k in ("completed_at", "started_at"):
                v = getattr(s, k, 0) or 0
                if v > 0:
                    ts_values.append(v)
        try:
            _evs = list(getattr(agent, "events", []) or [])
            if _evs:
                _last_ev_ts = float(getattr(_evs[-1], "timestamp", 0) or 0)
                if _last_ev_ts > 0:
                    ts_values.append(_last_ev_ts)
        except Exception:
            pass
        last_update = max(ts_values) if ts_values else getattr(
            plan, "created_at", 0) or 0
        now = time.time()
        if last_update <= 0 or (now - last_update) < self._STUCK_WATCHDOG_IDLE_SEC:
            return
        # Per-plan wake cap
        _plan_id = getattr(plan, "id", "")
        counts = getattr(agent, "_stuck_wake_counts", None)
        if not isinstance(counts, dict):
            counts = {}
            try:
                agent._stuck_wake_counts = counts
            except Exception:
                pass
        n = counts.get(_plan_id, 0)
        if n >= self._STUCK_WATCHDOG_MAX_WAKES:
            return
        # Pick the step to nudge: prefer the one already in-progress;
        # otherwise the first pending.
        target = next((s for s in open_steps
                       if s.status == StepStatus.IN_PROGRESS), None) \
                 or open_steps[0]

        # ── Final atomic re-check before sending the wake ─────────
        # Between the initial guard above and this point, another
        # thread (the agent's chat loop, a manual UI action, a
        # plan_update call) may have completed the plan or the target
        # step. If so, sending the wake would be a false reminder —
        # exactly the "state sync issue" users see in the chat history
        # when "已结束的任务又提醒".
        plan_now = getattr(agent, "_current_plan", None)
        if plan_now is None or getattr(plan_now, "status", "") != "active":
            return
        target_now = next((s for s in plan_now.steps if s.id == target.id), None)
        if target_now is None or target_now.status not in (
                StepStatus.PENDING, StepStatus.IN_PROGRESS):
            return
        still_open = [s for s in plan_now.steps
                      if s.status in (StepStatus.PENDING,
                                       StepStatus.IN_PROGRESS)]
        if not still_open:
            return
        # Only after passing the final check do we consume a wake count.
        counts[_plan_id] = n + 1

        msg = (
            "[SYSTEM · 唤醒] 你的当前任务还有未完成步骤，"
            f"已经 {int(now - last_update)} 秒没有任何进展（无新工具调用 / 步骤更新）。\n"
            f"计划：{plan_now.task_summary[:120]}\n"
            f"剩余开放步骤：{len(still_open)}\n"
            f"下一步（id={target_now.id}）：{target_now.title}\n"
            f"acceptance：{(target_now.acceptance or '')[:200]}\n"
            f"请调 plan_update(action='start_step', step_id='{target_now.id}') "
            "然后继续执行。如果你判断任务已经交付或无法推进，请调 "
            "plan_update(action='complete_step' 或 'fail_step') 收尾，而不是静止。\n"
            f"这是自动唤醒 #{n + 1}，最多 {self._STUCK_WATCHDOG_MAX_WAKES} 次。"
        )
        logger.warning(
            "Hub watchdog: waking stuck agent %s — plan '%s' has %d open "
            "steps, idle for %.0fs (wake #%d)",
            agent.id[:8], plan_now.task_summary[:40], len(still_open),
            now - last_update, n + 1)
        try:
            self.supervisor.chat_async(agent.id, msg,
                                       source="system:watchdog")
        except Exception as _ce:
            logger.warning(
                "Hub watchdog: chat_async for %s failed: %s",
                agent.id[:8], _ce)

    def _maybe_reset_busy_stalled_agent(self, agent) -> None:
        """Force-reset a BUSY agent that has emitted no events for too long.

        Different from ``_maybe_wake_stuck_agent`` — that one targets
        IDLE agents with unfinished plans. This one catches the opposite
        failure mode: agent is stuck in BUSY (LLM call hanging past the
        connection's own timeout, dead tool subprocess, deadlocked thread,
        etc.) and the UI shows "busy" but no new events appear in the log.

        Fires only when:
          1. agent.status == BUSY
          2. Last ``AgentEvent`` timestamp (or agent creation time if
             no events yet) is more than ``_BUSY_STALL_SEC`` seconds old.
          3. We haven't already force-reset this BUSY streak — track via
             ``agent._busy_stall_reset_at`` so repeated heartbeats don't
             spam the user.

        Action: transition status → IDLE, emit a user-visible assistant
        message explaining what happened, and clear any in-flight
        ConversationTask that's still claimed active. User can then
        retry or type /new to continue.
        """
        from ..agent_types import AgentStatus, AgentEvent
        if getattr(agent, "status", None) != AgentStatus.BUSY:
            # No longer stalled — clear the flag so a future BUSY streak
            # gets its own reset opportunity.
            try:
                agent._busy_stall_reset_at = 0.0
            except Exception:
                pass
            return
        events = getattr(agent, "events", None) or []
        if events:
            last_event_ts = float(getattr(events[-1], "time", 0) or 0)
        else:
            last_event_ts = float(getattr(agent, "created_at", 0) or 0)
        now = time.time()
        if last_event_ts <= 0 or (now - last_event_ts) < self._BUSY_STALL_SEC:
            return
        # Debounce: don't reset the same stall twice within one minute
        # (gives the forced IDLE transition a moment to actually propagate).
        last_reset = float(getattr(agent, "_busy_stall_reset_at", 0) or 0)
        if last_reset > 0 and (now - last_reset) < 60:
            return
        stall_sec = now - last_event_ts
        logger.error(
            "Hub watchdog: BUSY-stall detected on agent %s — no events "
            "for %.0fs, force-resetting to IDLE",
            agent.id[:8], stall_sec)
        try:
            agent._busy_stall_reset_at = now
        except Exception:
            pass
        # 1) Emit a user-visible message so the chat doesn't look dead.
        try:
            warn = (
                f"⚠️ 检测到 agent 卡死（BUSY 状态 {int(stall_sec)} 秒未产出日志）。"
                "已自动重置为 IDLE，你可以重新输入指令，或用 /new 清空上下文再试。\n"
                "常见原因：LLM 响应挂起、工具子进程死锁。如反复发生，建议切到更稳的 "
                "LLM（设置 auto_route + 云端 primary）并看下 provider 的 status 页。"
            )
            agent.events.append(AgentEvent(now, "message",
                                           {"role": "assistant",
                                            "content": warn,
                                            "_source": "busy_stall_guard"}))
            # Also append to conversation messages so next prompt has
            # the context (don't add if messages list doesn't exist).
            msgs = getattr(agent, "messages", None)
            if isinstance(msgs, list):
                msgs.append({"role": "assistant",
                             "content": warn,
                             "_source": "busy_stall_guard"})
        except Exception as _ee:
            logger.debug("busy-stall emit failed: %s", _ee)
        # 2) Force status → IDLE so the UI unlocks the input.
        try:
            agent.status = AgentStatus.IDLE
        except Exception as _se:
            logger.warning(
                "busy-stall: failed to set IDLE on %s: %s",
                agent.id[:8], _se)
        # 3) Best-effort: close any in-flight ConversationTask that still
        # thinks it's running. (Observer handles the sweep elsewhere; here
        # we just tap it to fast-forward.)
        try:
            from ..conversation_observer import on_agent_event
            on_agent_event(agent.id, {
                "timestamp": now,
                "kind": "agent_reset",
                "data": {"reason": "busy_stall", "stall_sec": int(stall_sec)},
            })
        except Exception:
            pass

    def wake_up_agent(self, agent_id: str,
                       max_tasks: int = 5) -> dict:
        """
        唤醒 agent：扫描其所有未完成任务并依次发起执行。

        - 跳过 paused 项目
        - 每个任务通过 ProjectChatEngine._agent_respond 在后台 daemon 线程触发
        - 一次最多 max_tasks 个，避免一次性 spawn 太多
        - 返回触发清单
        """
        import threading
        agent = self.agents.get(agent_id)
        if not agent:
            return {"ok": False, "error": "agent not found", "triggered": []}

        all_pending = self.list_agent_pending_tasks(agent_id)
        active = [p for p in all_pending if not p["project_paused"]]

        if not active:
            return {"ok": True, "triggered": [], "skipped_paused": [
                p for p in all_pending if p["project_paused"]],
                "message": "no pending tasks (or all in paused projects)"}

        triggered = []
        for item in active[:max_tasks]:
            proj = self.projects.get(item["project_id"])
            if not proj:
                continue
            trigger_msg = (
                f"【唤醒】系统检测到你有未完成的任务需要继续：\n"
                f"- {item['title']}\n"
                f"  描述: {item['description'][:300]}\n"
                f"  状态: {item['status']}\n"
                f"\n请立即继续执行这个任务。完成后请在回复中包含 ✅ 和 '已完成' "
                f"以更新任务状态。如果有阻塞，请明确说出阻塞点。"
            )
            try:
                t = threading.Thread(
                    target=self.project_chat_engine._agent_respond,
                    args=(proj, agent_id, trigger_msg),
                    daemon=True,
                )
                t.start()
                triggered.append({
                    "project_id": item["project_id"],
                    "project_name": item["project_name"],
                    "task_id": item["task_id"],
                    "title": item["title"],
                })
                logger.info("Agent wake-up: triggered %s on task '%s' "
                            "(project=%s)",
                            agent_id[:8], item["title"], item["project_name"])
            except Exception as e:
                logger.error("Agent wake-up failed for task '%s': %s",
                             item["title"], e)

        return {
            "ok": True,
            "triggered": triggered,
            "total_pending": len(all_pending),
            "active_pending": len(active),
            "skipped_paused": [p["project_name"]
                               for p in all_pending if p["project_paused"]],
        }

    def remove_agent(self, agent_id: str) -> bool:
        with self._lock:
            if agent_id in self.agents:
                del self.agents[agent_id]
                self._save_agents()
                # ── 持久层清理 ──
                if self._db:
                    try:
                        self._db.delete_agent(agent_id)
                    except Exception as e:
                        logger.warning("delete_agent from DB failed: %s", e)
                    try:
                        self._db.delete_agent_route(agent_id)
                    except Exception as e:
                        logger.warning("delete_agent_route failed: %s", e)
                # 清理 workspace 目录
                try:
                    import shutil
                    ws_dir = os.path.join(
                        os.environ.get("TUDOU_CLAW_DATA_DIR") or self._data_dir,
                        "workspaces", agent_id,
                    )
                    if os.path.isdir(ws_dir):
                        shutil.rmtree(ws_dir, ignore_errors=True)
                        logger.info("Removed workspace: %s", ws_dir)
                except Exception as e:
                    logger.warning("workspace cleanup failed: %s", e)
                return True
        # Try remote agent: proxy DELETE to hosting node
        node = self.find_agent_node(agent_id)
        if node and node.url:
            try:
                headers = {}
                if node.secret:
                    headers["X-Claw-Secret"] = node.secret
                url = f"{node.url}/api/portal/agent/{agent_id}"
                resp = http_requests.delete(url, headers=headers, timeout=10)
                ok = resp.status_code == 200
            except Exception as e:
                logger.error("HUB remove_agent remote failed: %s -> %s",
                             agent_id, e)
                ok = False
            # Always remove the agent from the local hub's agent list,
            # regardless of remote response (since user wants it gone from the UI)
            try:
                node.agents = [a for a in node.agents if a.get("id") != agent_id]
                self._save_remote_nodes()
            except Exception:
                pass
            return ok
        return False

    def list_agents(self) -> list[dict]:
        result = []
        seen_ids = set()
        for a in self.agents.values():
            if a.id in seen_ids:
                continue
            seen_ids.add(a.id)
            d = a.to_dict()
            d["location"] = "local"
            result.append(d)
        for node in self.remote_nodes.values():
            for ra in node.agents:
                aid = ra.get("id")
                if not aid or aid in seen_ids:
                    # Skip duplicates that can arise when the remote node
                    # exposes its own view of *our* agents (circular remotes).
                    continue
                seen_ids.add(aid)
                ra_copy = dict(ra)
                ra_copy["node_id"] = node.node_id
                ra_copy["node_name"] = node.name
                ra_copy["node_url"] = node.url
                ra_copy["location"] = "remote"
                result.append(ra_copy)
        return result

    def proxy_remote_agent_get(self, agent_id: str, sub_path: str) -> dict | None:
        """Proxy a GET request for a remote agent to its host node.
        sub_path e.g. '/events', '/tasks', '/cost'"""
        node = self.find_agent_node(agent_id)
        if not node:
            return None
        try:
            headers = {}
            if node.secret:
                headers["X-Claw-Secret"] = node.secret
            url = f"{node.url}/api/portal/agent/{agent_id}{sub_path}"
            logger.debug("PROXY GET %s", url)
            resp = http_requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            logger.warning("PROXY GET %s -> %s", url, resp.status_code)
            return None
        except Exception as e:
            logger.error("PROXY GET failed: %s -> %s", agent_id, e)
            return None

    def proxy_remote_agent_post(self, agent_id: str, sub_path: str, body: dict) -> dict | None:
        """Proxy a POST request for a remote agent to its host node."""
        node = self.find_agent_node(agent_id)
        if not node:
            return None
        try:
            headers = {"Content-Type": "application/json"}
            if node.secret:
                headers["X-Claw-Secret"] = node.secret
            url = f"{node.url}/api/portal/agent/{agent_id}{sub_path}"
            logger.debug("PROXY POST %s", url)
            resp = http_requests.post(url, headers=headers, json=body, timeout=15)
            if resp.status_code == 200:
                return resp.json()
            logger.warning("PROXY POST %s -> %s: %s", url, resp.status_code, resp.text[:200])
            return None
        except Exception as e:
            logger.error("PROXY POST failed: %s -> %s", agent_id, e)
            return None

    # ---- Find which node owns an agent ----

    def find_agent_node(self, agent_id: str) -> RemoteNode | None:
        """Find the remote node that hosts a given agent."""
        for node in self.remote_nodes.values():
            for ra in node.agents:
                if ra.get("id") == agent_id:
                    return node
        return None

    def is_local_agent(self, agent_id: str) -> bool:
        return agent_id in self.agents

    # ---- Remote node management ----

    def register_node(self, node_id: str, name: str, url: str,
                      agents: list[dict] = None,
                      secret: str = "") -> RemoteNode:
        logger.info("HUB register_node: id=%s name=%s url=%s agents=%d has_secret=%s",
                     node_id, name, url, len(agents or []), bool(secret))
        node = RemoteNode(
            node_id=node_id, name=name, url=url.rstrip("/") if url else "",
            agents=agents or [], last_seen=time.time(),
            secret=secret,
        )
        with self._lock:
            self.remote_nodes[node_id] = node
        self._save_remote_nodes()
        logger.info("HUB register_node OK: %s now has %d remote nodes",
                     node_id, len(self.remote_nodes))
        return node

    def unregister_node(self, node_id: str):
        with self._lock:
            self.remote_nodes.pop(node_id, None)
        self._save_remote_nodes()

    def update_node_agents(self, node_id: str, agents: list[dict]):
        with self._lock:
            if node_id in self.remote_nodes:
                self.remote_nodes[node_id].agents = agents
                self.remote_nodes[node_id].last_seen = time.time()
                self.remote_nodes[node_id].status = "online"
        self._save_remote_nodes()

    def list_nodes(self) -> list[dict]:
        result = [{
            "node_id": self.node_id,
            "name": self.node_name,
            "url": "local",
            "agent_count": len(self.agents),
            "status": "online",
            "is_self": True,
        }]
        for n in self.remote_nodes.values():
            d = n.to_dict()
            d["is_self"] = False
            result.append(d)
        return result

    # ---- Remote node health check ----

    def refresh_node(self, node_id: str) -> bool:
        node = self.remote_nodes.get(node_id)
        if not node or not node.url:
            logger.warning("HUB refresh_node: node %s not found or no url", node_id)
            return False
        try:
            headers = {}
            if node.secret:
                headers["X-Claw-Secret"] = node.secret
            url = f"{node.url}/api/hub/agents"
            logger.debug("HUB refresh_node: GET %s", url)
            resp = http_requests.get(url, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            node.agents = data.get("agents", [])
            node.last_seen = time.time()
            node.status = "online"
            logger.info("HUB refresh_node OK: %s agents=%d", node_id, len(node.agents))
            return True
        except Exception as e:
            node.status = "error"
            logger.error("HUB refresh_node FAIL: %s error=%s", node_id, e)
            return False

    def refresh_all_nodes(self):
        for nid in list(self.remote_nodes.keys()):
            self.refresh_node(nid)

    # ---- Config deployment (Hub → Node → Agent) ----

    def apply_config_to_local_agent(self, agent_id: str,
                                     config: AgentConfigPayload) -> bool:
        """Apply a config payload to a local agent. Returns True if applied."""
        agent = self.agents.get(agent_id)
        if not agent:
            return False
        if config.name:
            agent.name = config.name
        if config.role:
            agent.role = config.role
        if config.model:
            agent.model = config.model
        if config.provider:
            agent.provider = config.provider
        if config.system_prompt:
            agent.system_prompt = config.system_prompt
        if config.working_dir:
            agent.working_dir = config.working_dir
        if config.profile:
            from ..agent import AgentProfile
            if config.partial:
                # Merge: only overwrite non-empty fields
                current = agent.profile.to_dict()
                for k, v in config.profile.items():
                    if v or v == 0 or v is False:
                        current[k] = v
                agent.profile = AgentProfile.from_dict(current)
            else:
                agent.profile = AgentProfile.from_dict(config.profile)
        # Reset system message so it rebuilds on next chat
        if agent.messages and agent.messages[0].get("role") == "system":
            agent.messages[0] = {"role": "system",
                                  "content": agent._build_system_prompt()}
        agent.history_log.add("config_applied",
                              f"fields: {','.join(k for k, v in config.to_dict().items() if v and k not in ('agent_id', 'partial'))}")
        self._save_agents()
        return True

    def dispatch_config(self, node_id: str, agent_id: str,
                        config: AgentConfigPayload) -> ConfigDeployment:
        """
        Push config to an agent on any node.
        - Local node: apply immediately
        - Remote node: HTTP POST to node, track deployment status
        """
        logger.info("HUB dispatch_config: node=%s agent=%s", node_id, agent_id)
        deployment = ConfigDeployment(
            node_id=node_id,
            agent_id=agent_id,
            config=config.to_dict(),
        )
        config.agent_id = agent_id

        with self._lock:
            self.config_deployments[deployment.deploy_id] = deployment

        # Local node — apply immediately
        if node_id in ("local", self.node_id, ""):
            deployment.status = "dispatched"
            deployment.dispatched_at = time.time()
            ok = self.apply_config_to_local_agent(agent_id, config)
            if ok:
                deployment.status = "applied"
                deployment.applied_at = time.time()
                logger.info("HUB dispatch_config local OK: deploy=%s", deployment.deploy_id)
            else:
                deployment.status = "failed"
                deployment.error = "Agent not found or apply failed"
                logger.error("HUB dispatch_config local FAIL: agent=%s", agent_id)
            return deployment

        # Remote node — push via HTTP
        node = self.remote_nodes.get(node_id)
        if not node or not node.url:
            deployment.status = "failed"
            deployment.error = f"Node '{node_id}' not found or no URL"
            logger.error("HUB dispatch_config: node %s not found", node_id)
            return deployment

        deployment.status = "dispatched"
        deployment.dispatched_at = time.time()

        def _push():
            target_url = f"{node.url}/api/hub/apply-config"
            try:
                headers = {"Content-Type": "application/json"}
                if node.secret:
                    headers["X-Claw-Secret"] = node.secret
                logger.info("HUB dispatch_config -> POST %s", target_url)
                resp = http_requests.post(
                    target_url,
                    headers=headers,
                    json={
                        "deploy_id": deployment.deploy_id,
                        "agent_id": agent_id,
                        "config": config.to_dict(),
                    },
                    timeout=30,
                )
                logger.info("HUB dispatch_config <- status=%s body=%s",
                            resp.status_code, resp.text[:300])
                resp.raise_for_status()
                data = resp.json()
                if data.get("ok"):
                    deployment.status = "ack"
                    deployment.acked_at = time.time()
                    if data.get("applied"):
                        deployment.status = "applied"
                        deployment.applied_at = time.time()
                    logger.info("HUB dispatch_config remote OK: deploy=%s status=%s",
                                deployment.deploy_id, deployment.status)
                else:
                    deployment.status = "failed"
                    deployment.error = data.get("error", "Unknown error")
                    logger.error("HUB dispatch_config remote FAIL: %s", deployment.error)
            except Exception as e:
                deployment.status = "failed"
                deployment.error = str(e)
                logger.exception("HUB dispatch_config EXCEPTION: %s -> %s", target_url, e)

        threading.Thread(target=_push, daemon=True).start()
        return deployment

    def confirm_config_applied(self, deploy_id: str, success: bool = True,
                                error: str = "") -> bool:
        """Called by remote node to confirm config was loaded successfully."""
        dep = self.config_deployments.get(deploy_id)
        if not dep:
            return False
        if success:
            dep.status = "applied"
            dep.applied_at = time.time()
        else:
            dep.status = "failed"
            dep.error = error
        return True

    def batch_dispatch_config(self, configs: list[dict]) -> list[ConfigDeployment]:
        """
        Deploy configs to multiple agents across multiple nodes.
        configs: [{"node_id": ..., "agent_id": ..., "config": {...}}, ...]
        """
        deployments = []
        for item in configs:
            node_id = item.get("node_id", "local")
            agent_id = item.get("agent_id", "")
            cfg = AgentConfigPayload.from_dict(item.get("config", {}))
            dep = self.dispatch_config(node_id, agent_id, cfg)
            deployments.append(dep)
        return deployments

    def get_deployment_status(self, deploy_id: str = "") -> dict | list:
        """Get deployment status. If deploy_id given, return one; else return all."""
        if deploy_id:
            dep = self.config_deployments.get(deploy_id)
            return dep.to_dict() if dep else {}
        # Return all, sorted by created_at desc
        deps = sorted(self.config_deployments.values(),
                       key=lambda d: d.created_at, reverse=True)
        return [d.to_dict() for d in deps[:100]]

    def get_node_config_status(self, node_id: str) -> dict:
        """Get all config deployments for a specific node, with summary."""
        deps = [d for d in self.config_deployments.values()
                if d.node_id == node_id]
        deps.sort(key=lambda d: d.created_at, reverse=True)
        return {
            "node_id": node_id,
            "total": len(deps),
            "applied": sum(1 for d in deps if d.status == "applied"),
            "pending": sum(1 for d in deps if d.status in ("pending", "dispatched", "ack")),
            "failed": sum(1 for d in deps if d.status == "failed"),
            "deployments": [d.to_dict() for d in deps[:50]],
        }

    # ---- Proxy chat to remote agent ----

    def proxy_chat(self, agent_id: str, message: str) -> Any:
        """
        Send a chat message to a remote agent.
        Returns a requests.Response (SSE stream) or None.
        """
        node = self.find_agent_node(agent_id)
        if not node or not node.url:
            return None
        try:
            headers = {"Content-Type": "application/json"}
            if node.secret:
                headers["X-Claw-Secret"] = node.secret
            resp = http_requests.post(
                f"{node.url}/api/agent/chat",
                headers=headers,
                json={"message": message},
                stream=True,
                timeout=300,
            )
            resp.raise_for_status()
            return resp
        except Exception:
            return None

    def proxy_chat_sync(self, agent_id: str, node: RemoteNode,
                         message: str) -> str:
        """同步调用远程 agent，收集完整文本结果（用于 workflow）。"""
        try:
            headers = {"Content-Type": "application/json"}
            if node.secret:
                headers["X-Claw-Secret"] = node.secret
            resp = http_requests.post(
                f"{node.url}/api/agent/chat",
                headers=headers,
                json={"message": message},
                stream=True,
                timeout=600,
            )
            resp.raise_for_status()
            # Collect SSE text events
            full_text = ""
            for line in resp.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    import json as _json
                    evt = _json.loads(data)
                    if evt.get("type") == "text_delta":
                        full_text += evt.get("content", "")
                    elif evt.get("type") == "text":
                        full_text = evt.get("content", "")
                except Exception:
                    pass
            return full_text or "(no response)"
        except Exception as e:
            raise ValueError(f"Remote chat failed: {e}")

    def proxy_clear(self, agent_id: str) -> bool:
        node = self.find_agent_node(agent_id)
        if not node or not node.url:
            return False
        try:
            headers = {}
            if node.secret:
                headers["X-Claw-Secret"] = node.secret
            http_requests.post(
                f"{node.url}/api/agent/clear",
                headers=headers, timeout=10,
            )
            return True
        except Exception:
            return False

    def proxy_events(self, agent_id: str) -> list[dict]:
        node = self.find_agent_node(agent_id)
        if not node or not node.url:
            return []
        try:
            headers = {}
            if node.secret:
                headers["X-Claw-Secret"] = node.secret
            resp = http_requests.get(
                f"{node.url}/api/agent/events",
                headers=headers, timeout=10,
            )
            resp.raise_for_status()
            return resp.json().get("events", [])
        except Exception:
            return []

    def proxy_approvals(self, agent_id: str) -> dict:
        """Get pending approvals from a remote agent."""
        node = self.find_agent_node(agent_id)
        if not node or not node.url:
            return {"pending": [], "history": []}
        try:
            headers = {}
            if node.secret:
                headers["X-Claw-Secret"] = node.secret
            resp = http_requests.get(
                f"{node.url}/api/agent/approvals",
                headers=headers, timeout=10,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception:
            return {"pending": [], "history": []}

    def proxy_approve(self, agent_id: str, approval_id: str,
                      action: str = "approve") -> bool:
        """Approve/deny a tool execution on a remote agent."""
        node = self.find_agent_node(agent_id)
        if not node or not node.url:
            return False
        try:
            headers = {"Content-Type": "application/json"}
            if node.secret:
                headers["X-Claw-Secret"] = node.secret
            resp = http_requests.post(
                f"{node.url}/api/agent/approve",
                headers=headers,
                json={"approval_id": approval_id, "action": action},
                timeout=10,
            )
            return resp.json().get("ok", False)
        except Exception:
            return False

    def proxy_update_model(self, agent_id: str, node: RemoteNode,
                           provider: str = "", model: str = "") -> bool:
        """Update provider/model on a remote agent."""
        if not node or not node.url:
            return False
        try:
            headers = {"Content-Type": "application/json"}
            if node.secret:
                headers["X-Claw-Secret"] = node.secret
            resp = http_requests.post(
                f"{node.url}/api/agent/model",
                headers=headers,
                json={"provider": provider, "model": model},
                timeout=10,
            )
            return resp.json().get("ok", False)
        except Exception:
            return False

    # ---- Inter-agent messaging ----

    def _load_messages(self):
        """Load inter-agent messages from SQLite on startup."""
        if self._db:
            try:
                rows = self._db.load_messages(limit=3000)
                for d in rows:
                    msg = AgentMessage(
                        id=d.get("id", ""),
                        from_agent=d.get("from_agent", ""),
                        to_agent=d.get("to_agent", ""),
                        from_agent_name=d.get("from_agent_name", ""),
                        to_agent_name=d.get("to_agent_name", ""),
                        content=d.get("content", ""),
                        msg_type=d.get("msg_type", "task"),
                        timestamp=d.get("timestamp", 0),
                        status=d.get("status", "pending"),
                    )
                    self.messages.append(msg)
                # Reverse so oldest first (DB returns newest first)
                self.messages.reverse()
                logger.info("Loaded %d agent messages from SQLite",
                            len(self.messages))
            except Exception as e:
                logger.warning("Failed to load agent messages: %s", e)

    def _save_message(self, msg: AgentMessage):
        """Persist a single message to SQLite."""
        if self._db:
            try:
                self._db.save_message(msg.to_dict())
            except Exception as e:
                logger.warning("Failed to save message %s: %s", msg.id, e)

    def _update_message_status(self, msg: AgentMessage):
        """Update message status in SQLite."""
        if self._db:
            try:
                self._db.update_message_status(msg.id, msg.status)
            except Exception:
                pass

    def send_message(self, from_agent: str, to_agent: str, content: str,
                     msg_type: str = "task") -> AgentMessage:
        # Resolve display names from agent registry
        _from_a = self.agents.get(from_agent)
        _to_a = self.agents.get(to_agent)
        _from_name = f"{_from_a.role}-{_from_a.name}" if _from_a else from_agent
        _to_name = f"{_to_a.role}-{_to_a.name}" if _to_a else to_agent
        msg = AgentMessage(
            from_agent=from_agent, to_agent=to_agent,
            from_agent_name=_from_name, to_agent_name=_to_name,
            content=content, msg_type=msg_type,
        )
        with self._lock:
            self.messages.append(msg)
            if len(self.messages) > 5000:
                self.messages = self.messages[-3000:]
        # Persist to DB
        self._save_message(msg)

        # ---- Central audit: every cross-agent message is logged ----
        try:
            from ..auth import get_auth as _get_auth
            _auth = _get_auth()
            if _auth is not None:
                _auth.audit(
                    action="agent_message",
                    actor=from_agent or "system",
                    target=to_agent or "broadcast",
                    detail=f"[{msg_type}] {content[:300]}",
                )
        except Exception as _aud_err:
            logger.debug("audit skipped for send_message: %s", _aud_err)

        if to_agent in self.agents:
            self._deliver_local(msg)
        else:
            threading.Thread(target=self._deliver_remote, args=(msg,),
                             daemon=True).start()
        return msg

    def route_message(self, from_agent: str, to_agent: str, content: str,
                      msg_type: str = "task", source: str = "api",
                      metadata: dict | None = None) -> AgentMessage | None:
        """Canonical entry point for all inter-agent messages.

        Phase-1 responsibilities:
        1. Validate that the sender/target exist (if not 'user'/'system').
        2. Audit the routing request with explicit `source` + `metadata`.
        3. Delegate actual delivery to `send_message`, which persists and
           fires local or remote dispatch.

        All new code paths that need to send an agent-to-agent message should
        go through this method instead of `send_message` or direct
        `agent.delegate()` invocations.
        """
        # Basic validation — allow "user"/"system"/"admin" pseudo-senders.
        _pseudo = {"user", "system", "admin", "hub", "orchestrator", "workflow"}
        if from_agent and from_agent not in _pseudo and from_agent not in self.agents:
            logger.warning("route_message: unknown sender %s", from_agent)
        if to_agent and to_agent not in self.agents and not self.find_agent_node(to_agent):
            logger.warning("route_message: unknown target %s", to_agent)
            try:
                from ..auth import get_auth as _get_auth
                _a = _get_auth()
                if _a is not None:
                    _a.audit(
                        action="agent_message_rejected",
                        actor=from_agent or "system",
                        target=to_agent or "",
                        detail=f"unknown_target source={source}",
                        success=False,
                    )
            except Exception:
                pass
            return None

        # Structured routing audit (distinct from send_message's audit)
        try:
            from ..auth import get_auth as _get_auth
            _a = _get_auth()
            if _a is not None:
                _detail = f"source={source} type={msg_type} len={len(content)}"
                if metadata:
                    try:
                        import json as _json
                        _detail += " meta=" + _json.dumps(metadata, ensure_ascii=False)[:200]
                    except Exception:
                        pass
                _a.audit(
                    action="agent_route",
                    actor=from_agent or "system",
                    target=to_agent or "",
                    detail=_detail,
                )
        except Exception as _e:
            logger.debug("route_message audit skipped: %s", _e)

        return self.send_message(from_agent, to_agent, content, msg_type=msg_type)

    def _deliver_local(self, msg: AgentMessage):
        if msg.to_agent not in self.agents:
            msg.status = "error"
            self._update_message_status(msg)
            return
        msg.status = "delivered"
        self._update_message_status(msg)

        def _run():
            result = self.supervisor.delegate(
                msg.to_agent, msg.content, from_agent=msg.from_agent)
            msg.status = "completed"
            self._update_message_status(msg)
            if msg.from_agent:
                self.send_message(msg.to_agent, msg.from_agent, result,
                                  msg_type="result")

        threading.Thread(target=_run, daemon=True).start()

    def _deliver_remote(self, msg: AgentMessage):
        node = self.find_agent_node(msg.to_agent)
        if not node or not node.url:
            msg.status = "error"
            self._update_message_status(msg)
            return
        try:
            headers = {"Content-Type": "application/json"}
            if node.secret:
                headers["X-Claw-Secret"] = node.secret
            http_requests.post(
                f"{node.url}/api/hub/deliver",
                headers=headers,
                json=msg.to_dict(),
                timeout=30,
            )
            msg.status = "delivered"
        except Exception:
            msg.status = "error"
        self._update_message_status(msg)

    def get_messages(self, agent_id: str = "", limit: int = 50) -> list[dict]:
        with self._lock:
            entries = self.messages
            if agent_id:
                entries = [m for m in entries
                           if m.from_agent == agent_id or m.to_agent == agent_id]
            return [m.to_dict() for m in entries[-limit:]]

    # ---- Orchestration ----

    def broadcast(self, content: str, from_agent: str = "hub") -> list[AgentMessage]:
        msgs = []
        for aid in list(self.agents.keys()):
            if aid != from_agent:
                msgs.append(self.send_message(from_agent, aid, content,
                                              msg_type="broadcast"))
        return msgs

    def orchestrate(self, task: str,
                    agent_ids: list[str] | None = None) -> dict:
        targets = agent_ids or list(self.agents.keys())
        results = {}
        threads = []

        def _work(aid: str):
            if aid in self.agents:
                results[aid] = self.supervisor.delegate(
                    aid, task, from_agent="orchestrator")

        for aid in targets:
            t = threading.Thread(target=_work, args=(aid,))
            t.start()
            threads.append(t)

        for t in threads:
            t.join(timeout=300)

        return results

    # ---- Workflow orchestration ----

    def create_workflow_from_template(self, template_id: str,
                                      input_data: str = "") -> Workflow | None:
        """
        从模板创建流水线，自动匹配本地 Agent 到步骤。
        模板步骤中的 role 会被映射到实际的 agent_id。
        """
        tmpl = get_workflow_template(template_id)
        if not tmpl:
            return None

        # 按 role 建立索引：role -> agent_id
        role_agents: dict[str, str] = {}
        for aid, agent in self.agents.items():
            if agent.role not in role_agents:
                role_agents[agent.role] = aid

        steps = []
        for s in tmpl["steps"]:
            role = s.get("role", "general")
            agent_id = role_agents.get(role, "")
            if not agent_id:
                # 没有匹配的 Agent，取第一个 general
                agent_id = role_agents.get("general", "")
            steps.append({
                "name": s["name"],
                "agent_id": agent_id,
                "prompt_template": s.get("prompt_template", "{input}"),
                "max_retries": s.get("max_retries", 1),
                "condition": s.get("condition", ""),
                "skip_condition": s.get("skip_condition", ""),
            })

        wf = self.workflow_engine.create_workflow(
            name=tmpl["name"],
            description=tmpl["description"],
            steps=steps,
            input_data=input_data,
        )
        return wf

    def create_custom_workflow(self, name: str, description: str,
                                steps: list[dict],
                                input_data: str = "") -> Workflow:
        """创建自定义流水线。"""
        return self.workflow_engine.create_workflow(
            name=name, description=description,
            steps=steps, input_data=input_data,
        )

    def start_workflow(self, wf_id: str) -> bool:
        return self.workflow_engine.start_workflow(wf_id)

    def abort_workflow(self, wf_id: str) -> bool:
        return self.workflow_engine.abort_workflow(wf_id)

    def get_workflow(self, wf_id: str) -> Workflow | None:
        return self.workflow_engine.get_workflow(wf_id)

    def list_workflows(self) -> list[dict]:
        return self.workflow_engine.list_workflows()

    # ---- src bridge: intelligent routing & system info ----

    def smart_route(self, prompt: str) -> str | None:
        """
        智能路由：根据用户 prompt 自动匹配最适合的 Agent。
        使用 src.PortRuntime 的 token 匹配算法。
        """
        bridge = get_bridge()
        return bridge.route_to_agent_role(prompt, self.agents)

    def get_system_info(self) -> dict:
        """获取完整的系统信息（src + app 合并）。"""
        bridge = get_bridge()
        info = bridge.get_system_info()
        import platform
        import os  # used by workspaces_root below; module-level `os` import
                  # is present at top of file but rebinding here keeps the
                  # closure robust if that import ever moves.
        from .. import DEFAULT_DATA_DIR, USER_HOME
        info["app"] = {
            "local_agents": len(self.agents),
            "remote_nodes": len(self.remote_nodes),
            "projects": len(self.projects),
            "workflows": len(self.workflow_engine._workflows)
                if hasattr(self, 'workflow_engine') else 0,
        }
        info["platform"] = {
            "os": platform.system(),
            "user_home": USER_HOME,
            "data_dir": DEFAULT_DATA_DIR,
            "workspaces_root": os.path.join(DEFAULT_DATA_DIR, "workspaces"),
        }
        return info

    def get_parity_report(self) -> dict:
        """获取 src/archive 的代码一致性审计报告。"""
        bridge = get_bridge()
        return bridge.get_parity_report()

    def get_workspace_summary(self) -> str:
        """获取完整的工作空间摘要。"""
        bridge = get_bridge()
        return bridge.get_summary()

    def route_and_dispatch(self, prompt: str,
                            fallback_agent_id: str = "") -> dict:
        """
        智能路由并调度：找到最合适的 Agent 执行任务。
        Returns: {"agent_id": str, "agent_name": str, "routed": bool}
        """
        best = self.smart_route(prompt)
        if best and best in self.agents:
            return {
                "agent_id": best,
                "agent_name": self.agents[best].name,
                "routed": True,
            }
        if fallback_agent_id and fallback_agent_id in self.agents:
            return {
                "agent_id": fallback_agent_id,
                "agent_name": self.agents[fallback_agent_id].name,
                "routed": False,
            }
        # Pick first idle agent
        for aid, agent in self.agents.items():
            if agent.status.value == "idle":
                return {
                    "agent_id": aid,
                    "agent_name": agent.name,
                    "routed": False,
                }
        return {"agent_id": "", "agent_name": "", "routed": False}

    # ---- src integration: cost & session ----

    def get_agent_cost(self, agent_id: str) -> dict:
        """获取 Agent 的 token 用量和费用统计。"""
        agent = self.agents.get(agent_id)
        if not agent:
            return {}
        return agent.get_cost_summary()

    def get_agent_history(self, agent_id: str) -> str:
        """获取 Agent 的活动历史 Markdown。"""
        agent = self.agents.get(agent_id)
        if not agent:
            return ""
        return agent.get_history_markdown()

    def get_all_costs(self) -> dict:
        """获取所有 Agent 的费用汇总。"""
        result = {}
        total = 0
        for aid, agent in self.agents.items():
            cost = agent.get_cost_summary()
            result[aid] = {
                "name": agent.name,
                "total_units": cost["total_units"],
                "input_tokens": cost["total_input_tokens"],
                "output_tokens": cost["total_output_tokens"],
            }
            total += cost["total_units"]
        result["_total"] = total
        return result

    def save_agent_session(self, agent_id: str) -> str:
        """手动保存单个 Agent 的会话记忆。"""
        agent = self.agents.get(agent_id)
        if not agent:
            return ""
        from pathlib import Path
        session_dir = Path(self._data_dir) / "sessions"
        return agent.save_memory(session_dir)

    def load_agent_session(self, agent_id: str) -> bool:
        """加载 Agent 的会话记忆。"""
        agent = self.agents.get(agent_id)
        if not agent:
            return False
        from pathlib import Path
        session_dir = Path(self._data_dir) / "sessions"
        return agent.load_memory(session_dir)

    def get_tool_surface(self, query: str = "") -> str:
        """查询 src 工具表面索引。"""
        from src.tools import render_tool_index
        return render_tool_index(query=query or None)

    # ---- src memory engine integration ----

    def save_engine_session(self, agent_id: str) -> str:
        """Persist agent's QueryEngine session (full transcript + usage)."""
        agent = self.agents.get(agent_id)
        if not agent:
            return ""
        return agent.persist_engine_session()

    def restore_engine_session(self, agent_id: str) -> bool:
        """Restore agent's QueryEngine session from disk."""
        agent = self.agents.get(agent_id)
        if not agent:
            return False
        return agent.restore_engine_session()

    def get_agent_engine_info(self, agent_id: str) -> dict:
        """Get agent's src engine state summary."""
        agent = self.agents.get(agent_id)
        if not agent:
            return {}
        return {
            "engine_summary": agent.get_engine_summary(),
            "turn_count": agent.turn_count,
            "transcript_size": len(agent.transcript.entries),
            "transcript_preview": list(agent.transcript.entries[-5:]),
            "tool_pool": agent.get_tool_pool_info(),
        }

    def route_agent_prompt(self, agent_id: str, prompt: str) -> dict:
        """Route a prompt through the agent's PortRuntime."""
        agent = self.agents.get(agent_id)
        if not agent:
            return {"error": "Agent not found"}
        return agent.get_routed_tools_for_prompt(prompt)

    def execute_src_tool(self, agent_id: str, tool_name: str,
                         payload: str = "") -> dict:
        """Execute a src-mirrored tool through the agent's ExecutionRegistry."""
        agent = self.agents.get(agent_id)
        if not agent:
            return {"error": "Agent not found"}
        result = agent.execute_src_tool(tool_name, payload)
        return {
            "name": result.name,
            "handled": result.handled,
            "message": result.message,
            "source_hint": result.source_hint,
        }

    def execute_src_command(self, agent_id: str, command_name: str,
                            prompt: str = "") -> dict:
        """Execute a src-mirrored command through the agent's ExecutionRegistry."""
        agent = self.agents.get(agent_id)
        if not agent:
            return {"error": "Agent not found"}
        result = agent.execute_src_command(command_name, prompt)
        return {
            "name": result.name,
            "handled": result.handled,
            "message": result.message,
            "source_hint": result.source_hint,
        }

    def get_agent_transcript(self, agent_id: str) -> list[str]:
        """Get agent's full transcript replay."""
        agent = self.agents.get(agent_id)
        if not agent:
            return []
        return list(agent.replay_transcript())

    def compact_agent_memory(self, agent_id: str) -> bool:
        """Compact an agent's transcript and message memory."""
        agent = self.agents.get(agent_id)
        if not agent:
            return False
        agent.compact_memory()
        return True

    # ---- Summary ----

    def summary(self) -> dict:
        total_cost = sum(a.cost_tracker.total_units for a in self.agents.values())
        return {
            "node_id": self.node_id,
            "node_name": self.node_name,
            "local_agents": len(self.agents),
            "remote_nodes": len(self.remote_nodes),
            "total_remote_agents": sum(
                len(n.agents) for n in self.remote_nodes.values()),
            "pending_messages": sum(
                1 for m in self.messages if m.status == "pending"),
            "total_cost_units": total_cost,
            "projects": len(self.projects),
            "workflows": len(self.workflow_engine._workflows)
                if hasattr(self, 'workflow_engine') else 0,
        }


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_hub: Hub | None = None
_hub_lock = threading.Lock()


def get_hub() -> Hub:
    global _hub
    if _hub is None:
        with _hub_lock:
            if _hub is None:
                _hub = Hub()
    return _hub


def init_hub(node_id: str = "local", node_name: str = "",
             data_dir: str = "") -> Hub:
    global _hub
    with _hub_lock:
        _hub = Hub(node_id=node_id, node_name=node_name,
                   data_dir=data_dir)
    return _hub
