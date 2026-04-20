"""
Persistence — load/save methods for agents, nodes, projects, configs, messages.

Migrated from ``Hub._core`` into a dedicated manager so that the Hub class
stays slim while all serialisation logic lives in one place.

Target methods (from Hub):
    _load_agents, _save_agents, _save_agent_workspace,
    _load_remote_nodes, _save_remote_nodes,
    _load_node_configs, _save_node_configs,
    _load_projects, _save_projects,
    sync_all_agent_mcps
"""
from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING

from .manager_base import ManagerBase
from .types import RemoteNode, NodeConfig

if TYPE_CHECKING:
    from ..agent import Agent
    from ..project import Project

logger = logging.getLogger("tudou.hub.persistence")


class PersistenceManager(ManagerBase):
    """Handles serialisation of Hub state to/from JSON and SQLite."""

    # ------------------------------------------------------------------
    # Agent persistence
    # ------------------------------------------------------------------

    def _load_agents(self):
        """Load agents from SQLite (primary) or JSON (fallback)."""
        from ..agent import Agent

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
        if not os.path.exists(self._hub._agents_file):
            return
        try:
            with open(self._hub._agents_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for d in data.get("agents", []):
                agent = Agent.from_persist_dict(d)
                self.agents[agent.id] = agent
        except Exception as e:
            import traceback
            traceback.print_exc()
        self._auto_migrate_role_defaults()

    def _auto_migrate_role_defaults(self) -> None:
        """Back-fill role_defaults on existing agents at load time.

        When a new prompt pack is added to ``ROLE_DEFAULTS`` (e.g.
        ``action-first``), agents that were created BEFORE the addition
        won't have it in their ``bound_prompt_packs`` — ``create_agent``
        only resolves role defaults at creation time, not retroactively.

        This method does a union merge: for every loaded agent, resolve
        what its role SHOULD have today and append any missing IDs.
        User customizations (extras they added) are preserved; we never
        REMOVE anything that's already bound.

        Runs once per server start, after all agents are loaded.
        Failures are non-fatal (just logged).
        """
        try:
            from ..core.role_defaults import resolve_role_default_ids
            from ..core.prompt_enhancer import get_prompt_pack_registry
        except Exception as _imp:
            logger.debug("role-defaults auto-migrate: imports failed: %s", _imp)
            return

        try:
            pp_reg = get_prompt_pack_registry()
        except Exception:
            pp_reg = None
        skill_registry = getattr(self._hub, "skill_registry", None)

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

            # Union merge — preserve order, append only what's missing.
            added_here = False
            cur_skills = list(getattr(agent, "granted_skills", []) or [])
            for sid in want_skills:
                if sid not in cur_skills:
                    cur_skills.append(sid)
                    added_skills_total += 1
                    added_here = True
                    # Also grant at registry level so the skill is accessible
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
            # Persist so the next restart doesn't redo the work
            try:
                self._hub._save_agents()
            except Exception as _se:
                logger.debug("auto-migrate save failed: %s", _se)

    def _save_agents(self):
        """Persist agents to SQLite (primary) + JSON (backup) + workspace.

        SQLite write is a **sync**: upsert all in-memory rows, then DELETE
        rows whose agent_id is NOT in the in-memory set. Belt-and-braces:
        ``remove_agent`` already calls ``db.delete_agent`` explicitly, but
        this catches any future code path that pops from ``self.agents``
        without going through the manager.
        """
        os.makedirs(self._data_dir, exist_ok=True)

        # SQLite — upsert-all + delete-missing
        if self._db:
            try:
                live_ids = set(self.agents.keys())
                for a in self.agents.values():
                    self._db.save_agent(a.to_persist_dict())
                try:
                    existing = {
                        (r.get("agent_id") or r.get("id") or "")
                        for r in self._db.load_agents()
                    }
                    for stale in existing - live_ids:
                        if not stale:
                            continue
                        if hasattr(self._db, "delete_agent"):
                            self._db.delete_agent(stale)
                        else:
                            self._db.delete("agents", "agent_id", stale)
                except Exception as e:
                    logger.warning("agent sync-delete failed: %s", e)
            except Exception as e:
                logger.warning("SQLite agent save failed: %s", e)

        # JSON backup (保留兼容性)
        data = {"agents": [a.to_persist_dict()
                           for a in self.agents.values()]}
        try:
            with open(self._hub._agents_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning("Failed to save agents to %s: %s",
                           self._hub._agents_file, e)

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
                    logger.debug("Failed to sync MCP for agent %s: %s",
                                 agent.id, e)
        except Exception as e:
            logger.warning("Failed to get MCP manager for sync: %s", e)

    def _save_agent_workspace(self, agent: Agent):
        """Save individual agent to ~/.tudou_claw/workspaces/{id}/agent.json"""
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

    # ------------------------------------------------------------------
    # Remote node persistence
    # ------------------------------------------------------------------

    def _load_remote_nodes(self):
        """Load remote nodes from SQLite (primary) or JSON (fallback)."""
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

        if not os.path.exists(self._hub._nodes_file):
            return
        try:
            with open(self._hub._nodes_file, "r", encoding="utf-8") as f:
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
        import datetime

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
            lines = ["# Tudou Claw — Nodes", "",
                     f"Updated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}",
                     ""]
            lines += [f"## Local Node: {self._hub.node_id}",
                      f"Name: {self._hub.node_name}", ""]
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
            with open(self._hub._nodes_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Node config persistence
    # ------------------------------------------------------------------

    def _load_node_configs(self):
        """Load per-node configurations from SQLite (primary) or JSON."""
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
                    self._hub.node_configs[nid] = nc
                logger.info("Loaded node configs for %d nodes from SQLite",
                            len(self._hub.node_configs))
                return
            except Exception as e:
                logger.warning("SQLite node_config load failed: %s", e)

        if not os.path.exists(self._hub._node_configs_file):
            return
        try:
            with open(self._hub._node_configs_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for d in data.get("configs", []):
                nc = NodeConfig.from_dict(d)
                if nc.node_id:
                    self._hub.node_configs[nc.node_id] = nc
            logger.info("Loaded node configs for %d nodes",
                        len(self._hub.node_configs))
        except Exception:
            import traceback
            traceback.print_exc()

    def _save_node_configs(self):
        """Persist per-node configurations to SQLite + JSON backup."""
        os.makedirs(self._data_dir, exist_ok=True)

        # SQLite primary
        if self._db:
            try:
                for nc in self._hub.node_configs.values():
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
                            for nc in self._hub.node_configs.values()]}
        try:
            with open(self._hub._node_configs_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            import traceback
            traceback.print_exc()

    # ------------------------------------------------------------------
    # Project persistence
    # ------------------------------------------------------------------

    def _load_projects(self):
        """Load projects from SQLite (primary) or JSON (fallback)."""
        from ..project import Project

        if self._db and self._db.count("projects") > 0:
            try:
                for d in self._db.load_projects():
                    proj = Project.from_persist_dict(d)
                    self._hub.projects[proj.id] = proj
                logger.info("Loaded %d projects from SQLite",
                            len(self._hub.projects))
                return
            except Exception as e:
                logger.warning("SQLite project load failed: %s", e)

        if not os.path.exists(self._hub._projects_file):
            return
        try:
            with open(self._hub._projects_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for d in data.get("projects", []):
                proj = Project.from_persist_dict(d)
                self._hub.projects[proj.id] = proj
        except Exception:
            import traceback
            traceback.print_exc()

    def _save_projects(self):
        """Persist projects to SQLite (primary) + JSON (backup) + Markdown.

        SQLite write is a **sync**: upsert all in-memory rows, then DELETE
        any DB rows whose project_id is NOT in the current in-memory set.
        Without this, ``remove_project`` leaves orphan rows in the DB and
        they get resurrected on next ``_load_projects``.
        """
        from ..project import Project

        os.makedirs(self._data_dir, exist_ok=True)

        # SQLite primary — upsert-all + delete-missing
        if self._db:
            try:
                live_ids = set(self._hub.projects.keys())
                for p in self._hub.projects.values():
                    self._db.save_project(p.to_persist_dict())
                try:
                    existing = {
                        r.get("project_id")
                        for r in self._db.load_projects()
                    }
                    existing.discard(None)
                    for stale in existing - live_ids:
                        if hasattr(self._db, "delete_project"):
                            self._db.delete_project(stale)
                        else:
                            self._db.delete("projects", "project_id", stale)
                except Exception as e:
                    logger.warning("project sync-delete failed: %s", e)
            except Exception as e:
                logger.warning("SQLite project save failed: %s", e)

        # JSON backup
        data = {"projects": [p.to_persist_dict()
                              for p in self._hub.projects.values()]}
        try:
            with open(self._hub._projects_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

        # Also export each project as Markdown + per-agent context
        agent_lookup = lambda aid: self.agents.get(aid)
        for proj in self._hub.projects.values():
            try:
                proj.save_markdown(self._data_dir,
                                   agent_lookup=agent_lookup)
            except Exception:
                pass
