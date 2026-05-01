"""
app.supervisor — Agent-level process isolation supervisor.

Wraps the existing ``WorkerPool`` (app.isolation.worker_pool) to provide
agent-level crash isolation. Each Agent runs its chat/delegate calls
inside an isolated subprocess managed by the pool.

Two operating modes controlled by the ``TUDOU_AGENT_ISOLATION`` env var:

  TUDOU_AGENT_ISOLATION=0  (default)
      All agents run in-process (current behavior, zero overhead).

  TUDOU_AGENT_ISOLATION=1
      Each agent gets a long-lived worker subprocess. chat_async() and
      delegate() are routed to the worker via the frame protocol.
      If a worker crashes (OOM, LLM hang, uncaught exception), the
      WorkerPool auto-respawns it without affecting Hub or other agents.

The supervisor bridges worker EVENT frames (chat_event) into the Hub's
ChatTaskManager so SSE streaming works unchanged for the frontend.

Integration:
    hub._core.py creates an AgentSupervisor on startup and routes
    _workflow_chat / _deliver_local / chat_async through it.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger("tudou.supervisor")


def is_isolation_enabled() -> bool:
    """Check whether agent-level process isolation is turned on."""
    return os.environ.get("TUDOU_AGENT_ISOLATION", "0") == "1"


class AgentSupervisor:
    """Routes agent chat/delegate to isolated worker subprocesses.

    If isolation is disabled, all calls fall through to the in-process
    agent directly (zero overhead path).
    """

    def __init__(
        self,
        data_dir: str,
        *,
        get_agent_fn: Callable[[str], Any] = None,
        save_fn: Callable[[], None] = None,
    ) -> None:
        self._data_dir = data_dir
        self._get_agent = get_agent_fn  # hub.agents.get
        self._save_fn = save_fn         # hub._save_agents
        self._enabled = is_isolation_enabled()
        self._pool = None               # lazy WorkerPool
        self._pool_lock = threading.Lock()
        self._event_bridges: Dict[str, threading.Thread] = {}

        if self._enabled:
            logger.info("AgentSupervisor: isolation ENABLED "
                        "(TUDOU_AGENT_ISOLATION=1)")
        else:
            logger.info("AgentSupervisor: isolation disabled (in-process mode)")

    # ── Pool lifecycle ──

    def _ensure_pool(self):
        """Lazy-initialize the WorkerPool."""
        if self._pool is not None:
            return self._pool
        with self._pool_lock:
            if self._pool is not None:
                return self._pool
            from .isolation.worker_pool import WorkerPool, LocalWorkerLauncher
            launcher = LocalWorkerLauncher(
                logger=lambda m: logger.debug(m),
            )
            self._pool = WorkerPool(
                launcher=launcher,
                idle_timeout=3600.0,  # 1 hour idle before reap
                logger=lambda m: logger.debug(m),
            )
            self._pool.start_reaper()
            return self._pool

    def _build_boot_config(self, agent) -> Dict[str, Any]:
        """Build the boot_config dict for spawning a full_agent worker.

        Phase 2: includes UID allocation and shared directory setup for
        OS-level isolation.
        """
        work_dir = getattr(agent, "working_dir", "") or os.path.join(
            self._data_dir, "workspaces", "agents", agent.id, "workspace"
        )
        os.makedirs(work_dir, exist_ok=True)

        # Resolve from active context (project/meeting), not deprecated field.
        # Falls back to "" for solo chat — solo MUST NOT cross into project dirs.
        shared_ws = (
            agent.get_active_shared_workspace()
            if hasattr(agent, "get_active_shared_workspace") else ""
        )
        project_id = getattr(agent, "project_id", "") or ""

        # Phase 2: UID isolation setup
        uid_isolation = os.environ.get("TUDOU_UID_ISOLATION", "0") == "1"
        boot_uid = 0
        boot_gids: list[int] = []

        if uid_isolation:
            try:
                from .isolation.uid_manager import get_uid_manager
                uid_mgr = get_uid_manager(self._data_dir)
                boot_uid = uid_mgr.allocate_uid(agent.id)

                # Setup private workspace permissions
                uid_mgr.setup_private_workspace(work_dir, agent.id)

                # Setup shared directory group membership
                if project_id and shared_ws:
                    group_name = f"proj_{project_id}"
                    uid_mgr.allocate_project_gid(project_id)
                    uid_mgr.add_to_group(agent.id, group_name)
                    uid_mgr.setup_shared_directory(shared_ws, group_name)
                    boot_gids = uid_mgr.get_agent_gids(agent.id)
            except Exception as e:
                logger.warning("UID isolation setup failed for %s: %s "
                              "(falling back to permissive)",
                              agent.id[:8], e)
                uid_isolation = False

        return {
            "agent_id": agent.id,
            "agent_name": agent.name,
            "work_dir": work_dir,
            "data_dir": self._data_dir,
            "mode": "full_agent",
            "agent_persist_dict": agent.to_persist_dict(),
            "sandbox_mode": "permissive",  # full_agent needs broad access
            "shared_workspace": shared_ws,
            "authorized_workspaces": [work_dir],
            "project_id": project_id,
            # Phase 2 fields
            "uid_isolation": uid_isolation,
            "uid": boot_uid,
            "gids": boot_gids,
        }

    def _get_or_spawn_worker(self, agent_id: str):
        """Get existing worker or spawn a new one for the agent."""
        pool = self._ensure_pool()

        # Check if already running
        w = pool.get(agent_id)
        if w is not None:
            return w

        # Need the agent to build boot config
        agent = self._get_agent(agent_id) if self._get_agent else None
        if agent is None:
            raise RuntimeError(f"Agent {agent_id[:8]} not found for worker spawn")

        boot_config = self._build_boot_config(agent)

        def _event_handler(frame):
            """Handle EVENT frames from worker (chat_event streaming)."""
            if frame.kind2 == "chat_event":
                self._bridge_chat_event(frame.payload)

        def _gate_handler(frame):
            """Handle GATE frames from worker (cross-boundary operations).

            Phase 2: routes shared directory writes through SharedFileRouter
            for concurrent safety (locking + atomic writes + audit).
            """
            from .isolation.protocol import Frame
            action = frame.method or ""
            params = frame.params or {}
            req_id = frame.id or ""

            if action == "shared_write":
                return self._gate_shared_write(req_id, params)
            elif action == "shared_append":
                return self._gate_shared_append(req_id, params)
            elif action == "shared_read":
                return self._gate_shared_read(req_id, params)
            elif action == "shared_mkdir":
                return self._gate_shared_mkdir(req_id, params)
            elif action == "shared_delete":
                return self._gate_shared_delete(req_id, params)
            elif action == "shared_list":
                return self._gate_shared_list(req_id, params)

            # Default: approve (forward compatibility)
            return Frame.gate_resp_ok(req_id, {"approved": True})

        try:
            w = pool.get_or_spawn(
                agent_id=agent_id,
                boot_config=boot_config,
                event_handler=_event_handler,
                gate_handler=_gate_handler,
                boot_timeout=30.0,  # full agent may take longer to boot
            )
            logger.info("Worker for agent %s (%s) ready",
                       agent_id[:8], agent.name)
            return w
        except Exception as e:
            logger.error("Failed to spawn worker for %s: %s",
                        agent_id[:8], e)
            raise

    # ── Chat event bridge ──

    def _bridge_chat_event(self, payload: Dict[str, Any]):
        """Bridge a chat_event from worker into Hub's ChatTaskManager."""
        task_id = payload.get("task_id", "")
        if not task_id:
            return
        try:
            from .chat_task import get_chat_task_manager, ChatTaskStatus
            mgr = get_chat_task_manager()
            task = mgr.get_task(task_id)
            if task is None:
                return

            kind = payload.get("kind", "")
            data = payload.get("data", {})

            if kind == "text_delta":
                task.set_status(ChatTaskStatus.STREAMING,
                                "Generating response...", 80)
                task.push_event({"type": "text_delta",
                                 "content": data.get("content", "")})
            elif kind == "message" and data.get("role") == "assistant":
                task.set_status(ChatTaskStatus.STREAMING,
                                "Generating response...", 85)
                task.push_event({"type": "text",
                                 "content": data.get("content", "")})
            elif kind == "tool_call":
                name = data.get("name", "")
                task.set_status(ChatTaskStatus.TOOL_EXEC, name, -1)
                task.push_event({
                    "type": "tool_call",
                    "name": name,
                    "args": json.dumps(
                        data.get("arguments", {}),
                        ensure_ascii=False)[:200],
                })
            elif kind == "tool_result":
                task.set_status(ChatTaskStatus.THINKING, "Analyzing...", -1)
                task.push_event({
                    "type": "tool_result",
                    "content": data.get("result", "")[:500],
                })
            elif kind == "plan_update":
                task.push_event({
                    "type": "plan_update",
                    "plan": data.get("plan"),
                })
            elif kind == "error":
                task.push_event({"type": "error",
                                 "content": data.get("error", "")})
        except Exception as e:
            logger.debug("bridge_chat_event failed: %s", e)

    # ── Gate handlers for shared directory operations ──

    def _get_router(self):
        from .isolation.shared_file_router import get_shared_file_router
        return get_shared_file_router(self._data_dir)

    def _gate_shared_write(self, req_id: str, params: Dict[str, Any]):
        from .isolation.protocol import Frame
        try:
            router = self._get_router()
            path = params.get("path", "")
            content = params.get("content", "")
            agent_id = params.get("agent_id", "")
            is_bytes = params.get("is_bytes", False)
            if is_bytes:
                import base64
                data = base64.b64decode(content)
                n = router.write_bytes(path, data, agent_id=agent_id)
            else:
                n = router.write(path, content, agent_id=agent_id)
            return Frame.gate_resp_ok(req_id, {"written": n, "path": path})
        except Exception as e:
            return Frame.gate_resp_err(req_id, "shared_write_failed", str(e))

    def _gate_shared_append(self, req_id: str, params: Dict[str, Any]):
        from .isolation.protocol import Frame
        try:
            router = self._get_router()
            n = router.append(
                params.get("path", ""),
                params.get("content", ""),
                agent_id=params.get("agent_id", ""),
            )
            return Frame.gate_resp_ok(req_id, {"appended": n})
        except Exception as e:
            return Frame.gate_resp_err(req_id, "shared_append_failed", str(e))

    def _gate_shared_read(self, req_id: str, params: Dict[str, Any]):
        from .isolation.protocol import Frame
        try:
            router = self._get_router()
            content = router.read(params.get("path", ""))
            return Frame.gate_resp_ok(req_id, {"content": content})
        except Exception as e:
            return Frame.gate_resp_err(req_id, "shared_read_failed", str(e))

    def _gate_shared_mkdir(self, req_id: str, params: Dict[str, Any]):
        from .isolation.protocol import Frame
        try:
            router = self._get_router()
            resolved = router.mkdir(
                params.get("path", ""),
                agent_id=params.get("agent_id", ""),
            )
            return Frame.gate_resp_ok(req_id, {"path": resolved})
        except Exception as e:
            return Frame.gate_resp_err(req_id, "shared_mkdir_failed", str(e))

    def _gate_shared_delete(self, req_id: str, params: Dict[str, Any]):
        from .isolation.protocol import Frame
        try:
            router = self._get_router()
            deleted = router.delete(
                params.get("path", ""),
                agent_id=params.get("agent_id", ""),
            )
            return Frame.gate_resp_ok(req_id, {"deleted": deleted})
        except Exception as e:
            return Frame.gate_resp_err(req_id, "shared_delete_failed", str(e))

    def _gate_shared_list(self, req_id: str, params: Dict[str, Any]):
        from .isolation.protocol import Frame
        try:
            router = self._get_router()
            files = router.list_dir(
                params.get("path", ""),
                recursive=params.get("recursive", False),
            )
            return Frame.gate_resp_ok(req_id, {"files": files})
        except Exception as e:
            return Frame.gate_resp_err(req_id, "shared_list_failed", str(e))

    # ── Public API ──

    @property
    def enabled(self) -> bool:
        return self._enabled

    def delegate(self, agent_id: str, content, from_agent: str = "hub",
                 timeout: float = 300) -> str:
        """Route delegate call — isolated or in-process."""
        if not self._enabled:
            agent = self._get_agent(agent_id) if self._get_agent else None
            if agent is None:
                raise ValueError(f"Agent not found: {agent_id}")
            return agent.delegate(content, from_agent=from_agent)

        # Isolated path
        msg = content if isinstance(content, str) else (
            " ".join(p.get("text", "") for p in content
                     if isinstance(p, dict) and p.get("type") == "text")
            or str(content)
        )
        try:
            w = self._get_or_spawn_worker(agent_id)
            result = w.call("delegate", {
                "content": msg,
                "from_agent": from_agent,
            }, timeout=timeout)
            return result.get("result", "") if isinstance(result, dict) else str(result)
        except Exception as e:
            logger.error("Isolated delegate failed for %s: %s",
                        agent_id[:8], e)
            # Fallback to in-process
            agent = self._get_agent(agent_id) if self._get_agent else None
            if agent is not None:
                logger.info("Falling back to in-process delegate for %s",
                           agent_id[:8])
                return agent.delegate(
                    content if isinstance(content, str) else msg,
                    from_agent=from_agent)
            return f"ERROR: {e}"

    def chat_async(self, agent_id: str, content: str,
                   source: str = "admin") -> Any:
        """Route chat_async — isolated or in-process.

        Returns a ChatTask object (same as agent.chat_async).
        """
        if not self._enabled:
            agent = self._get_agent(agent_id) if self._get_agent else None
            if agent is None:
                raise ValueError(f"Agent not found: {agent_id}")
            return agent.chat_async(content, source=source)

        # Isolated path: create ChatTask in Hub, dispatch to worker
        from .chat_task import get_chat_task_manager, ChatTask, ChatTaskStatus
        mgr = get_chat_task_manager()
        task = mgr.create_task(agent_id, content)
        task.set_status(ChatTaskStatus.THINKING, "🚀 Launching isolated worker...", 5)

        def _run():
            try:
                w = self._get_or_spawn_worker(agent_id)
                task.set_status(ChatTaskStatus.THINKING, "🧠 Thinking...", 10)
                # Worker chat streams events via EVENT frames → _bridge_chat_event
                result = w.call("chat", {
                    "content": content,
                    "source": source,
                    "task_id": task.id,
                }, timeout=600)  # 10 min max
                res_text = result.get("result", "") if isinstance(result, dict) else str(result)
                task.result = res_text
                task.set_status(ChatTaskStatus.COMPLETED, "Done", 100)
                task.push_event({"type": "done", "source": "llm"})
            except Exception as e:
                logger.error("Isolated chat failed for %s: %s",
                            agent_id[:8], e)
                task.error = str(e)
                task.set_status(ChatTaskStatus.FAILED, f"Error: {e}", -1)
                task.push_event({"type": "error", "content": str(e)})
                task.push_event({"type": "done"})
            finally:
                # Sync agent state back from worker
                self._sync_agent_state(agent_id)

        threading.Thread(target=_run, daemon=True,
                         name=f"supervisor-chat-{agent_id[:8]}").start()
        return task

    def _sync_agent_state(self, agent_id: str):
        """Pull updated agent state from worker back to Hub's in-memory copy."""
        try:
            pool = self._pool
            if pool is None:
                return
            w = pool.get(agent_id)
            if w is None:
                return
            result = w.call("get_state", {}, timeout=10)
            if not isinstance(result, dict):
                return
            persist_dict = result.get("persist_dict")
            if not persist_dict:
                return
            # Update Hub's in-memory agent from the worker's state
            agent = self._get_agent(agent_id) if self._get_agent else None
            if agent is None:
                return
            # Sync messages (most important — chat history)
            new_messages = persist_dict.get("messages", [])
            if new_messages and len(new_messages) > len(agent.messages):
                from .agent import Agent
                refreshed = Agent.from_persist_dict(persist_dict)
                agent.messages = refreshed.messages
                logger.debug("Synced %d messages from worker to hub for %s",
                            len(agent.messages), agent_id[:8])
            # Persist
            if self._save_fn:
                try:
                    self._save_fn()
                except Exception:
                    pass
        except Exception as e:
            logger.debug("State sync failed for %s: %s", agent_id[:8], e)

    def spawn_all(self, agents: dict):
        """Pre-spawn workers for all agents (called at Hub startup)."""
        if not self._enabled:
            return
        for agent_id in list(agents.keys()):
            try:
                self._get_or_spawn_worker(agent_id)
            except Exception as e:
                logger.warning("Failed to pre-spawn worker for %s: %s",
                              agent_id[:8], e)

    def stop_worker(self, agent_id: str):
        """Stop a specific agent's worker."""
        if self._pool is not None:
            self._pool.stop_worker(agent_id)

    def restart_worker(self, agent_id: str):
        """Restart an agent's worker (e.g. after config change)."""
        self.stop_worker(agent_id)
        if self._enabled:
            try:
                self._get_or_spawn_worker(agent_id)
            except Exception as e:
                logger.warning("Failed to restart worker for %s: %s",
                              agent_id[:8], e)

    def shutdown(self):
        """Shut down all workers."""
        if self._pool is not None:
            self._pool.shutdown_all()

    def get_status(self) -> Dict[str, Any]:
        """Return status of all workers for monitoring."""
        if not self._enabled or self._pool is None:
            return {"enabled": self._enabled, "workers": {}}
        workers = {}
        for agent_id, w in list(self._pool._workers.items()):
            workers[agent_id] = {
                "agent_id": agent_id,
                "alive": w.is_alive(),
                "idle_seconds": w.idle_seconds(),
                "uptime": time.time() - w._started_at if w._started_at else 0,
            }
        return {"enabled": True, "workers": workers}
