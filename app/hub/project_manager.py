"""
ProjectManager — project CRUD, task assignment, and workflow binding.

Migrated from ``Hub._core`` into its own manager module.

Target methods (from Hub):
    create_project, get_project, remove_project, list_projects,
    project_chat, project_assign_task,
    _bind_workflow_to_project, _on_workflow_step_complete,
    _sync_agent_to_project_dir, _sync_all_project_dirs,
    create_workflow_from_template, create_custom_workflow,
    start_workflow, abort_workflow, get_workflow, list_workflows
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .manager_base import ManagerBase

if TYPE_CHECKING:
    from ..project import Project, ProjectTask
    from ..workflow import Workflow

logger = logging.getLogger("tudou.hub.project_manager")


class ProjectManager(ManagerBase):
    """Manages projects, tasks, and workflow orchestration."""

    # ------------------------------------------------------------------
    # Project CRUD
    # ------------------------------------------------------------------

    def create_project(self, name: str, description: str = "",
                       member_configs: list[dict] | None = None,
                       working_directory: str = "",
                       node_id: str = "local",
                       workflow_id: str = "",
                       step_assignments: list[dict] | None = None) -> Project:
        from ..project import Project
        from ..agent import Agent
        import os as _os

        proj = Project(name=name, description=description,
                       working_directory=working_directory, node_id=node_id)

        # Canonical project working directory = shared workspace, derived from
        # the generated project id. If the caller didn't provide one, use the
        # shared path so the UI (project detail, chat header, etc.) always has
        # a concrete value every member can see.
        if not proj.working_directory:
            proj.working_directory = Agent.get_shared_workspace_path(proj.id)

        # Materialize the shared directory on disk — agents will drop
        # deliverables here; the deliverables API scans it.
        try:
            shared_dir = Agent.get_shared_workspace_path(proj.id)
            _os.makedirs(shared_dir, exist_ok=True)
        except OSError:
            pass

        if member_configs:
            for mc in member_configs:
                agent_id = mc.get("agent_id", "")
                proj.add_member(agent_id,
                                mc.get("responsibility", ""))
                # Every member must have its shared_workspace hooked up at
                # creation time — previously this only happened when the user
                # typed a working_directory, so members created without one had
                # to wait for the next server restart to be synced.
                if agent_id:
                    self._sync_agent_to_project_dir(
                        agent_id, proj.working_directory,
                        project_id=proj.id, project_name=proj.name)

        # Workflow 绑定
        if workflow_id:
            self._bind_workflow_to_project(proj, workflow_id,
                                           step_assignments or [])

        with self._lock:
            self._hub.projects[proj.id] = proj
        self._hub._save_projects()
        # shared_workspace changed on every member → persist agents unconditionally
        self._hub._save_agents()
        return proj

    def get_project(self, project_id: str) -> Project | None:
        return self._hub.projects.get(project_id)

    def remove_project(self, project_id: str) -> bool:
        with self._lock:
            if project_id not in self._hub.projects:
                return False
            del self._hub.projects[project_id]
            self._hub._save_projects()

            # ``_save_projects`` now does sync-delete, but call the DB
            # delete explicitly as a belt-and-braces guarantee.
            try:
                db = getattr(self, "_db", None) or getattr(self._hub, "_db", None)
            except Exception:
                db = None
            if db is not None:
                try:
                    if hasattr(db, "delete_project"):
                        db.delete_project(project_id)
                    else:
                        db.delete("projects", "project_id", project_id)
                except Exception as e:
                    logger.warning(
                        "remove_project: DB row delete failed: %s", e)
        return True

    def list_projects(self) -> list[dict]:
        return [p.to_dict() for p in self._hub.projects.values()]

    def project_chat(self, project_id: str, content: str,
                     target_agents: list[str] | None = None) -> list[str]:
        """用户在项目群聊中发消息，返回会回复的 agent 列表。"""
        proj = self._hub.projects.get(project_id)
        if not proj:
            return []
        respondents = self._hub.project_chat_engine.handle_user_message(
            proj, content, target_agents)
        # Persist project (including new chat messages) to disk
        self._hub._save_projects()
        return respondents

    def project_assign_task(self, project_id: str, title: str,
                            description: str = "", assigned_to: str = "",
                            priority: int = 0) -> ProjectTask | None:
        from ..project import ProjectTask

        proj = self._hub.projects.get(project_id)
        if not proj:
            return None
        task = proj.add_task(title, description, assigned_to,
                             created_by="user", priority=priority)
        self._hub._save_projects()
        # 如果分配了 agent，自动驱动执行
        if assigned_to:
            self._hub.project_chat_engine.handle_task_assignment(proj, task)
        return task

    # ------------------------------------------------------------------
    # Workflow binding helpers
    # ------------------------------------------------------------------

    def _bind_workflow_to_project(self, proj: Project, workflow_id: str,
                                  step_assignments: list[dict]):
        """查找 WorkflowTemplate 并绑定到项目，自动生成任务。"""
        tmpl = self._hub.workflow_engine.get_template(workflow_id)
        if not tmpl:
            logger.warning("bind_workflow: template %s not found", workflow_id)
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
            for proj in self._hub.projects.values():
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
        self._hub._save_projects()

        # Step 完成后触发该 Agent 的记忆整理（归并 plan→done）
        if status == "done" and agent_id_for_consolidate:
            agent = self._hub.get_agent(agent_id_for_consolidate)
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
                    logger.debug("Post-workflow consolidate failed: %s", e)

            # Auto-progress: 触发下一个步骤的 Agent
            for proj in self._hub.projects.values():
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
                        self._hub.project_chat_engine._auto_progress_next_step(
                            proj, completed_task)
                    except Exception as e:
                        logger.warning("WF auto-progress failed: %s", e)

    # ------------------------------------------------------------------
    # Agent ↔ Project directory sync
    # ------------------------------------------------------------------

    def _sync_agent_to_project_dir(self, agent_id: str, project_dir: str,
                                   project_id: str = "", project_name: str = ""):
        """Sync a project's shared workspace to an agent.

        Directory layout:
          ~/.tudou_claw/workspaces/agents/{agent_id}/   <- private (working_dir)
          ~/.tudou_claw/workspaces/shared/{project_id}/ <- shared (shared_workspace)

        Does NOT overwrite working_dir -- that stays as the agent's private
        workspace. The agent's system prompt will tell it which directory to
        use for project tasks vs personal tasks.
        """
        agent = self._hub.get_agent(agent_id)
        if not agent:
            return
        import os
        from ..agent import Agent

        # Compute the canonical shared workspace path under the standard root
        if project_id:
            shared_dir = Agent.get_shared_workspace_path(project_id)
        else:
            shared_dir = project_dir  # fallback if no project_id

        # Set shared_workspace so sandbox allows access AND prompt tells agent
        agent.shared_workspace = shared_dir
        # Set project identity so prompt context is aware
        if project_id:
            agent.project_id = project_id
        if project_name:
            agent.project_name = project_name
        # Mark routing context: all deliverables must go to shared_dir.
        # (Set unconditionally when binding to a project — overrides any
        # stale "solo" value from when the agent was created standalone.)
        agent.context_type = "project"
        # Ensure the shared directory exists (skip if path is invalid)
        try:
            os.makedirs(shared_dir, exist_ok=True)
        except OSError:
            pass
        # Re-create workspace symlink (workspace/shared -> shared_dir)
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
        from ..agent import Agent

        for proj in self._hub.projects.values():
            expected_shared = Agent.get_shared_workspace_path(proj.id)
            for member in proj.members:
                agent = self.agents.get(member.agent_id)
                if agent and (agent.shared_workspace != expected_shared
                              or agent.project_id != proj.id):
                    self._sync_agent_to_project_dir(
                        member.agent_id, proj.working_directory or "",
                        project_id=proj.id, project_name=proj.name)
                    synced += 1
        if synced:
            logger.info("Synced %d agents to their project shared workspaces", synced)
            self._hub._save_agents()

    # ------------------------------------------------------------------
    # Workflow orchestration
    # ------------------------------------------------------------------

    def create_workflow_from_template(self, template_id: str,
                                     input_data: str = "") -> Workflow | None:
        """
        从模板创建流水线，自动匹配本地 Agent 到步骤。
        模板步骤中的 role 会被映射到实际的 agent_id。
        """
        from ..workflow import get_workflow_template

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

        wf = self._hub.workflow_engine.create_workflow(
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
        return self._hub.workflow_engine.create_workflow(
            name=name, description=description,
            steps=steps, input_data=input_data,
        )

    def start_workflow(self, wf_id: str) -> bool:
        return self._hub.workflow_engine.start_workflow(wf_id)

    def abort_workflow(self, wf_id: str) -> bool:
        return self._hub.workflow_engine.abort_workflow(wf_id)

    def get_workflow(self, wf_id: str) -> Workflow | None:
        return self._hub.workflow_engine.get_workflow(wf_id)

    def list_workflows(self) -> list[dict]:
        return self._hub.workflow_engine.list_workflows()
