"""
AgentManager — agent CRUD, persona application, wake-up, and cost tracking.

Migrated from Hub._core methods.  Each method formerly lived on the Hub
class; the manager holds a ``_hub`` back-reference (via ManagerBase) so it
can still reach shared state such as ``agents``, ``remote_nodes``, etc.

The Hub now delegates to this manager:

    def create_agent(self, **kw):
        return self._agent_mgr.create_agent(**kw)
"""
from __future__ import annotations

import logging
import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import requests as http_requests

from .manager_base import ManagerBase

if TYPE_CHECKING:
    from ..agent import Agent

logger = logging.getLogger("tudou.hub.agent_manager")


class AgentManager(ManagerBase):
    """Manages local agent lifecycle — create, update, remove, query."""

    # ------------------------------------------------------------------
    # Agent CRUD
    # ------------------------------------------------------------------

    def create_agent(self, **kwargs) -> "Agent":
        from ..agent import create_agent
        from ..persona import apply_persona_to_agent

        logger.info(
            "HUB create_agent: kwargs=%s node_id=%s",
            {k: v for k, v in kwargs.items() if k != "system_prompt"},
            self._hub.node_id,
        )
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

        agent = create_agent(**kwargs, node_id=self._hub.node_id)
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

            skill_registry = getattr(self._hub, "skill_registry", None)
            default_skills, default_packs = resolve_role_default_ids(
                agent.role, skill_registry, pp_reg
            )
            if default_skills and not agent.granted_skills:
                agent.granted_skills = list(default_skills)
                # Also grant at registry level so the skill is actually accessible
                if skill_registry is not None:
                    for sid in default_skills:
                        try:
                            skill_registry.grant(sid, agent.id)
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
        self._hub._save_agents()
        logger.info(
            "HUB create_agent OK: id=%s name=%s role=%s",
            agent.id, agent.name, agent.role,
        )
        return agent

    def apply_persona(self, agent_id: str, persona_id: str) -> bool:
        """给已有 Agent 应用人设。"""
        from ..persona import apply_persona_to_agent

        agent = self.agents.get(agent_id)
        if not agent:
            return False
        ok = apply_persona_to_agent(agent, persona_id)
        if ok:
            self._hub._save_agents()
        return ok

    def get_agent(self, agent_id: str) -> "Agent | None":
        return self.agents.get(agent_id)

    # ------------------------------------------------------------------
    # Agent pending tasks & wake-up
    # ------------------------------------------------------------------

    def list_agent_pending_tasks(self, agent_id: str) -> list[dict]:
        """
        汇总指定 agent 在所有 project 中的待办任务（todo + in_progress）。
        返回按 (project_paused, priority desc, created_at asc) 排序的字典列表。
        """
        from ..project import ProjectTaskStatus

        out: list[dict] = []
        for proj in self._hub.projects.values():
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

    def wake_up_agent(self, agent_id: str, max_tasks: int = 5) -> dict:
        """
        唤醒 agent：扫描其所有未完成任务并依次发起执行。

        - 跳过 paused 项目
        - 每个任务通过 ProjectChatEngine._agent_respond 在后台 daemon 线程触发
        - 一次最多 max_tasks 个，避免一次性 spawn 太多
        - 返回触发清单
        """
        agent = self.agents.get(agent_id)
        if not agent:
            return {"ok": False, "error": "agent not found", "triggered": []}

        all_pending = self.list_agent_pending_tasks(agent_id)
        active = [p for p in all_pending if not p["project_paused"]]

        if not active:
            return {
                "ok": True,
                "triggered": [],
                "skipped_paused": [
                    p for p in all_pending if p["project_paused"]
                ],
                "message": "no pending tasks (or all in paused projects)",
            }

        triggered: list[dict] = []
        for item in active[:max_tasks]:
            proj = self._hub.projects.get(item["project_id"])
            if not proj:
                continue
            trigger_msg = (
                f"【唤醒】系统检测到你有未完成的任务需要继续：\n"
                f"- {item['title']}\n"
                f"  描述: {item['description'][:300]}\n"
                f"  状态: {item['status']}\n"
                f"\n请立即继续执行这个任务。完成后请在回复中包含 \u2705 和 '已完成' "
                f"以更新任务状态。如果有阻塞，请明确说出阻塞点。"
            )
            try:
                t = threading.Thread(
                    target=self._hub.project_chat_engine._agent_respond,
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
                logger.info(
                    "Agent wake-up: triggered %s on task '%s' (project=%s)",
                    agent_id[:8], item["title"], item["project_name"],
                )
            except Exception as e:
                logger.error("Agent wake-up failed for task '%s': %s",
                             item["title"], e)

        return {
            "ok": True,
            "triggered": triggered,
            "total_pending": len(all_pending),
            "active_pending": len(active),
            "skipped_paused": [
                p["project_name"] for p in all_pending if p["project_paused"]
            ],
        }

    # ------------------------------------------------------------------
    # Remove & list
    # ------------------------------------------------------------------

    def remove_agent(self, agent_id: str) -> bool:
        with self._lock:
            if agent_id in self.agents:
                del self.agents[agent_id]
                self._hub._save_agents()
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
                # ── 级联清理依赖数据 ──
                # purge_agent 处理: memory (episodic/semantic/config),
                # file_manifests, approvals, agent_messages (from/to),
                # delegations, MCP agent_bindings, skill grants, V2 tasks,
                # v2/agents/<id> 目录等。每个子系统 best-effort，失败不阻塞。
                try:
                    from ..cleanup import purge_agent as _purge
                    report = _purge(agent_id)
                    logger.info("agent %s cascade purge: %s", agent_id, report)
                except Exception as e:
                    logger.warning("cascade purge failed: %s", e)
                return True

        # Try remote agent: proxy DELETE to hosting node
        node = self._hub.find_agent_node(agent_id)
        if node and node.url:
            try:
                headers: dict[str, str] = {}
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
                self._hub._save_remote_nodes()
            except Exception:
                pass
            return ok
        return False

    def list_agents(self) -> list[dict]:
        result: list[dict] = []
        seen_ids: set[str] = set()
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

    # ------------------------------------------------------------------
    # Cost & history
    # ------------------------------------------------------------------

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
        result: dict = {}
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

    # ------------------------------------------------------------------
    # Session persistence
    # ------------------------------------------------------------------

    def save_agent_session(self, agent_id: str) -> str:
        """手动保存单个 Agent 的会话记忆。"""
        agent = self.agents.get(agent_id)
        if not agent:
            return ""
        session_dir = Path(self._data_dir) / "sessions"
        return agent.save_memory(session_dir)

    def load_agent_session(self, agent_id: str) -> bool:
        """加载 Agent 的会话记忆。"""
        agent = self.agents.get(agent_id)
        if not agent:
            return False
        session_dir = Path(self._data_dir) / "sessions"
        return agent.load_memory(session_dir)

    # ------------------------------------------------------------------
    # Tool surface
    # ------------------------------------------------------------------

    def get_tool_surface(self, query: str = "") -> str:
        """查询 src 工具表面索引。"""
        from src.tools import render_tool_index
        return render_tool_index(query=query or None)

    # ------------------------------------------------------------------
    # Engine session (QueryEngine persistence)
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Prompt routing & src tool execution
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # Transcript & memory compaction
    # ------------------------------------------------------------------

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
