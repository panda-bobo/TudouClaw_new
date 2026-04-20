"""
Agent growth mixin — enhancement, active thinking, self-improvement, and growth.

Extracted from agent.py to reduce file size.  The Agent dataclass inherits
from this mixin so all ``self.*`` references resolve at runtime.
"""
from __future__ import annotations
import json
import logging
import time
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from .agent_types import AgentTask

logger = logging.getLogger("tudou.agent")


class AgentGrowthMixin:
    """Mixin providing enhancement, active thinking, self-improvement, and growth."""

    # ---- Enhancement ----

    def enable_enhancement(self, domain) -> dict:
        """Enable an enhancement domain for this agent.

        `domain` may be a single string (legacy) or a list of up to 8
        preset ids to merge into a composite enhancer.
        """
        from .enhancement import build_enhancer, build_multi_enhancer
        if isinstance(domain, (list, tuple)):
            doms = [str(d).strip() for d in domain if str(d).strip()][:8]
            if not doms:
                self.enhancer = build_enhancer("custom")
            elif len(doms) == 1:
                self.enhancer = build_enhancer(doms[0])
            else:
                self.enhancer = build_multi_enhancer(doms)
            label = "+".join(doms) if doms else "custom"
        else:
            self.enhancer = build_enhancer(str(domain))
            label = str(domain)
        # Rebuild system prompt to include new knowledge
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = self._build_system_prompt()
        logger.info("Enhancement enabled for agent %s: domain=%s", self.id, label)
        return self.enhancer.get_stats()

    def disable_enhancement(self):
        """Disable the enhancement module."""
        self.enhancer = None
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = self._build_system_prompt()
        logger.info("Enhancement disabled for agent %s", self.id)

    def get_enhancement_info(self) -> dict | None:
        """Get detailed enhancement module info."""
        if not self.enhancer:
            return None
        return {
            **self.enhancer.get_stats(),
            "knowledge_entries": [e.to_dict() for e in self.enhancer.knowledge.entries.values()],
            "reasoning_patterns": [p.to_dict() for p in self.enhancer.reasoning.patterns.values()],
            "memory_nodes": [n.to_dict() for n in self.enhancer.memory.nodes.values()],
            "tool_chains": [tc.to_dict() for tc in self.enhancer.tool_chains.values()],
        }

    # ---- Active Thinking ----

    def enable_active_thinking(self, **kwargs) -> dict:
        """Enable the active thinking engine for this agent."""
        from .active_thinking import ActiveThinkingEngine
        if not self.active_thinking:
            self.active_thinking = ActiveThinkingEngine(
                agent=self, role=self.role)
        self.active_thinking.enable(**kwargs)
        # Rebuild system prompt to include active thinking context
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = self._build_system_prompt()
        logger.info("Active thinking enabled for agent %s", self.id)
        return self.active_thinking.get_stats()

    def disable_active_thinking(self):
        """Disable active thinking."""
        if self.active_thinking:
            self.active_thinking.disable()
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = self._build_system_prompt()
        logger.info("Active thinking disabled for agent %s", self.id)

    def trigger_thinking(self, trigger: str = "manual",
                         context: str = "") -> dict:
        """Manually trigger one active thinking cycle."""
        from .active_thinking import ActiveThinkingEngine
        if not self.active_thinking:
            self.active_thinking = ActiveThinkingEngine(
                agent=self, role=self.role)
            self.active_thinking.enable()
        result = self.active_thinking.think_now(trigger=trigger,
                                                 context=context)
        return result.to_dict()

    # ---- Self-Improvement (Experience Library) ----

    def enable_self_improvement(self, auto_retro: bool = True,
                                 auto_learn_interval: int = 0,
                                 import_experience: bool = True,
                                 import_limit: int = 50) -> dict:
        """Enable self-improvement for this agent."""
        from .experience_library import SelfImprovementEngine
        if not self.self_improvement:
            self.self_improvement = SelfImprovementEngine(
                agent=self, role=self.role)
        self.self_improvement.enable(
            auto_retro=auto_retro,
            auto_learn_interval=auto_learn_interval,
            import_experience=import_experience,
            import_limit=import_limit,
        )
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = self._build_system_prompt()
        logger.info("Self-improvement enabled for agent %s", self.id)
        return self.self_improvement.get_stats()

    def disable_self_improvement(self):
        """Disable self-improvement."""
        if self.self_improvement:
            self.self_improvement.disable()
        if self.messages and self.messages[0].get("role") == "system":
            self.messages[0]["content"] = self._build_system_prompt()
        logger.info("Self-improvement disabled for agent %s", self.id)

    def trigger_retrospective(self, task_summary: str = "",
                               context: str = "") -> dict:
        """Trigger a retrospective analysis."""
        from .experience_library import SelfImprovementEngine
        if not self.self_improvement:
            # Don't auto-enable background growth on a one-shot call.
            self.self_improvement = SelfImprovementEngine(
                agent=self, role=self.role)

        # Build prompt and run through LLM
        prompt = self.self_improvement.build_retrospective_prompt(
            task_summary=task_summary, context=context)

        # Use agent's own LLM to perform retrospective
        try:
            from . import llm
            _prov, _mdl = self._resolve_effective_provider_model()
            resp = llm.chat_no_stream(
                messages=[
                    {"role": "system", "content": "你是一个经验复盘助手。请严格按JSON格式输出。"},
                    {"role": "user", "content": prompt},
                ],
                model=_mdl, provider=_prov,
            )
            raw = resp.get("content", "") if isinstance(resp, dict) else str(resp)
            result = self.self_improvement.process_retrospective_output(
                raw, task_summary=task_summary)
            return result.to_dict()
        except Exception as e:
            logger.error(f"Retrospective failed for agent {self.id}: {e}")
            return {"error": str(e)}

    def trigger_active_learning(self, learning_goal: str = "",
                                 knowledge_gap: str = "") -> dict:
        """Trigger active learning. Lower priority than tasks/projects.

        Rejects empty / placeholder goals — these produce noise in the
        learning plan board and never converge to real experiences. Callers
        (growth tick, portal button, etc.) MUST pass a concrete goal.
        """
        goal = (learning_goal or "").strip()
        if not goal or goal in ("(未设定)", "未设定", "未设定目标"):
            return {
                "status": "rejected",
                "error": "learning_goal required: provide a specific study objective",
                "learning_goal": "",
            }

        from .experience_library import SelfImprovementEngine
        if not self.self_improvement:
            # One-shot call — don't permanently enable background growth.
            self.self_improvement = SelfImprovementEngine(
                agent=self, role=self.role)

        # Priority check: if agent has pending tasks/projects, queue instead of executing
        if self.self_improvement.should_pause_for_tasks():
            try:
                queued = self.self_improvement.queue_learning(goal, knowledge_gap)
            except ValueError as ve:
                return {"status": "rejected", "error": str(ve), "learning_goal": goal}
            return {
                "status": "queued",
                "message": f"Agent 有未完成的任务/项目，学习计划已排队。任务完成后将自动执行。",
                "queued_task": queued,
                "learning_goal": goal,
            }

        prompt = self.self_improvement.build_learning_prompt(
            learning_goal=learning_goal, knowledge_gap=knowledge_gap)

        try:
            from . import llm
            _prov, _mdl = self._resolve_effective_provider_model()
            resp = llm.chat_no_stream(
                messages=[
                    {"role": "system", "content": "你是一个主动学习助手。请严格按JSON格式输出。"},
                    {"role": "user", "content": prompt},
                ],
                model=_mdl, provider=_prov,
            )
            raw = resp.get("content", "") if isinstance(resp, dict) else str(resp)
            result = self.self_improvement.process_learning_output(raw)
            return result.to_dict()
        except Exception as e:
            logger.error(f"Active learning failed for agent {self.id}: {e}")
            return {"error": str(e)}

    # ── Self-growth closed loop ───────────────────────────────────────────

    def enqueue_growth_task(self, learning_goal: str = "",
                            knowledge_gap: str = "",
                            title: str = "") -> AgentTask:
        """Create a low-priority self-growth task.

        Growth tasks have priority -1 (background). They only run when the
        agent has no pending normal/high/urgent tasks AND is not busy
        chatting. They use the agent's learning_provider/learning_model
        (cheap/local) instead of the main provider/model.
        """
        task_title = title or (
            f"自我成长: {learning_goal}" if learning_goal else "自我成长 — 经验积累"
        )
        task = AgentTask(
            title=task_title,
            description=knowledge_gap or learning_goal or "Background self-improvement run.",
            priority=-1,
            assigned_by="system",
            source="system",
            tags=["growth"],
            provider=self.learning_provider or "",
            model=self.learning_model or "",
            learning_goal=learning_goal or "",
            knowledge_gap=knowledge_gap or "",
        )
        self.tasks.append(task)
        self._log("task", {
            "action": "growth_enqueued",
            "task_id": task.id,
            "title": task_title,
            "learning_provider": self.learning_provider or "(default)",
            "learning_model": self.learning_model or "(default)",
        })
        logger.info("GROWTH task enqueued: agent=%s task=%s",
                    self.name, task.id)
        return task

    def _has_higher_priority_pending(self) -> bool:
        """True if there are any non-growth tasks still pending or running."""
        from .agent_types import TaskStatus
        for t in self.tasks:
            if t.status in (TaskStatus.TODO, TaskStatus.IN_PROGRESS) and t.priority >= 0:
                return True
        return False

    def _next_growth_task(self) -> AgentTask | None:
        from .agent_types import TaskStatus
        for t in self.tasks:
            if t.status == TaskStatus.TODO and t.priority < 0:
                return t
        return None

    def _derive_growth_gap(self) -> dict | None:
        """Introspect recent activity to synthesize a new growth topic.

        Strategy (cheap, no-LLM):
        1. If there are recent failed tool calls in history_log, target the
           most frequent failing tool.
        2. Otherwise pick a topic from the agent's role/expertise that
           hasn't been studied in the last 24h (tracked via _learning_topics_seen).
        3. Throttle: only synthesize a new topic at most once every 30 minutes
           per agent to avoid spamming the learning model.
        """
        now = time.time()
        # Throttle synthesis
        last_synth = getattr(self, "_last_growth_synth", 0.0) or 0.0
        if (now - last_synth) < 1800:
            return None
        self._last_growth_synth = now

        # Scan recent history for failures
        try:
            recent = list(self.history_log.events[-40:])
        except Exception:
            recent = []
        failures = []
        for e in recent:
            title = (getattr(e, "title", "") or "").lower()
            detail = (getattr(e, "detail", "") or "")
            if "fail" in title or "error" in title or "denied" in title:
                failures.append(detail[:200])
        if failures:
            return {
                "goal": "排查最近的执行失败并提炼改进经验",
                "gap": "最近的失败样本：\n" + "\n".join("- " + f for f in failures[:5]),
            }

        # Fallback: role/expertise-based self-study
        topics_seen = getattr(self, "_growth_topics_seen", None)
        if topics_seen is None:
            topics_seen = {}
            self._growth_topics_seen = topics_seen
        candidates = []
        role = (self.role or "general").lower()
        expertise = list(getattr(self.profile, "expertise", []) or [])
        base_topics = {
            "coder": ["代码重构技巧", "常见 bug 模式", "测试策略", "性能优化"],
            "reviewer": ["代码审查清单", "安全漏洞模式", "可读性准则"],
            "cto": ["系统设计权衡", "技术选型评估", "团队协作流程"],
            "pm": ["需求拆解方法", "风险管理", "里程碑规划"],
            "general": ["沟通技巧", "问题分解方法", "学习新工具的流程"],
        }
        candidates = list(expertise) + base_topics.get(role, base_topics["general"])
        for topic in candidates:
            last_ts = topics_seen.get(topic, 0.0)
            if (now - last_ts) > 86400:  # not in last 24h
                topics_seen[topic] = now
                return {
                    "goal": f"自我学习 — {topic}",
                    "gap": f"围绕角色 '{self.role}' 深化对「{topic}」的理解。请总结关键要点并提炼可复用的经验条目。",
                }
        return None

    def tick_growth(self, min_interval: float = 60.0) -> dict | None:
        """Heartbeat hook: pick a growth task and run it if truly idle.

        Idle = no higher-priority pending tasks AND no active chat. Throttled
        by ``min_interval`` seconds between runs per agent. Returns the task's
        result dict on success, or None if nothing happened.
        """
        from .agent_types import AgentStatus, TaskStatus
        now = time.time()
        if (now - self._last_growth_tick) < min_interval:
            return None
        # Don't interrupt user chat or running work
        if self.status == AgentStatus.BUSY:
            return None
        if self._has_higher_priority_pending():
            return None
        task = self._next_growth_task()
        # ── Closed-loop #1: drain SelfImprovementEngine._learning_queue into
        # growth tasks. trigger_active_learning() queues here when the agent
        # was busy at request time; we now unblock them. Skip entries whose
        # goal is empty — they'd just pollute the board. ──
        if task is None and self.self_improvement is not None:
            queue = getattr(self.self_improvement, "_learning_queue", None) or []
            while queue and task is None:
                nxt = queue.pop(0)
                _g = (nxt.get("learning_goal") or "").strip()
                if not _g:
                    continue  # drop noise
                task = self.enqueue_growth_task(
                    learning_goal=_g,
                    knowledge_gap=(nxt.get("knowledge_gap") or "").strip(),
                )
        # ── Closed-loop #2: if still nothing, synthesize a background
        # introspection task from recent failures / unreviewed experience.
        # We ONLY enqueue when derive returns a concrete goal; no more empty
        # "自我反思与经验沉淀" fallback. ──
        if task is None:
            gap = self._derive_growth_gap()
            if gap and (gap.get("goal") or "").strip():
                task = self.enqueue_growth_task(
                    learning_goal=gap["goal"].strip(),
                    knowledge_gap=(gap.get("gap") or "").strip(),
                )
        # ── Closed-loop #3: periodically check if SkillForge can generate
        # skill drafts from accumulated experiences. Throttle: at most once
        # per 3600s (1 hour) per agent. Runs when agent is truly idle. ──
        if task is None:
            _sf_last = getattr(self, "_last_skill_forge_check", 0.0) or 0.0
            if (now - _sf_last) > 3600:
                self._last_skill_forge_check = now
                try:
                    from .skills._skill_forge import get_skill_forge
                    forge = get_skill_forge()
                    role = getattr(self, "role", "") or ""
                    if role:
                        candidates = forge.scan_for_candidates(role=role)
                        if candidates:
                            logger.info(
                                "SkillForge auto-generated %d candidates for agent=%s role=%s",
                                len(candidates), getattr(self, "name", "?"), role,
                            )
                except Exception as _sfe:
                    logger.debug("SkillForge auto-check failed: %s", _sfe)

        if task is None:
            return None
        self._last_growth_tick = now
        # Mark in-progress and run
        task.status = TaskStatus.IN_PROGRESS
        task.updated_at = now
        prev_task = self._current_task
        self._current_task = task  # routes provider/model via resolver
        try:
            result = self.trigger_active_learning(
                learning_goal=task.learning_goal,
                knowledge_gap=task.knowledge_gap,
            )
            task.status = TaskStatus.DONE
            task.result = (
                result.get("message", "")
                or result.get("status", "")
                or "growth tick complete"
            )[:500] if isinstance(result, dict) else str(result)[:500]
            task.updated_at = time.time()
            self._log("task", {
                "action": "growth_done",
                "task_id": task.id,
            })
            return result if isinstance(result, dict) else {"result": str(result)}
        except Exception as e:
            task.status = TaskStatus.BLOCKED
            task.result = f"growth tick failed: {e}"
            logger.error("GROWTH tick failed for agent %s: %s", self.name, e)
            return {"error": str(e)}
        finally:
            self._current_task = prev_task
