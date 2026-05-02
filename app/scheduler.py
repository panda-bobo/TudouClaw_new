"""
Scheduler — 计划任务引擎 for Tudou Claws multi-agent platform.

Complete scheduled job execution system supporting:
- One-time and recurring (cron-based) tasks
- Template variable expansion ({date}, {time}, {weekday})
- Template library integration for context injection
- Channel-based notifications (Slack, Telegram, etc.)
- Execution history tracking and persistence

Architecture:
    ScheduledJob (dataclass)
        ├── cron_expr parsing via CronParser
        ├── template variable expansion
        └── execution via agent.chat() in worker threads

    TaskScheduler (main engine)
        ├── _run_loop() daemon thread (checks every 30s)
        ├── _execute_job() worker threads (per job)
        ├── JSON persistence (~/.tudou_claw/scheduled_jobs.json)
        └── ExecutionRecord tracking

Usage:
    from app.scheduler import init_scheduler, get_scheduler, PRESET_JOBS

    scheduler = init_scheduler()
    scheduler.start()

    # Add from preset
    job = scheduler.add_job(
        agent_id="agent1",
        name="Daily Digest",
        **PRESET_JOBS["daily_aigc_digest"],
        notify_channels=["channel1"],
    )

    # Manually trigger
    scheduler.trigger_now("job_id")

    # Get history
    history = scheduler.get_execution_history("job_id", limit=20)
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("tudou.scheduler")
if not logger.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)-5s %(name)s | %(message)s",
        datefmt="%H:%M:%S"))
    logger.addHandler(_h)
    logger.setLevel(logging.DEBUG)


# ---------------------------------------------------------------------------
# ScheduledJob — 单个计划任务的完整定义
# ---------------------------------------------------------------------------

@dataclass
class ScheduledJob:
    """A single scheduled job definition."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str = ""
    description: str = ""
    agent_id: str = ""

    job_type: str = "recurring"  # "one_time" or "recurring"
    cron_expr: str = "0 9 * * *"  # minute hour day_of_month month day_of_week
    next_run_at: float = 0.0      # unix timestamp

    # Execution tracking — 执行历史记录
    last_run_at: float = 0.0
    last_result: str = ""
    last_status: str = "pending"   # pending | running | success | failed | timeout

    # Job limits — 任务限制
    run_count: int = 0             # how many times executed
    max_runs: int = 0              # 0 = unlimited

    # Prompt & context — 提示词与上下文注入
    prompt_template: str = ""      # main prompt; can use {date}, {time}, {weekday}
    template_ids: list[str] = field(default_factory=list)  # templates to inject

    # Notifications — 通知配置
    notify_channels: list[str] = field(default_factory=list)  # channel_ids to notify
    notify_on: str = "always"  # "always" | "success" | "failure"

    # Job control — 控制状态
    enabled: bool = True
    created_by: str = "admin"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # Metadata — 元数据
    tags: list[str] = field(default_factory=list)
    timeout: int = 600  # seconds, default 10 minutes

    # ── Workflow targeting ──
    # target_type: "chat"            — call agent.chat with prompt_template (default)
    #              "workflow"        — legacy WorkflowEngine.create_instance
    #                                  + start_instance (固定流程节点)
    #              "canvas_workflow" — new dynamic DAG via canvas_executor
    #                                  (动态编排,2026-05-02)
    target_type: str = "chat"
    workflow_id: str = ""                                # WorkflowTemplate id (legacy)
    workflow_step_assignments: list[dict] = field(       # [{step_index, agent_id}]
        default_factory=list)
    workflow_input: str = ""                             # initial input_data;
                                                         # falls back to expanded
                                                         # prompt_template if empty
    canvas_workflow_id: str = ""                         # Canvas workflow id (new)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "agent_id": self.agent_id,
            "job_type": self.job_type,
            "cron_expr": self.cron_expr,
            "next_run_at": self.next_run_at,
            "last_run_at": self.last_run_at,
            "last_result": self.last_result,
            "last_status": self.last_status,
            "run_count": self.run_count,
            "max_runs": self.max_runs,
            "prompt_template": self.prompt_template,
            "template_ids": self.template_ids,
            "notify_channels": self.notify_channels,
            "notify_on": self.notify_on,
            "enabled": self.enabled,
            "created_by": self.created_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "tags": self.tags,
            "timeout": self.timeout,
            "target_type": self.target_type,
            "workflow_id": self.workflow_id,
            "workflow_step_assignments": list(self.workflow_step_assignments),
            "workflow_input": self.workflow_input,
            "canvas_workflow_id": self.canvas_workflow_id,
        }

    @staticmethod
    def from_dict(d: dict) -> ScheduledJob:
        return ScheduledJob(
            id=d.get("id", ""),
            name=d.get("name", ""),
            description=d.get("description", ""),
            agent_id=d.get("agent_id", ""),
            job_type=d.get("job_type", "recurring"),
            cron_expr=d.get("cron_expr", "0 9 * * *"),
            next_run_at=d.get("next_run_at", 0.0),
            last_run_at=d.get("last_run_at", 0.0),
            last_result=d.get("last_result", ""),
            last_status=d.get("last_status", "pending"),
            run_count=d.get("run_count", 0),
            max_runs=d.get("max_runs", 0),
            prompt_template=d.get("prompt_template", ""),
            template_ids=d.get("template_ids", []),
            notify_channels=d.get("notify_channels", []),
            notify_on=d.get("notify_on", "always"),
            enabled=d.get("enabled", True),
            created_by=d.get("created_by", "admin"),
            created_at=d.get("created_at", 0),
            updated_at=d.get("updated_at", 0),
            tags=d.get("tags", []),
            timeout=d.get("timeout", 600),
            target_type=d.get("target_type", "chat"),
            workflow_id=d.get("workflow_id", ""),
            workflow_step_assignments=list(d.get("workflow_step_assignments", []) or []),
            workflow_input=d.get("workflow_input", ""),
            canvas_workflow_id=d.get("canvas_workflow_id", ""),
        )


# ---------------------------------------------------------------------------
# ExecutionRecord — 单次执行记录
# ---------------------------------------------------------------------------

@dataclass
class ExecutionRecord:
    """Record of a single job execution."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    job_id: str = ""
    agent_id: str = ""

    started_at: float = 0.0
    completed_at: float = 0.0

    status: str = "running"  # "running" | "success" | "failed" | "timeout"
    prompt_sent: str = ""    # truncated to 500 chars
    result: str = ""         # truncated to 2000 chars
    notified_channels: list[str] = field(default_factory=list)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "job_id": self.job_id,
            "agent_id": self.agent_id,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "status": self.status,
            "prompt_sent": self.prompt_sent,
            "result": self.result,
            "notified_channels": self.notified_channels,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# CronParser — 极简 cron 表达式解析器
# ---------------------------------------------------------------------------

class CronParser:
    """Minimal cron expression parser.

    Supports format: minute hour day_of_month month day_of_week
    Patterns: * (any), N (specific), N-M (range), N,O,P (list), */N (step)
    """

    @staticmethod
    def parse_field(field_str: str, min_val: int, max_val: int) -> set[int]:
        """Parse a single cron field and return matching values."""
        values = set()

        if field_str == "*":
            return set(range(min_val, max_val + 1))

        # Handle step values: */5, 0-30/5
        if "/" in field_str:
            base, step = field_str.split("/")
            step = int(step)

            if base == "*":
                values = set(range(min_val, max_val + 1, step))
            elif "-" in base:
                start, end = map(int, base.split("-"))
                values = set(range(start, min(end + 1, max_val + 1), step))
            else:
                start = int(base)
                values = set(range(start, max_val + 1, step))
            return values

        # Handle ranges and lists
        for part in field_str.split(","):
            if "-" in part:
                start, end = map(int, part.split("-"))
                values.update(range(start, end + 1))
            else:
                values.add(int(part))

        return values

    @staticmethod
    def matches(cron_expr: str, dt: datetime) -> bool:
        """Check if a datetime matches a cron expression."""
        try:
            parts = cron_expr.strip().split()
            if len(parts) != 5:
                return False

            minute_vals = CronParser.parse_field(parts[0], 0, 59)
            hour_vals = CronParser.parse_field(parts[1], 0, 23)
            day_vals = CronParser.parse_field(parts[2], 1, 31)
            month_vals = CronParser.parse_field(parts[3], 1, 12)
            dow_vals = CronParser.parse_field(parts[4], 0, 6)  # 0=Sunday

            # Check each component
            if dt.minute not in minute_vals:
                return False
            if dt.hour not in hour_vals:
                return False
            if dt.month not in month_vals:
                return False

            # Day and dow matching: match if either matches (cron semantics)
            day_match = dt.day in day_vals or (dt.day <= 31)
            dow_match = dt.weekday() + 1 % 7 in dow_vals or dt.weekday() == (dow_vals.pop() - 1 if dow_vals else dt.weekday())

            # Simplified: check day and weekday
            if parts[2] != "*" and parts[4] == "*":
                return day_match
            if parts[2] == "*" and parts[4] != "*":
                return dt.weekday() in {(d - 1) % 7 for d in dow_vals}
            if parts[2] != "*" and parts[4] != "*":
                return day_match or dt.weekday() in {(d - 1) % 7 for d in dow_vals}

            return True
        except Exception as e:
            logger.error(f"Cron parse error for '{cron_expr}': {e}")
            return False

    @staticmethod
    def next_fire_time(cron_expr: str, after: datetime | None = None) -> datetime:
        """Calculate next fire time for a cron expression."""
        if after is None:
            after = datetime.now()
        else:
            after = after + timedelta(seconds=1)

        # Search forward up to 4 years
        max_iterations = 365 * 4 * 24
        current = after.replace(second=0, microsecond=0)

        for _ in range(max_iterations):
            if CronParser.matches(cron_expr, current):
                return current
            current = current + timedelta(minutes=1)

        # Fallback: next hour
        return current


def recurrence_to_cron(recurrence: str, spec: str) -> str:
    """Convert a friendly recurrence spec to a cron expression.

    Recurrence values:
      - "daily"   → spec="HH:MM"         → "MM HH * * *"
      - "weekly"  → spec="DOW HH:MM"     → "MM HH * * DOW"
          DOW: MON|TUE|WED|THU|FRI|SAT|SUN or 0-6
      - "monthly" → spec="D HH:MM"       → "MM HH D * *"
      - "cron"    → spec=raw cron expr   → passed through
    Returns "" on invalid input.
    """
    r = (recurrence or "").lower()
    s = (spec or "").strip()
    if r == "cron":
        return s
    dow_map = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4,
               "FRI": 5, "SAT": 6}
    try:
        if r == "daily":
            hh, mm = s.split(":")
            return f"{int(mm)} {int(hh)} * * *"
        if r == "weekly":
            dow_part, _, time_part = s.partition(" ")
            hh, mm = time_part.split(":")
            dow_part = dow_part.upper()
            dow = dow_map.get(dow_part, None)
            if dow is None:
                dow = int(dow_part)
            return f"{int(mm)} {int(hh)} * * {dow}"
        if r == "monthly":
            day_part, _, time_part = s.partition(" ")
            hh, mm = time_part.split(":")
            return f"{int(mm)} {int(hh)} {int(day_part)} * *"
    except Exception:
        return ""
    return ""


def compute_next_run(recurrence: str, spec: str,
                     after_ts: float | None = None) -> float:
    """Return next-run unix timestamp for a recurring task, or 0 if none."""
    if (recurrence or "once") == "once":
        return 0.0
    cron_expr = recurrence_to_cron(recurrence, spec)
    if not cron_expr:
        return 0.0
    after = datetime.fromtimestamp(after_ts) if after_ts else datetime.now()
    try:
        dt = CronParser.next_fire_time(cron_expr, after=after)
        return dt.timestamp()
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# TaskScheduler — 主调度引擎
# ---------------------------------------------------------------------------

class TaskScheduler:
    """Complete scheduled task engine with persistence and execution."""

    def __init__(self, data_dir: str = ""):
        from . import DEFAULT_DATA_DIR
        self.data_dir = data_dir or DEFAULT_DATA_DIR
        self.jobs_file = os.path.join(self.data_dir, "scheduled_jobs.json")
        self.history_file = os.path.join(self.data_dir, "execution_history.json")

        self._jobs: dict[str, ScheduledJob] = {}  # job_id -> ScheduledJob
        self._lock = threading.Lock()
        self._running = False
        self._scheduler_thread: threading.Thread | None = None

        # Execution history — 执行历史存储（内存 + 磁盘）
        self._execution_history: dict[str, list[ExecutionRecord]] = {}  # job_id -> [records]

        # Dependencies (injected) — 依赖项注入
        self._hub: Any = None
        self._channel_router: Any = None
        self._template_library: Any = None

        self._load()
        logger.info(f"TaskScheduler initialized with {len(self._jobs)} jobs")

    # ---- Dependency injection ----

    def set_hub(self, hub: Any):
        """Set the Hub instance for agent access."""
        self._hub = hub

    def set_channel_router(self, router: Any):
        """Set the ChannelRouter for notifications."""
        self._channel_router = router

    def set_template_library(self, lib: Any):
        """Set the TemplateLibrary for context injection."""
        self._template_library = lib

    # ---- Lifecycle ----

    def start(self):
        """Start the scheduler daemon thread."""
        if self._running:
            logger.warning("Scheduler already running")
            return

        # Reset stale "running" statuses from a previous crash/restart.
        # On startup no jobs are actually executing, so "running" is stale.
        with self._lock:
            stale = [j for j in self._jobs.values() if j.last_status == "running"]
            for j in stale:
                logger.warning("Scheduler: resetting stale 'running' status for job %s (%s)", j.id, j.name)
                j.last_status = "pending"
            if stale:
                self._save()

        self._running = True
        self._scheduler_thread = threading.Thread(
            target=self._run_loop, daemon=True, name="claw-scheduler")
        self._scheduler_thread.start()
        logger.info("Scheduler started")

    def stop(self):
        """Stop the scheduler daemon thread."""
        self._running = False
        if self._scheduler_thread:
            self._scheduler_thread.join(timeout=5)
        logger.info("Scheduler stopped")

    # ---- Job CRUD ----

    def add_job(self, agent_id: str, **kwargs) -> ScheduledJob:
        """Create and add a new scheduled job.

        自动去重: 如果已存在同 agent + 同 cron 的 job，直接返回已有 job。
        Also fuzzy-matches on agent_id + cron_expr (ignoring name) to
        catch LLM-generated jobs with slightly different titles.
        """
        name = kwargs.pop("name", "")
        cron_expr = kwargs.get("cron_expr", "")

        with self._lock:
            # 去重检查 1: exact match — name + agent_id + cron_expr
            if name:
                for existing in self._jobs.values():
                    if (existing.name == name
                            and existing.agent_id == agent_id
                            and existing.cron_expr == cron_expr
                            and existing.enabled):
                        logger.info(
                            "Duplicate job skipped (exact): '%s' "
                            "(agent=%s, cron=%s) → existing %s",
                            name, agent_id, cron_expr, existing.id)
                        return existing

            # 去重检查 2: fuzzy match — same agent + same cron schedule.
            # LLMs often create jobs with slightly different names
            # ("每日AI早报", "AI早报TOP10") for the same purpose. If
            # this agent already has a job at this exact cron, reuse it.
            if cron_expr:
                for existing in self._jobs.values():
                    if (existing.agent_id == agent_id
                            and existing.cron_expr == cron_expr
                            and existing.enabled):
                        logger.info(
                            "Duplicate job skipped (same cron): '%s' "
                            "(agent=%s, cron=%s) → existing '%s' %s",
                            name, agent_id, cron_expr,
                            existing.name, existing.id)
                        return existing

            job = ScheduledJob(
                agent_id=agent_id,
                name=name,
                **kwargs
            )

            # Calculate first run time if not provided
            if job.next_run_at == 0:
                now = datetime.now()
                job.next_run_at = CronParser.next_fire_time(
                    job.cron_expr, now).timestamp()

            self._jobs[job.id] = job
            self._save()

        logger.info(f"Added job '{name}' (id={job.id})")
        return job

    def update_job(self, job_id: str, **kwargs) -> ScheduledJob | None:
        """Update an existing job."""
        with self._lock:
            if job_id not in self._jobs:
                return None

            job = self._jobs[job_id]
            for k, v in kwargs.items():
                if hasattr(job, k):
                    setattr(job, k, v)

            job.updated_at = time.time()
            self._save()

        logger.info(f"Updated job {job_id}")
        return job

    def remove_job(self, job_id: str) -> bool:
        """Remove a job."""
        with self._lock:
            if job_id in self._jobs:
                del self._jobs[job_id]
                self._execution_history.pop(job_id, None)
                self._save()
                logger.info(f"Removed job {job_id}")
                return True
        return False

    def list_jobs(self, agent_id: str = "", tags: list[str] | None = None) -> list[ScheduledJob]:
        """List jobs, optionally filtered by agent_id and tags."""
        with self._lock:
            jobs = list(self._jobs.values())

        if agent_id:
            jobs = [j for j in jobs if j.agent_id == agent_id]

        if tags:
            jobs = [j for j in jobs if any(t in j.tags for t in tags)]

        return jobs

    def get_job(self, job_id: str) -> ScheduledJob | None:
        """Get a job by ID."""
        return self._jobs.get(job_id)

    # ---- Manual execution ----

    def trigger_now(self, job_id: str) -> bool:
        """Manually trigger a job immediately."""
        job = self.get_job(job_id)
        if not job:
            logger.warning("trigger_now: Job %s not found", job_id)
            return False

        logger.info("trigger_now: Job '%s' (id=%s, agent=%s) — spawning execution thread",
                     job.name, job.id, job.agent_id)
        logger.info("trigger_now: prompt_template='%s', hub=%s",
                     (job.prompt_template or "")[:100],
                     "set" if self._hub else "NOT SET")

        threading.Thread(
            target=self._execute_job, args=(job,), daemon=True,
            name=f"job-trigger-{job_id}").start()
        return True

    # ---- Main scheduler loop ----

    def _run_loop(self):
        """Main scheduler loop: checks every 30 seconds for jobs to run."""
        logger.info("Scheduler loop started")

        while self._running:
            try:
                now = time.time()
                with self._lock:
                    jobs_to_run = [
                        j for j in self._jobs.values()
                        if j.enabled and j.next_run_at <= now
                        and j.last_status != "running"
                    ]

                for job in jobs_to_run:
                    # Check max_runs limit
                    if job.max_runs > 0 and job.run_count >= job.max_runs:
                        logger.info(f"Job {job.id} reached max_runs limit")
                        self.update_job(job.id, enabled=False)
                        continue

                    # Mark as running BEFORE spawning thread to prevent
                    # duplicate execution on next loop iteration.
                    with self._lock:
                        job.last_status = "running"
                        self._save()

                    # Spawn execution thread
                    threading.Thread(
                        target=self._execute_job, args=(job,), daemon=True,
                        name=f"job-{job.id}").start()

                # NOTE: Active Thinking auto-trigger removed.
                # "Thinking" is now a deep-reasoning mode (LLM thinking/extended
                # thinking) invoked on demand, NOT a periodic loop.

                time.sleep(30)  # Check interval: 30 seconds
            except Exception as e:
                logger.error(f"Error in scheduler loop: {e}", exc_info=True)
                time.sleep(30)

    # ---- Job execution — 执行任务的核心逻辑 ----

    def _execute_job(self, job: ScheduledJob):
        """Execute a single job: prompt expansion, context injection, agent call, notifications."""
        logger.info("_execute_job START: job='%s' (id=%s) agent=%s type=%s",
                     job.name, job.id, job.agent_id, job.job_type)
        record = ExecutionRecord(
            job_id=job.id,
            agent_id=job.agent_id,
            started_at=time.time(),
        )
        # Immediately mark as running so UI can see progress
        with self._lock:
            job.last_status = "running"
            self._save()

        try:
            # 1. Expand template variables in prompt
            prompt = self._expand_prompt(job.prompt_template)
            if not prompt.strip():
                # Fallback: use job name as prompt if template is empty
                prompt = job.name
                logger.warning("_execute_job: empty prompt_template, using job name: '%s'", prompt)
            record.prompt_sent = prompt[:500]

            # 2. Inject templates from TemplateLibrary
            if job.template_ids and self._template_library:
                context_lines = []
                for tpl_id in job.template_ids:
                    try:
                        tpl = self._template_library.templates.get(tpl_id)
                        if tpl and tpl.content:
                            context_lines.append(f"--- Template: {tpl.name} ---\n{tpl.content}")
                    except Exception as e:
                        logger.warning(f"Failed to load template {tpl_id}: {e}")

                if context_lines:
                    prompt = "\n\n".join(context_lines) + "\n\n" + prompt

            # 3. Dispatch — workflow branch vs chat branch
            if not self._hub:
                raise RuntimeError("Hub not configured")

            if job.target_type == "canvas_workflow" or job.canvas_workflow_id:
                # ─────────── Canvas workflow execution (动态编排) ───────────
                result_text = self._execute_canvas_workflow_job(job, prompt)
                record.result = (result_text or "")[:2000]
                record.status = "success"
            elif job.target_type == "workflow" or job.workflow_id:
                # ─────────── Legacy workflow execution (固定流程) ───────────
                result_text = self._execute_workflow_job(job, prompt)
                record.result = (result_text or "")[:2000]
                record.status = "success"
            else:
                # ─────────────── Chat execution ───────────────
                agent = self._hub.get_agent(job.agent_id)
                if not agent:
                    # 尝试远程 Agent 执行 (Remote agent execution via Hub proxy)
                    try:
                        proxy_result = self._hub.proxy_remote_agent_post(
                            job.agent_id, "/chat", {"message": prompt})
                        if proxy_result and "error" not in proxy_result:
                            result_text = proxy_result.get("result", str(proxy_result))
                            record.status = "success"
                            record.result = result_text[:2000]
                            # Flow continues to notification section below
                        else:
                            raise ValueError(f"Agent {job.agent_id} not found locally or remotely")
                    except Exception as re:
                        raise ValueError(f"Agent {job.agent_id} not found: {re}")
                else:
                    # Chat in worker thread with timeout
                    result = self._call_agent_with_timeout(agent, prompt, job.timeout)
                    record.result = result[:2000] if result else ""
                    record.status = "success"

        except TimeoutError as e:
            logger.error(f"Job timeout: {e}")
            record.status = "timeout"
            record.error = str(e)[:500]
            record.result = f"⏰ 超时: {str(e)[:200]}"
        except Exception as e:
            logger.error(f"Job execution error: {e}", exc_info=True)
            record.status = "failed"
            record.error = str(e)[:500]
            record.result = f"Error: {str(e)[:100]}"

        finally:
            record.completed_at = time.time()

        # 4. Update job state
        with self._lock:
            job.last_run_at = record.started_at
            job.last_status = record.status
            job.last_result = record.result[:200]
            job.run_count += 1

            # 5. Calculate next_run_at for recurring jobs
            if job.job_type == "recurring":
                now_dt = datetime.fromtimestamp(time.time())
                job.next_run_at = CronParser.next_fire_time(
                    job.cron_expr, now_dt).timestamp()
            else:
                job.enabled = False  # one_time jobs disable after execution

            job.updated_at = time.time()
            self._save()

        # 6. Send notifications
        if self._should_notify(job, record):
            self._send_notifications(job, record)

        # 7. Store execution record
        with self._lock:
            if job.id not in self._execution_history:
                self._execution_history[job.id] = []
            self._execution_history[job.id].append(record)

            # Keep last 100 executions per job
            if len(self._execution_history[job.id]) > 100:
                self._execution_history[job.id] = \
                    self._execution_history[job.id][-100:]

        logger.info(f"Job {job.id} executed: status={record.status}")

    def _expand_prompt(self, template: str) -> str:
        """Expand template variables: {date}, {time}, {weekday}."""
        now = datetime.now()

        expansions = {
            "{date}": now.strftime("%Y-%m-%d"),
            "{time}": now.strftime("%H:%M:%S"),
            "{weekday}": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][now.weekday()],
            "{datetime}": now.strftime("%Y-%m-%d %H:%M:%S"),
            "{timestamp}": str(int(time.time())),
        }

        result = template
        for key, value in expansions.items():
            result = result.replace(key, value)

        return result

    def _execute_workflow_job(self, job: ScheduledJob, expanded_prompt: str) -> str:
        """Trigger a workflow instance for a scheduled job and wait for terminal state.

        Resolution rules:
          - workflow_id must point to an existing WorkflowTemplate
          - step_assignments are taken from job.workflow_step_assignments;
            if empty, every step is assigned to job.agent_id (sensible default
            for "make this agent run my workflow on a schedule")
          - input_data is workflow_input if non-empty, else the expanded prompt
          - polls instance status up to job.timeout seconds; on timeout,
            aborts the instance and raises TimeoutError
        """
        engine = getattr(self._hub, "workflow_engine", None)
        if engine is None:
            raise RuntimeError("workflow_engine not available on hub")

        tmpl = engine.get_template(job.workflow_id)
        if not tmpl:
            raise ValueError(f"Workflow template not found: {job.workflow_id}")

        # Build step assignments
        assignments = list(job.workflow_step_assignments or [])
        if not assignments:
            # Default: assign every step to job.agent_id
            if not job.agent_id:
                raise ValueError(
                    "workflow job has no step_assignments and no agent_id "
                    "to use as default")
            assignments = [
                {"step_index": i, "agent_id": job.agent_id}
                for i in range(len(tmpl.steps))
            ]

        input_data = job.workflow_input or expanded_prompt or job.name

        inst = engine.create_instance(
            template_id=job.workflow_id,
            step_assignments=assignments,
            input_data=input_data,
        )
        if inst is None:
            raise RuntimeError(
                f"Failed to create instance from workflow template "
                f"{job.workflow_id}")

        ok = engine.start_instance(inst.id)
        if not ok:
            raise RuntimeError(
                f"Failed to start workflow instance {inst.id} "
                f"(already running?)")

        logger.info("scheduled workflow started: job=%s tmpl=%s inst=%s",
                    job.id, job.workflow_id, inst.id)

        # Poll for terminal state. Workflow runs in a daemon thread inside
        # WorkflowEngine, so we just observe `inst.status`.
        try:
            from .workflow import WorkflowStatus as _WS
        except Exception:
            from app.workflow import WorkflowStatus as _WS  # type: ignore
        terminal = {_WS.COMPLETED, _WS.FAILED, _WS.ABORTED}

        deadline = time.time() + max(int(job.timeout or 600), 30)
        while time.time() < deadline:
            if inst.status in terminal:
                break
            time.sleep(2)
        else:
            # Timed out — abort and raise
            try:
                engine.abort_instance(inst.id)
            except Exception:
                pass
            raise TimeoutError(
                f"workflow instance {inst.id} did not finish within "
                f"{job.timeout}s")

        # Build summary text
        status_label = inst.status.value if hasattr(inst.status, "value") else str(inst.status)
        summary_lines = [f"workflow={tmpl.name} instance={inst.id} status={status_label}"]
        try:
            done = sum(1 for s in inst.steps if str(getattr(s, "status", "")).endswith("done"))
            total = len(inst.steps)
            summary_lines.append(f"steps={done}/{total}")
        except Exception:
            pass
        if inst.status == _WS.FAILED:
            err = getattr(inst, "error", "") or ""
            if err:
                summary_lines.append(f"error={err[:300]}")
            # Don't raise on FAILED — caller still records as success at the
            # scheduler level so notifications fire; the failure is encoded in
            # the result text.
        return "\n".join(summary_lines)

    def _execute_canvas_workflow_job(self, job: ScheduledJob,
                                      expanded_prompt: str) -> str:
        """Trigger a canvas (dynamic DAG) workflow run on schedule.

        2026-05-02 — companion to ``_execute_workflow_job``. Distinct
        because canvas workflows have a totally different runtime
        (canvas_executor.WorkflowEngine: per-run shared dir + artifact
        store + var substitution + daemon thread per node), and
        because the user explicitly wanted both kinds of workflow
        scheduling to coexist (legacy state-machine vs new dynamic).

        Resolution rules:
          * canvas_workflow_id must point to a saved canvas workflow
            with executable_status == "ready"
          * job.agent_id is IGNORED — agent assignments live in the
            canvas workflow's individual agent nodes, not at the job
            level. (Legacy workflows have a default-agent fallback;
            canvas workflows don't need one.)
          * job.workflow_input + expanded_prompt are NOT used. The
            canvas workflow's own start node + agent prompts are
            self-contained. Future: could inject as a workflow-level
            variable {{__schedule.prompt}} if a real use case appears.
          * polls run state up to job.timeout seconds; on timeout,
            best-effort marks the run as failed (no abort API yet)
            and raises TimeoutError.
        """
        store = getattr(self._hub, "canvas_workflow_store", None)
        engine = getattr(self._hub, "canvas_executor", None)
        if store is None or engine is None:
            raise RuntimeError(
                "canvas_workflow_store / canvas_executor not available "
                "on hub — server may need restart"
            )

        wf_id = (job.canvas_workflow_id or "").strip()
        if not wf_id:
            raise ValueError(
                "canvas_workflow target requires job.canvas_workflow_id"
            )

        wf = store.get(wf_id)
        if not wf:
            raise ValueError(f"canvas workflow not found: {wf_id}")
        if str(wf.get("executable_status", "")) != "ready":
            raise ValueError(
                f"canvas workflow {wf_id} is in status "
                f"{wf.get('executable_status', 'draft')!r}; only 'ready' "
                f"workflows can be scheduled"
            )

        try:
            run = engine.trigger(wf, started_by=f"scheduler:{job.id}")
        except ValueError as ve:
            # Pre-flight validation failure (e.g., a referenced agent
            # was deleted between marking ready and the schedule firing).
            raise ValueError(f"canvas workflow trigger refused: {ve}")

        logger.info(
            "scheduled canvas workflow started: job=%s wf=%s run=%s",
            job.id, wf_id, run.id,
        )

        # Poll the run state. Canvas executor writes state.json after
        # each transition, so we re-read from disk.
        terminal_states = {"succeeded", "failed", "aborted"}
        deadline = time.time() + max(int(job.timeout or 600), 30)
        final_state = None
        while time.time() < deadline:
            state = engine.store.load_state(run.id) or {}
            if state.get("state") in terminal_states:
                final_state = state
                break
            time.sleep(2)
        if final_state is None:
            # Timeout — there's no abort API on canvas executor in MVP,
            # but the run keeps going in the background. Just record the
            # timeout in the job result.
            raise TimeoutError(
                f"canvas workflow run {run.id} did not reach a terminal "
                f"state within {job.timeout}s (still executing in background)"
            )

        # Build summary
        run_state = final_state.get("state", "?")
        node_states = final_state.get("node_states", {})
        succeeded = sum(1 for s in node_states.values() if s == "succeeded")
        failed = sum(1 for s in node_states.values() if s == "failed")
        skipped = sum(1 for s in node_states.values() if s == "skipped")
        # Artifact count (best-effort — store may be unavailable in tests)
        artifact_count = 0
        try:
            from . import canvas_artifacts as _ca
            ast = _ca.get_store()
            if ast is not None:
                artifact_count = len(ast.list_artifacts(run.id))
        except Exception:
            pass

        lines = [
            f"canvas workflow={wf.get('name', wf_id)} run={run.id} "
            f"state={run_state}",
            f"nodes: {succeeded} ok / {failed} failed / {skipped} skipped",
        ]
        if artifact_count:
            lines.append(f"artifacts: {artifact_count}")
        if final_state.get("error"):
            lines.append(f"error: {final_state['error'][:300]}")
        return "\n".join(lines)

    def _call_agent_with_timeout(self, agent: Any, prompt: str,
                                  timeout_sec: int = 600) -> str:
        """Call agent.chat() with timeout handling.

        超时后会通过 _cancellation_event 通知 Agent 优雅中止，
        并等待一小段时间让 Agent 清理资源。
        """
        import threading

        result_holder = {"result": "", "error": None}

        # 确保 cancellation event 处于未触发状态
        if hasattr(agent, '_cancellation_event'):
            agent._cancellation_event.clear()

        # ── Context isolation: scheduled tasks start with a clean message
        #    history so that ad-hoc conversations don't pollute the context.
        #    We snapshot messages, set scheduled flag BEFORE thread start,
        #    then restore after execution.
        #    The _scheduled_context flag suppresses _log() and _auto_save_check()
        #    inside the agent so events/saves don't leak into normal chat.
        original_messages = list(agent.messages)  # shallow copy
        # Keep only system messages (the base system prompt) for clean context
        agent.messages = [m for m in agent.messages if m.get("role") == "system"]
        # Set scheduled flag BEFORE thread start to prevent any race window
        agent._scheduled_context = True
        logger.debug(
            "Scheduler: isolated context for agent %s — saved %d msgs, "
            "starting with %d system msgs",
            agent.id, len(original_messages), len(agent.messages))

        def run():
            try:
                result_holder["result"] = agent.chat(prompt)
            except Exception as e:
                result_holder["error"] = e

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            thread.join(timeout=timeout_sec)

            if thread.is_alive():
                # 触发 Agent 的取消机制，让它在下一个检查点优雅退出
                if hasattr(agent, '_cancellation_event'):
                    agent._cancellation_event.set()
                    logger.warning(
                        "Agent %s scheduled task timed out after %ds, "
                        "sending cancellation signal...",
                        agent.id, timeout_sec)
                    # 给 Agent 一点时间优雅退出
                    thread.join(timeout=15)
                    if not thread.is_alive():
                        # Agent 成功中止，返回已有的部分结果
                        agent._cancellation_event.clear()
                        partial = result_holder.get("result", "")
                        if partial:
                            logger.info("Agent %s returned partial result after cancellation", agent.id)
                            return f"[超时中止，以下为部分结果]\n{partial}"
                    # 清理
                    agent._cancellation_event.clear()

                raise TimeoutError(
                    f"Agent chat exceeded {timeout_sec}s timeout. "
                    f"可在 job 配置中调大 timeout 值（当前 {timeout_sec}s）")

            if result_holder["error"]:
                raise result_holder["error"]

            return result_holder["result"]
        finally:
            # ── Restore: clear scheduled flag + restore messages
            agent._scheduled_context = False
            agent.messages = original_messages
            logger.debug(
                "Scheduler: restored %d messages for agent %s",
                len(original_messages), agent.id)

    def _should_notify(self, job: ScheduledJob, record: ExecutionRecord) -> bool:
        """Determine if notifications should be sent."""
        if not job.notify_channels:
            return False

        if job.notify_on == "always":
            return True
        elif job.notify_on == "success":
            return record.status == "success"
        elif job.notify_on == "failure":
            return record.status in ("failed", "timeout")

        return False

    def _send_notifications(self, job: ScheduledJob, record: ExecutionRecord):
        """Send execution result notifications to channels."""
        if not self._channel_router:
            logger.warning("ChannelRouter not configured, skipping notifications")
            return

        # Build message
        status_emoji = {
            "success": "✓",
            "failed": "✗",
            "timeout": "⏱",
            "running": "...",
        }.get(record.status, "?")

        message = f"{status_emoji} Job: {job.name}\n"
        message += f"Status: {record.status}\n"

        if record.result:
            preview = record.result[:200] + ("..." if len(record.result) > 200 else "")
            message += f"Result: {preview}\n"

        if record.error:
            message += f"Error: {record.error[:100]}\n"

        duration = record.completed_at - record.started_at
        message += f"Duration: {duration:.1f}s"

        # Send to all notify channels
        for channel_id in job.notify_channels:
            try:
                ok = self._channel_router.send_to_channel(
                    channel_id, message, metadata={"job_id": job.id})
                if ok:
                    record.notified_channels.append(channel_id)
            except Exception as e:
                logger.error(f"Failed to notify channel {channel_id}: {e}")

    # ---- History ----

    def get_execution_history(self, job_id: str,
                              limit: int = 20) -> list[ExecutionRecord]:
        """Get execution history for a job."""
        with self._lock:
            records = self._execution_history.get(job_id, [])

        # Return most recent first
        return sorted(records, key=lambda r: r.started_at, reverse=True)[:limit]

    # ---- Persistence ----

    def _get_db(self):
        try:
            from .database import get_database
            return get_database()
        except Exception:
            return None

    def _save(self):
        """Persist jobs to SQLite + JSON backup."""
        os.makedirs(self.data_dir, exist_ok=True)
        db = self._get_db()
        if db:
            try:
                for j in self._jobs.values():
                    db.save_job(j.to_dict())
            except Exception as e:
                logger.warning(f"SQLite job save failed: {e}")

        data = {
            "jobs": [j.to_dict() for j in self._jobs.values()],
            "version": 1,
            "saved_at": time.time(),
        }
        try:
            with open(self.jobs_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save jobs: {e}")

    def _load(self):
        """Load jobs from SQLite (primary) or JSON (fallback)."""
        db = self._get_db()
        if db and db.count("scheduled_jobs") > 0:
            try:
                for job_data in db.load_jobs():
                    job = ScheduledJob.from_dict(job_data)
                    self._jobs[job.id] = job
                logger.info(f"Loaded {len(self._jobs)} jobs from SQLite")
                return
            except Exception as e:
                logger.warning(f"SQLite job load failed: {e}")

        if not os.path.exists(self.jobs_file):
            return
        try:
            with open(self.jobs_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for job_data in data.get("jobs", []):
                job = ScheduledJob.from_dict(job_data)
                self._jobs[job.id] = job
            logger.info(f"Loaded {len(self._jobs)} jobs from disk")
        except Exception as e:
            logger.error(f"Failed to load jobs: {e}")


# ---------------------------------------------------------------------------
# Preset Jobs — 预设任务模版 for common use cases
# ---------------------------------------------------------------------------

PRESET_JOBS = {
    "daily_aigc_digest": {
        "name": "Daily AIGC Digest",
        "description": "每天早上9点 AIGC 热点摘要",
        "job_type": "recurring",
        "cron_expr": "0 9 * * *",  # 9 AM every day
        "prompt_template": """请生成今天的AIGC热点摘要。

包括以下内容：
1. 今天AI/AIGC领域的重大新闻
2. 技术突破和论文发布
3. 产业动态和融资信息
4. 值得关注的开源项目

格式：Markdown，含标题、链接和简要说明。""",
        "template_ids": [],
        "tags": ["daily", "aigc", "digest"],
    },

    "weekly_code_review": {
        "name": "Weekly Code Review",
        "description": "每周一上午10点代码审查提醒",
        "job_type": "recurring",
        "cron_expr": "0 10 * * 1",  # 10 AM every Monday
        "prompt_template": """提醒：本周的代码审查检查清单

请检查以下项目的最新代码：
1. 代码质量：是否符合团队规范？
2. 测试覆盖：是否有充分的单元测试？
3. 文档：是否更新了相关文档？
4. 安全：是否存在明显的安全隐患？
5. 性能：是否有优化空间？

请生成本周代码审查报告。""",
        "template_ids": [],
        "tags": ["weekly", "code_review", "quality"],
    },

    "daily_standup": {
        "name": "Daily Standup",
        "description": "每天早上9:30 站会准备",
        "job_type": "recurring",
        "cron_expr": "30 9 * * *",  # 9:30 AM every day
        "prompt_template": """准备每日站会：{date}

请生成今天的站会议程：
1. 昨天完成的事项
2. 今天的计划
3. 当前的阻碍和风险
4. 需要的帮助

格式简洁，每项2-3行。""",
        "template_ids": [],
        "tags": ["daily", "standup"],
    },

    "weekly_report": {
        "name": "Weekly Report",
        "description": "每周五下午5点生成周报",
        "job_type": "recurring",
        "cron_expr": "0 17 * * 5",  # 5 PM every Friday
        "prompt_template": """本周工作总结 - {date}

请生成周报，包括：
1. 本周主要成就（3-5项）
2. 关键指标和数据
3. 发现的问题和改进机会
4. 下周计划

使用Markdown格式，附带数据图表或表格。""",
        "template_ids": [],
        "tags": ["weekly", "report"],
    },

    "security_scan": {
        "name": "Security Scan",
        "description": "每天凌晨2点安全扫描",
        "job_type": "recurring",
        "cron_expr": "0 2 * * *",  # 2 AM every day
        "prompt_template": """执行安全扫描 - {datetime}

请对系统进行安全检查：
1. 依赖项漏洞扫描
2. 代码安全审查
3. 配置检查（API密钥、权限等）
4. 日志异常分析

生成安全报告，标记高风险项。""",
        "template_ids": [],
        "tags": ["daily", "security", "scan"],
    },
}


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_scheduler: TaskScheduler | None = None
_scheduler_lock = threading.Lock()


def init_scheduler(data_dir: str = "") -> TaskScheduler:
    """Initialize the global scheduler instance."""
    global _scheduler
    with _scheduler_lock:
        _scheduler = TaskScheduler(data_dir=data_dir)
    return _scheduler


def get_scheduler() -> TaskScheduler:
    """Get the global scheduler instance."""
    global _scheduler
    if _scheduler is None:
        with _scheduler_lock:
            if _scheduler is None:
                _scheduler = TaskScheduler()
    return _scheduler
