"""
Workflow — Agent 协同编排引擎 v2。

核心设计原则：
  1. 模板与实例分离 — WorkflowTemplate 定义抽象流程，WorkflowInstance 绑定 Agent 执行
  2. 结构化上下文 — StepContext 在步骤间传递结构化数据（文本 + 文件引用 + KV 元数据）
  3. 累积上下文 — 每个步骤可访问所有前序步骤的输出，而非仅上一步
  4. DAG 依赖 — 通过 depends_on 定义任意 DAG（不仅串行链），引擎自动调度并行/串行

架构：
  WorkflowTemplate (抽象流程)
    └── StepTemplate[] (步骤定义: name, description, input_spec, output_spec)
  WorkflowInstance (执行实例)
    └── StepInstance[] (运行时步骤: agent_id, status, context)
    └── WorkflowContext (累积上下文: 所有步骤的输入输出)
  WorkflowEngine (执行引擎)
    └── _run_instance() → 按 DAG 拓扑排序调度执行
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger("tudou.workflow")


# ─────────────────────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────────────────────

class StepStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class WorkflowStatus(str, Enum):
    DRAFT = "draft"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


# ─────────────────────────────────────────────────────────────
# StepContext — 步骤间通讯的结构化载体
# ─────────────────────────────────────────────────────────────

@dataclass
class StepContext:
    """
    步骤的输入/输出上下文。

    一个 StepContext 包含：
      - text:  主文本内容（Agent 的回复文本）
      - files: 文件引用列表 [{path, name, type, description}]
      - data:  结构化键值对（JSON-safe），用于传递参数、配置、中间变量
      - summary: 对 text 的摘要（可选，用于长文本传递时节省 token）
    """
    text: str = ""
    files: list[dict] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "files": self.files,
            "data": self.data,
            "summary": self.summary,
        }

    @staticmethod
    def from_dict(d: dict | None) -> StepContext:
        if not d:
            return StepContext()
        return StepContext(
            text=d.get("text", ""),
            files=d.get("files", []),
            data=d.get("data", {}),
            summary=d.get("summary", ""),
        )

    def to_prompt_block(self, step_name: str = "") -> str:
        """将上下文格式化为可嵌入 Prompt 的文本块。"""
        parts = []
        label = f"[{step_name}]" if step_name else "[Previous Step]"
        if self.summary:
            parts.append(f"{label} Summary:\n{self.summary}")
        if self.text:
            t = self.text
            # 如果有摘要，原文截断以节省 token
            if self.summary and len(t) > 2000:
                t = t[:2000] + "\n... (truncated, see summary above)"
            parts.append(f"{label} Output:\n{t}")
        if self.files:
            file_lines = [f"  - {f.get('name', f.get('path', '?'))}: {f.get('description', '')}"
                          for f in self.files]
            parts.append(f"{label} Files:\n" + "\n".join(file_lines))
        if self.data:
            parts.append(f"{label} Data:\n{json.dumps(self.data, ensure_ascii=False, indent=2)}")
        return "\n\n".join(parts) if parts else ""


# ─────────────────────────────────────────────────────────────
# WorkflowContext — 全局累积上下文
# ─────────────────────────────────────────────────────────────

@dataclass
class WorkflowContext:
    """
    工作流级别的累积上下文。记录每个已完成步骤的输出，
    后续步骤可按需引用任意前序步骤的输出。
    """
    original_input: str = ""                         # 用户原始输入
    step_outputs: dict[str, StepContext] = field(default_factory=dict)
    # key = step_id, value = StepContext
    shared_workspace: str = ""                       # 共享工作目录路径
    global_data: dict[str, Any] = field(default_factory=dict)
    # 跨步骤共享的全局 KV

    def set_step_output(self, step_id: str, ctx: StepContext):
        self.step_outputs[step_id] = ctx

    def get_step_output(self, step_id: str) -> StepContext | None:
        return self.step_outputs.get(step_id)

    def get_last_output(self, step_ids: list[str]) -> StepContext | None:
        """获取给定步骤列表中最后一个有输出的步骤上下文。"""
        for sid in reversed(step_ids):
            if sid in self.step_outputs:
                return self.step_outputs[sid]
        return None

    def build_context_prompt(self, for_step_deps: list[str],
                              all_steps: list = None) -> str:
        """
        为某个步骤构建上下文 Prompt。
        只包含该步骤 depends_on 指定的前序步骤输出。
        """
        parts = []
        if self.original_input:
            parts.append(f"[Original Request]\n{self.original_input}")

        # 构建步骤名称映射
        step_name_map = {}
        if all_steps:
            for s in all_steps:
                sid = s.id if hasattr(s, 'id') else s.get('id', '')
                sname = s.name if hasattr(s, 'name') else s.get('name', '')
                step_name_map[sid] = sname

        for dep_id in for_step_deps:
            ctx = self.step_outputs.get(dep_id)
            if ctx:
                name = step_name_map.get(dep_id, dep_id)
                parts.append(ctx.to_prompt_block(step_name=name))

        if self.global_data:
            parts.append(f"[Shared Data]\n{json.dumps(self.global_data, ensure_ascii=False, indent=2)}")

        return "\n\n---\n\n".join(parts)

    def to_dict(self) -> dict:
        return {
            "original_input": self.original_input[:500],
            "step_outputs": {k: v.to_dict() for k, v in self.step_outputs.items()},
            "shared_workspace": self.shared_workspace,
            "global_data": self.global_data,
        }

    @staticmethod
    def from_dict(d: dict | None) -> WorkflowContext:
        if not d:
            return WorkflowContext()
        return WorkflowContext(
            original_input=d.get("original_input", ""),
            step_outputs={k: StepContext.from_dict(v) for k, v in d.get("step_outputs", {}).items()},
            shared_workspace=d.get("shared_workspace", ""),
            global_data=d.get("global_data", {}),
        )


# ─────────────────────────────────────────────────────────────
# Step 定义 — Template (抽象) + Instance (运行时)
# ─────────────────────────────────────────────────────────────

@dataclass
class StepTemplate:
    """
    流程步骤模板（不绑定 Agent）。

    定义步骤做什么，而非谁来做。
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    name: str = ""
    description: str = ""              # 步骤描述：这一步做什么
    prompt_template: str = ""          # 提示词模板，支持 {input} {context} {prev_result} 等
    input_spec: str = ""               # 输入规格描述
    output_spec: str = ""              # 输出规格描述
    depends_on: list[str] = field(default_factory=list)   # 依赖的步骤 ID 列表
    condition: str = ""                # 执行条件
    skip_condition: str = ""           # 跳过条件
    max_retries: int = 1
    # 角色提示 — 建议用什么角色的 Agent 来执行（非强制绑定）
    suggested_role: str = ""
    # ── Stage-aware file pinning (added 2026-05-05) ──
    # Pre-declared file paths (relative to project's shared workspace).
    # bind_workflow propagates these onto the resulting ProjectTask
    # so the dispatcher's "## 📂 Pinned input files" block carries them.
    input_files: list[str] = field(default_factory=list)
    output_files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "prompt_template": self.prompt_template,
            "input_spec": self.input_spec,
            "output_spec": self.output_spec,
            "depends_on": self.depends_on,
            "condition": self.condition,
            "skip_condition": self.skip_condition,
            "max_retries": self.max_retries,
            "suggested_role": self.suggested_role,
            "input_files": list(self.input_files or []),
            "output_files": list(self.output_files or []),
        }

    @staticmethod
    def from_dict(d: dict) -> StepTemplate:
        return StepTemplate(
            id=d.get("id", uuid.uuid4().hex[:8]),
            name=d.get("name", ""),
            description=d.get("description", ""),
            prompt_template=d.get("prompt_template", ""),
            input_spec=d.get("input_spec", d.get("input_desc", "")),
            output_spec=d.get("output_spec", d.get("output_desc", "")),
            depends_on=d.get("depends_on", []),
            condition=d.get("condition", ""),
            skip_condition=d.get("skip_condition", ""),
            max_retries=d.get("max_retries", 1),
            suggested_role=d.get("suggested_role", d.get("role", "")),
            input_files=list(d.get("input_files") or []),
            output_files=list(d.get("output_files") or []),
        )


@dataclass
class StepInstance:
    """
    步骤运行时实例（绑定具体 Agent，含执行状态）。
    """
    id: str = ""                       # 对应 StepTemplate.id
    template: StepTemplate = field(default_factory=StepTemplate)
    agent_id: str = ""                 # 执行此步骤的 Agent ID
    status: StepStatus = StepStatus.PENDING
    output: StepContext = field(default_factory=StepContext)   # 步骤输出
    error: str = ""
    started_at: float = 0
    finished_at: float = 0
    retries: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.template.name,
            "description": self.template.description,
            "agent_id": self.agent_id,
            "status": self.status.value,
            "output": self.output.to_dict(),
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "retries": self.retries,
            "max_retries": self.template.max_retries,
            "input_spec": self.template.input_spec,
            "output_spec": self.template.output_spec,
            "depends_on": self.template.depends_on,
            "suggested_role": self.template.suggested_role,
            # Compat: keep result field for frontend
            "result": self.output.text[:2000] if self.output.text else "",
        }

    @staticmethod
    def from_dict(d: dict) -> StepInstance:
        tmpl = StepTemplate(
            id=d.get("id", uuid.uuid4().hex[:8]),
            name=d.get("name", ""),
            description=d.get("description", ""),
            prompt_template=d.get("prompt_template", ""),
            input_spec=d.get("input_spec", ""),
            output_spec=d.get("output_spec", ""),
            depends_on=d.get("depends_on", []),
            condition=d.get("condition", ""),
            skip_condition=d.get("skip_condition", ""),
            max_retries=d.get("max_retries", 1),
            suggested_role=d.get("suggested_role", ""),
        )
        return StepInstance(
            id=tmpl.id,
            template=tmpl,
            agent_id=d.get("agent_id", ""),
            status=StepStatus(d.get("status", "pending")),
            output=StepContext.from_dict(d.get("output")),
            error=d.get("error", ""),
            started_at=d.get("started_at", 0),
            finished_at=d.get("finished_at", 0),
            retries=d.get("retries", 0),
        )


# ─────────────────────────────────────────────────────────────
# WorkflowTemplate — 抽象流程定义
# ─────────────────────────────────────────────────────────────

@dataclass
class WorkflowTemplate:
    """
    工作流模板 — 定义抽象流程，不绑定任何 Agent。
    可复用于多个项目/任务。
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str = ""
    description: str = ""
    steps: list[StepTemplate] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    # ── Scheduling (P2 #4 dual-mode scheduler) ──
    schedule_enabled: bool = False
    # One of: "manual" (default on-demand), "interval", "cron"
    schedule_mode: str = "manual"
    schedule_interval_sec: int = 0            # used when mode=interval
    schedule_cron: str = ""                   # used when mode=cron
    schedule_default_input: str = ""          # input_data to pass on auto-fire
    schedule_last_run_at: float = 0.0
    schedule_next_run_at: float = 0.0
    schedule_run_count: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "steps": [s.to_dict() for s in self.steps],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            # Compat fields
            "status": "template",
            # Schedule fields
            "schedule_enabled": self.schedule_enabled,
            "schedule_mode": self.schedule_mode,
            "schedule_interval_sec": self.schedule_interval_sec,
            "schedule_cron": self.schedule_cron,
            "schedule_default_input": self.schedule_default_input,
            "schedule_last_run_at": self.schedule_last_run_at,
            "schedule_next_run_at": self.schedule_next_run_at,
            "schedule_run_count": self.schedule_run_count,
        }

    @staticmethod
    def from_dict(d: dict) -> WorkflowTemplate:
        return WorkflowTemplate(
            id=d.get("id", uuid.uuid4().hex[:10]),
            name=d.get("name", ""),
            description=d.get("description", ""),
            steps=[StepTemplate.from_dict(s) for s in d.get("steps", [])],
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
            schedule_enabled=bool(d.get("schedule_enabled", False)),
            schedule_mode=str(d.get("schedule_mode", "manual") or "manual"),
            schedule_interval_sec=int(d.get("schedule_interval_sec", 0) or 0),
            schedule_cron=str(d.get("schedule_cron", "") or ""),
            schedule_default_input=str(d.get("schedule_default_input", "") or ""),
            schedule_last_run_at=float(d.get("schedule_last_run_at", 0.0) or 0.0),
            schedule_next_run_at=float(d.get("schedule_next_run_at", 0.0) or 0.0),
            schedule_run_count=int(d.get("schedule_run_count", 0) or 0),
        )

    # ── Scheduler helpers ──

    def compute_next_run(self, now: float | None = None) -> float:
        """Return the next epoch time this template should fire, or 0 if disabled."""
        if not self.schedule_enabled:
            return 0.0
        now = now or time.time()
        mode = (self.schedule_mode or "manual").lower()
        if mode == "interval":
            if self.schedule_interval_sec <= 0:
                return 0.0
            base = self.schedule_last_run_at or now
            nxt = base + self.schedule_interval_sec
            if nxt <= now:
                nxt = now + self.schedule_interval_sec
            return nxt
        if mode == "cron":
            # Minimal cron: support "*/N * * * *" (every N minutes) as phase-1.
            try:
                parts = (self.schedule_cron or "").strip().split()
                if len(parts) == 5 and parts[0].startswith("*/"):
                    n = int(parts[0][2:])
                    if n > 0:
                        return now + n * 60
            except Exception:
                pass
            return 0.0
        return 0.0


# ─────────────────────────────────────────────────────────────
# WorkflowInstance — 流程执行实例
# ─────────────────────────────────────────────────────────────

@dataclass
class WorkflowInstance:
    """
    工作流执行实例。从 WorkflowTemplate 创建，绑定具体 Agent。

    一个 Template 可以创建多个 Instance（不同的 Agent 组合执行同一流程）。
    """
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    template_id: str = ""               # 对应的 WorkflowTemplate ID
    name: str = ""
    description: str = ""
    status: WorkflowStatus = WorkflowStatus.DRAFT
    steps: list[StepInstance] = field(default_factory=list)
    context: WorkflowContext = field(default_factory=WorkflowContext)
    current_step_index: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    # 事件流（供前端订阅）
    events: list[dict] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def push_event(self, evt: dict):
        evt.setdefault("ts", time.time())
        with self._lock:
            self.events.append(evt)
            self.updated_at = time.time()

    def get_events_since(self, cursor: int) -> tuple[list[dict], int]:
        with self._lock:
            return self.events[cursor:], len(self.events)

    @property
    def progress(self) -> int:
        if not self.steps:
            return 0
        done = sum(1 for s in self.steps
                   if s.status in (StepStatus.COMPLETED, StepStatus.SKIPPED))
        return int(done / len(self.steps) * 100)

    @property
    def final_result(self) -> str:
        """最后一个完成步骤的输出文本。"""
        for s in reversed(self.steps):
            if s.status == StepStatus.COMPLETED and s.output.text:
                return s.output.text
        return ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "template_id": self.template_id,
            "name": self.name,
            "description": self.description,
            "status": self.status.value,
            "steps": [s.to_dict() for s in self.steps],
            "context": self.context.to_dict(),
            "current_step_index": self.current_step_index,
            "progress": self.progress,
            "final_result": self.final_result[:2000],
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(d: dict) -> WorkflowInstance:
        return WorkflowInstance(
            id=d.get("id", uuid.uuid4().hex[:10]),
            template_id=d.get("template_id", ""),
            name=d.get("name", ""),
            description=d.get("description", ""),
            status=WorkflowStatus(d.get("status", "draft")),
            steps=[StepInstance.from_dict(s) for s in d.get("steps", [])],
            context=WorkflowContext.from_dict(d.get("context")),
            current_step_index=d.get("current_step_index", 0),
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
        )


# ─────────────────────────────────────────────────────────────
# Backward-compat aliases — 旧代码的 Workflow / WorkflowStep
# ─────────────────────────────────────────────────────────────

class WorkflowStep:
    """Compatibility wrapper: old WorkflowStep → new StepInstance."""

    def __init__(self, **kwargs):
        self._inst = StepInstance.from_dict(kwargs)

    def __getattr__(self, name):
        if name == '_inst':
            raise AttributeError
        # Map old fields
        if name == 'result':
            return self._inst.output.text
        if name == 'agent_id':
            return self._inst.agent_id
        if name == 'prompt_template':
            return self._inst.template.prompt_template
        if name == 'parallel_with':
            return []
        return getattr(self._inst, name, "")

    def to_dict(self):
        return self._inst.to_dict()

    @staticmethod
    def from_dict(d: dict) -> WorkflowStep:
        ws = WorkflowStep.__new__(WorkflowStep)
        ws._inst = StepInstance.from_dict(d)
        return ws


class Workflow:
    """Compatibility wrapper: old Workflow → new WorkflowInstance."""

    def __init__(self, **kwargs):
        self._instance = WorkflowInstance(
            name=kwargs.get("name", ""),
            description=kwargs.get("description", ""),
            status=WorkflowStatus(kwargs.get("status", "draft")),
        )
        if "steps" in kwargs:
            self._instance.steps = [StepInstance.from_dict(
                s.to_dict() if hasattr(s, 'to_dict') else s
            ) for s in kwargs["steps"]]
        if "input_data" in kwargs:
            self._instance.context.original_input = kwargs["input_data"]
        self._instance.id = kwargs.get("id", self._instance.id)

    @property
    def id(self): return self._instance.id

    @property
    def name(self): return self._instance.name

    @name.setter
    def name(self, v): self._instance.name = v

    @property
    def description(self): return self._instance.description

    @description.setter
    def description(self, v): self._instance.description = v

    @property
    def status(self): return self._instance.status

    @status.setter
    def status(self, v): self._instance.status = v

    @property
    def steps(self): return self._instance.steps

    @property
    def current_step_index(self): return self._instance.current_step_index

    @current_step_index.setter
    def current_step_index(self, v): self._instance.current_step_index = v

    @property
    def input_data(self): return self._instance.context.original_input

    @property
    def final_result(self): return self._instance.final_result

    @final_result.setter
    def final_result(self, v): pass  # Computed from steps now

    @property
    def progress(self): return self._instance.progress

    def push_event(self, evt): self._instance.push_event(evt)
    def get_events_since(self, c): return self._instance.get_events_since(c)

    def to_dict(self):
        d = self._instance.to_dict()
        d["input_data"] = d.get("context", {}).get("original_input", "")[:500]
        return d

    @staticmethod
    def from_dict(d: dict) -> Workflow:
        wf = Workflow.__new__(Workflow)
        wf._instance = WorkflowInstance.from_dict(d)
        return wf


# ─────────────────────────────────────────────────────────────
# WorkflowEngine v2
# ─────────────────────────────────────────────────────────────

class WorkflowEngine:
    """
    执行引擎 v2：
      - 管理 WorkflowTemplate（模板库）
      - 从模板 + Agent 分配创建 WorkflowInstance
      - 按 DAG 拓扑排序驱动步骤执行
      - 通过 WorkflowContext 在步骤间传递结构化数据

    需要注入：
        agent_chat_fn(agent_id: str, message: str) -> str
    """

    def __init__(self, agent_chat_fn: Callable[[str, str], str]):
        self._chat = agent_chat_fn
        self._templates: dict[str, WorkflowTemplate] = {}
        self._instances: dict[str, WorkflowInstance] = {}
        # Compat: _workflows points to _instances wrapped in Workflow
        self._workflows: dict[str, Workflow] = {}
        self._lock = threading.Lock()
        # 步骤完成回调: fn(template_id, step_index, step_id, status)
        self._on_step_complete: Callable | None = None

    # ── Catalog 相关 ──

    def list_catalog(self) -> list[dict]:
        """返回预置 Workflow Catalog 模板列表。"""
        try:
            from ..data.workflow_catalog import list_catalog_templates
            return list_catalog_templates()
        except ImportError:
            try:
                from app.data.workflow_catalog import list_catalog_templates
                return list_catalog_templates()
            except ImportError:
                return []

    def get_catalog_categories(self) -> dict:
        """按分类返回 Catalog 模板。"""
        try:
            from ..data.workflow_catalog import get_catalog_categories
            return get_catalog_categories()
        except ImportError:
            try:
                from app.data.workflow_catalog import get_catalog_categories
                return get_catalog_categories()
            except ImportError:
                return {}

    def create_from_catalog(self, catalog_id: str,
                            custom_name: str = "") -> WorkflowTemplate | None:
        """从 Catalog 创建一个可编辑的 WorkflowTemplate 副本。"""
        try:
            from ..data.workflow_catalog import get_catalog_template
        except ImportError:
            try:
                from app.data.workflow_catalog import get_catalog_template
            except ImportError:
                return None

        cat_tmpl = get_catalog_template(catalog_id)
        if not cat_tmpl:
            return None

        # 创建副本（新 ID），用户可自由编辑
        name = custom_name or cat_tmpl["name"]
        return self.create_template(
            name=name,
            description=cat_tmpl.get("description", ""),
            steps=cat_tmpl.get("steps", []),
        )

    # ── Template 管理 ──

    def create_template(self, name: str, description: str,
                        steps: list[dict]) -> WorkflowTemplate:
        """创建工作流模板。"""
        tmpl = WorkflowTemplate(
            name=name,
            description=description,
            steps=[StepTemplate.from_dict(s) for s in steps],
        )
        # 自动为串行步骤补 depends_on
        for i, step in enumerate(tmpl.steps):
            if i > 0 and not step.depends_on:
                step.depends_on = [tmpl.steps[i - 1].id]
        with self._lock:
            self._templates[tmpl.id] = tmpl
        self.save()
        return tmpl

    def get_template(self, tmpl_id: str) -> WorkflowTemplate | None:
        return self._templates.get(tmpl_id)

    def list_templates(self) -> list[dict]:
        with self._lock:
            return [t.to_dict() for t in self._templates.values()]

    def update_template(self, tmpl_id: str, **kwargs) -> WorkflowTemplate | None:
        tmpl = self._templates.get(tmpl_id)
        if not tmpl:
            return None
        if "name" in kwargs:
            tmpl.name = kwargs["name"]
        if "description" in kwargs:
            tmpl.description = kwargs["description"]
        if "steps" in kwargs:
            tmpl.steps = [StepTemplate.from_dict(s) for s in kwargs["steps"]]
        tmpl.updated_at = time.time()
        return tmpl

    def delete_template(self, tmpl_id: str) -> bool:
        with self._lock:
            return self._templates.pop(tmpl_id, None) is not None

    # ── Instance 管理 ──

    def create_instance(self, template_id: str,
                        step_assignments: list[dict],
                        input_data: str = "",
                        shared_workspace: str = "") -> WorkflowInstance | None:
        """
        从模板创建执行实例。

        step_assignments: [{step_index: int, agent_id: str}, ...]
        """
        tmpl = self._templates.get(template_id)
        if not tmpl:
            return None

        # 构建 step → agent 映射
        agent_map = {}
        for sa in step_assignments:
            idx = sa.get("step_index")
            aid = sa.get("agent_id", "")
            if idx is not None and aid:
                agent_map[idx] = aid

        # 创建步骤实例
        step_instances = []
        for i, st in enumerate(tmpl.steps):
            si = StepInstance(
                id=st.id,
                template=deepcopy(st),
                agent_id=agent_map.get(i, ""),
            )
            step_instances.append(si)

        inst = WorkflowInstance(
            template_id=template_id,
            name=tmpl.name,
            description=tmpl.description,
            steps=step_instances,
            context=WorkflowContext(
                original_input=input_data,
                shared_workspace=shared_workspace,
            ),
        )
        with self._lock:
            self._instances[inst.id] = inst
        self.save()
        return inst

    def tick_scheduler(self, now: float | None = None) -> list[str]:
        """Scheduler tick: fire any due templates.

        Returns the list of instance IDs that were started as a result.
        Called periodically from Hub._heartbeat_loop.
        """
        now = now or time.time()
        fired: list[str] = []
        with self._lock:
            tmpls = list(self._templates.values())
        for tmpl in tmpls:
            try:
                if not getattr(tmpl, "schedule_enabled", False):
                    continue
                nxt = tmpl.schedule_next_run_at or tmpl.compute_next_run(now)
                if nxt <= 0:
                    continue
                if now < nxt:
                    continue
                # Fire: create instance from template using any available agents.
                # We don't know assignments at schedule time, so pass empty —
                # callers using schedule should have pre-assigned via template
                # `preferred_agent` (future enhancement).
                inst = self.create_instance(
                    template_id=tmpl.id,
                    step_assignments=[],
                    input_data=tmpl.schedule_default_input or "",
                )
                if inst is None:
                    continue
                self.start_instance(inst.id)
                tmpl.schedule_last_run_at = now
                tmpl.schedule_run_count += 1
                tmpl.schedule_next_run_at = tmpl.compute_next_run(now)
                fired.append(inst.id)
            except Exception as _e:
                logger.debug("scheduler tick failed for %s: %s",
                             getattr(tmpl, "id", "?"), _e)
        if fired:
            try:
                self.save()
            except Exception:
                pass
        return fired

    def get_instance(self, inst_id: str) -> WorkflowInstance | None:
        return self._instances.get(inst_id)

    def list_instances(self) -> list[dict]:
        with self._lock:
            return [inst.to_dict() for inst in self._instances.values()]

    def start_instance(self, inst_id: str) -> bool:
        """启动工作流实例（异步后台线程）。"""
        inst = self._instances.get(inst_id)
        if not inst or inst.status == WorkflowStatus.RUNNING:
            return False
        inst.status = WorkflowStatus.RUNNING
        inst.push_event({"type": "workflow_start", "name": inst.name})
        t = threading.Thread(target=self._run_instance, args=(inst,), daemon=True)
        t.start()
        return True

    def abort_instance(self, inst_id: str) -> bool:
        inst = self._instances.get(inst_id)
        if not inst:
            return False
        inst.status = WorkflowStatus.ABORTED
        inst.push_event({"type": "workflow_abort"})
        return True

    # ── Compat: old API → new API mapping ──

    def create_workflow(self, name: str, description: str,
                        steps: list[dict], input_data: str = "") -> Workflow:
        """Compat: create_workflow → create_template (if no agent_ids) or instance."""
        # Check if steps have agent_id — if so, create instance-style
        has_agents = any(s.get("agent_id") for s in steps)
        if has_agents:
            # Old-style: create both template + instance
            tmpl = self.create_template(name, description, steps)
            assignments = []
            for i, s in enumerate(steps):
                if s.get("agent_id"):
                    assignments.append({"step_index": i, "agent_id": s["agent_id"]})
            inst = self.create_instance(tmpl.id, assignments, input_data)
            if inst:
                wf = Workflow.__new__(Workflow)
                wf._instance = inst
                self._workflows[inst.id] = wf
                return wf
        # No agents: just create template, wrap as Workflow for compat
        tmpl = self.create_template(name, description, steps)
        wf = Workflow(name=name, description=description, steps=steps)
        wf._instance.id = tmpl.id
        wf._instance.template_id = tmpl.id
        self._workflows[tmpl.id] = wf
        return wf

    def get_workflow(self, wf_id: str) -> Workflow | None:
        return self._workflows.get(wf_id)

    def list_workflows(self) -> list[dict]:
        """Compat: list all (templates shown as 'template' status, instances shown with real status)."""
        results = []
        with self._lock:
            # Templates
            for t in self._templates.values():
                results.append(t.to_dict())
            # Running instances
            for inst in self._instances.values():
                results.append(inst.to_dict())
        return results

    def start_workflow(self, wf_id: str) -> bool:
        """Compat: start_workflow → start_instance."""
        if wf_id in self._instances:
            return self.start_instance(wf_id)
        return False

    def abort_workflow(self, wf_id: str) -> bool:
        if wf_id in self._instances:
            return self.abort_instance(wf_id)
        return False

    def delete_workflow(self, wf_id: str) -> bool:
        with self._lock:
            removed = False
            if wf_id in self._templates:
                del self._templates[wf_id]
                removed = True
            if wf_id in self._instances:
                del self._instances[wf_id]
                removed = True
            if wf_id in self._workflows:
                del self._workflows[wf_id]
                removed = True
        if removed:
            self.save()
        return removed

    # ── 持久化 ──

    def set_data_dir(self, data_dir: str):
        """设置数据目录，用于持久化。"""
        self._data_dir = data_dir

    def _get_db(self):
        try:
            from .infra.database import get_database
            return get_database()
        except Exception:
            return None

    def save(self):
        """将模板和实例持久化到 SQLite + JSON backup。"""
        # SQLite primary
        db = self._get_db()
        if db:
            try:
                for k, v in self._templates.items():
                    d = v.to_dict()
                    d.setdefault("template_id", k)
                    db.save_workflow_template(d)
                for k, v in self._instances.items():
                    d = v.to_dict()
                    d.setdefault("instance_id", k)
                    db.save_workflow_instance(d)
            except Exception as e:
                logger.warning(f"SQLite workflow save failed: {e}")
        # JSON backup
        data_dir = getattr(self, '_data_dir', '')
        if not data_dir:
            return
        import json as _json
        path = os.path.join(data_dir, "workflows.json")
        payload = {
            "templates": {k: v.to_dict() for k, v in self._templates.items()},
            "instances": {k: v.to_dict() for k, v in self._instances.items()},
        }
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                _json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.error(f"Failed to save workflows JSON backup: {e}")

    def load(self):
        """从 SQLite (primary) 或 JSON (fallback) 加载模板和实例。"""
        db = self._get_db()
        if db:
            tpl_count = db.count("workflow_templates")
            inst_count = db.count("workflow_instances")
            if tpl_count > 0 or inst_count > 0:
                try:
                    with self._lock:
                        for d in db.load_workflow_templates():
                            tid = d.get("template_id", d.get("id", ""))
                            self._templates[tid] = WorkflowTemplate.from_dict(d)
                        for d in db.load_workflow_instances():
                            iid = d.get("instance_id", d.get("id", ""))
                            self._instances[iid] = WorkflowInstance.from_dict(d)
                    logger.info(f"Loaded {len(self._templates)} templates, "
                                f"{len(self._instances)} instances from SQLite")
                    return
                except Exception as e:
                    logger.warning(f"SQLite workflow load failed: {e}")
        # JSON fallback
        data_dir = getattr(self, '_data_dir', '')
        if not data_dir:
            return
        import json as _json
        path = os.path.join(data_dir, "workflows.json")
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = _json.load(f)
            with self._lock:
                for k, v in data.get("templates", {}).items():
                    self._templates[k] = WorkflowTemplate.from_dict(v)
                for k, v in data.get("instances", {}).items():
                    self._instances[k] = WorkflowInstance.from_dict(v)
            logger.info(f"Loaded {len(self._templates)} templates, "
                        f"{len(self._instances)} instances from disk")
        except Exception as e:
            logger.error(f"Failed to load workflows: {e}")

        # Rebuild _workflows compat dict from loaded templates/instances
        self._rebuild_compat_workflows()

    def _rebuild_compat_workflows(self):
        """Rebuild the _workflows compat dict from _templates and _instances."""
        with self._lock:
            self._workflows.clear()
            # Map instance → template
            inst_template_map = {}
            for inst in self._instances.values():
                inst_template_map[inst.template_id] = inst
            # For each template, create a Workflow wrapper
            for tid, tmpl in self._templates.items():
                inst = inst_template_map.get(tid)
                if inst:
                    wf = Workflow.__new__(Workflow)
                    wf._instance = inst
                    self._workflows[inst.id] = wf
                else:
                    # Template-only (no running instance)
                    wf = Workflow(
                        name=tmpl.name,
                        description=tmpl.description,
                        steps=[{"name": s.name, "description": s.description}
                               for s in tmpl.steps],
                    )
                    wf._instance.id = tid
                    wf._instance.template_id = tid
                    self._workflows[tid] = wf
            logger.info(f"Rebuilt {len(self._workflows)} compat workflows")

    # ── 执行引擎核心 ──

    def _run_instance(self, inst: WorkflowInstance):
        """
        按 DAG 拓扑排序执行步骤。
        每步执行时，从 WorkflowContext 中提取依赖步骤的输出构建 Prompt。
        """
        try:
            # 拓扑排序
            order = self._topo_sort(inst.steps)

            for step_idx in order:
                if inst.status == WorkflowStatus.ABORTED:
                    break

                step = inst.steps[step_idx]
                inst.current_step_index = step_idx

                # 条件检查
                if self._should_skip(step, inst.context):
                    step.status = StepStatus.SKIPPED
                    inst.push_event({
                        "type": "step_skip",
                        "step_id": step.id,
                        "step_name": step.template.name,
                    })
                    continue

                # 没有分配 Agent → 跳过
                if not step.agent_id:
                    step.status = StepStatus.SKIPPED
                    step.error = "No agent assigned"
                    inst.push_event({
                        "type": "step_skip",
                        "step_id": step.id,
                        "step_name": step.template.name,
                        "reason": "No agent assigned",
                    })
                    continue

                # 找到并行兄弟步骤（共享相同 depends_on 且都 PENDING）
                siblings = self._find_parallel_siblings(step, inst.steps, order, step_idx)

                if siblings:
                    # 并行执行
                    all_steps = [step] + siblings
                    self._run_parallel(inst, all_steps)
                else:
                    # 串行执行
                    self._run_step(inst, step)

                if step.status == StepStatus.FAILED:
                    inst.status = WorkflowStatus.FAILED
                    inst.push_event({
                        "type": "workflow_fail",
                        "step_id": step.id,
                        "error": step.error,
                    })
                    return

            # 全部完成
            if inst.status == WorkflowStatus.RUNNING:
                inst.status = WorkflowStatus.COMPLETED
                inst.push_event({
                    "type": "workflow_complete",
                    "progress": 100,
                    "result_preview": inst.final_result[:500],
                })

        except Exception as e:
            logger.error(f"Workflow instance {inst.id} error: {e}", exc_info=True)
            inst.status = WorkflowStatus.FAILED
            inst.push_event({"type": "workflow_error", "error": str(e)})
        finally:
            self.save()  # 持久化终态

    def _run_step(self, inst: WorkflowInstance, step: StepInstance):
        """执行单个步骤（带重试），输出写入 WorkflowContext。"""
        step.status = StepStatus.RUNNING
        step.started_at = time.time()
        inst.push_event({
            "type": "step_start",
            "step_id": step.id,
            "step_name": step.template.name,
            "agent_id": step.agent_id,
            "progress": inst.progress,
        })

        prompt = self._build_prompt(inst, step)
        last_error = ""

        for attempt in range(step.template.max_retries + 1):
            if inst.status == WorkflowStatus.ABORTED:
                step.status = StepStatus.FAILED
                step.error = "Workflow aborted"
                return

            try:
                result_text = self._chat(step.agent_id, prompt)

                # 解析结构化输出
                output_ctx = self._parse_output(result_text)
                step.output = output_ctx
                step.status = StepStatus.COMPLETED
                step.finished_at = time.time()
                step.retries = attempt

                # 写入全局上下文
                inst.context.set_step_output(step.id, output_ctx)

                inst.push_event({
                    "type": "step_complete",
                    "step_id": step.id,
                    "step_name": step.template.name,
                    "agent_id": step.agent_id,
                    "duration": step.finished_at - step.started_at,
                    "result_preview": result_text[:300],
                    "progress": inst.progress,
                    "has_files": len(output_ctx.files) > 0,
                    "has_data": len(output_ctx.data) > 0,
                })
                # 触发回调，同步到 Project Task
                if self._on_step_complete:
                    try:
                        step_idx = next(
                            (i for i, s in enumerate(inst.steps) if s.id == step.id), -1)
                        self._on_step_complete(
                            inst.template_id, step_idx, step.id, "done")
                    except Exception as cb_err:
                        logger.warning("step_complete callback error: %s", cb_err)
                return

            except Exception as e:
                last_error = str(e)
                step.retries = attempt + 1
                inst.push_event({
                    "type": "step_retry",
                    "step_id": step.id,
                    "attempt": attempt + 1,
                    "error": last_error,
                })
                time.sleep(2)

        step.status = StepStatus.FAILED
        step.error = last_error
        step.finished_at = time.time()
        # 触发回调，通知失败
        if self._on_step_complete:
            try:
                step_idx = next(
                    (i for i, s in enumerate(inst.steps) if s.id == step.id), -1)
                self._on_step_complete(
                    inst.template_id, step_idx, step.id, "blocked")
            except Exception as cb_err:
                logger.warning("step_complete callback error: %s", cb_err)

    def _run_parallel(self, inst: WorkflowInstance, steps: list[StepInstance]):
        """并行执行多个步骤。"""
        threads = []
        for step in steps:
            t = threading.Thread(
                target=self._run_step,
                args=(inst, step),
                daemon=True,
            )
            threads.append(t)
            t.start()
        for t in threads:
            t.join(timeout=600)

    def _build_prompt(self, inst: WorkflowInstance, step: StepInstance) -> str:
        """
        为步骤构建 Prompt。

        优先使用模板中的 prompt_template，支持占位符：
          {input}       — 用户原始输入
          {context}     — 依赖步骤的输出（结构化格式）
          {prev_result} — 上一步的纯文本输出（兼容旧模板）
          {step_name}   — 当前步骤名称

        如果没有 prompt_template，自动构建包含上下文的 Prompt。
        """
        deps = step.template.depends_on or []
        context_text = inst.context.build_context_prompt(deps, all_steps=inst.steps)

        # 获取上一步输出（兼容 {prev_result}）
        prev_result = ""
        if deps:
            last_ctx = inst.context.get_last_output(deps)
            if last_ctx:
                prev_result = last_ctx.text

        template = step.template.prompt_template
        if template:
            prompt = template.replace("{input}", inst.context.original_input or "")
            prompt = prompt.replace("{context}", context_text or "")
            prompt = prompt.replace("{prev_result}", prev_result or "")
            prompt = prompt.replace("{step_name}", step.template.name or "")
            return prompt

        # 自动构建 Prompt
        parts = []
        if step.template.description:
            parts.append(f"## Task: {step.template.name}\n{step.template.description}")
        else:
            parts.append(f"## Task: {step.template.name}")

        if step.template.input_spec:
            parts.append(f"## Expected Input\n{step.template.input_spec}")

        if step.template.output_spec:
            parts.append(f"## Expected Output\n{step.template.output_spec}")

        if context_text:
            parts.append(f"## Context from Previous Steps\n{context_text}")
        elif inst.context.original_input:
            parts.append(f"## Original Request\n{inst.context.original_input}")

        return "\n\n".join(parts)

    def _parse_output(self, result_text: str) -> StepContext:
        """
        尝试从 Agent 输出中解析结构化数据。

        如果 Agent 输出包含 ```json ... ``` 块，尝试解析为 StepContext.data。
        如果包含文件路径引用，提取到 StepContext.files。
        """
        ctx = StepContext(text=result_text)

        # 尝试提取 JSON 块
        import re
        json_blocks = re.findall(r'```json\s*\n(.*?)```', result_text, re.DOTALL)
        for block in json_blocks:
            try:
                parsed = json.loads(block.strip())
                if isinstance(parsed, dict):
                    ctx.data.update(parsed)
            except json.JSONDecodeError:
                pass

        # 提取文件路径引用
        file_patterns = re.findall(
            r'(?:file|path|output)[:：]\s*[`"]?([/~][^\s`"]+)[`"]?',
            result_text, re.IGNORECASE
        )
        for fp in file_patterns:
            if os.path.splitext(fp)[1]:  # 有扩展名的才算
                ctx.files.append({
                    "path": fp,
                    "name": os.path.basename(fp),
                    "type": os.path.splitext(fp)[1].lstrip('.'),
                })

        # 生成摘要（如果文本很长）
        if len(result_text) > 3000:
            # 简单截取前 500 字作为摘要（未来可换为 LLM 摘要）
            ctx.summary = result_text[:500] + "..."

        return ctx

    def _should_skip(self, step: StepInstance, context: WorkflowContext) -> bool:
        """检查步骤是否应跳过。"""
        tmpl = step.template

        if tmpl.condition:
            # 检查依赖步骤的输出是否包含条件关键词
            for dep_id in tmpl.depends_on:
                dep_ctx = context.get_step_output(dep_id)
                if dep_ctx and tmpl.condition.lower() not in dep_ctx.text.lower():
                    return True

        if tmpl.skip_condition:
            for dep_id in tmpl.depends_on:
                dep_ctx = context.get_step_output(dep_id)
                if dep_ctx and tmpl.skip_condition.lower() in dep_ctx.text.lower():
                    return True

        return False

    def _find_parallel_siblings(self, step: StepInstance,
                                 all_steps: list[StepInstance],
                                 order: list[int],
                                 current_pos: int) -> list[StepInstance]:
        """
        查找可以与当前步骤并行执行的兄弟步骤。
        条件：相同的 depends_on，且都处于 PENDING 状态。
        """
        siblings = []
        my_deps = set(step.template.depends_on)
        for idx in order[current_pos + 1:]:
            s = all_steps[idx]
            if s.status != StepStatus.PENDING:
                continue
            if set(s.template.depends_on) == my_deps and s.agent_id:
                siblings.append(s)
        return siblings

    def _topo_sort(self, steps: list[StepInstance]) -> list[int]:
        """DAG 拓扑排序，返回步骤索引列表。"""
        n = len(steps)
        id_to_idx = {s.id: i for i, s in enumerate(steps)}
        in_degree = [0] * n
        adj = [[] for _ in range(n)]

        for i, s in enumerate(steps):
            for dep_id in s.template.depends_on:
                dep_idx = id_to_idx.get(dep_id)
                if dep_idx is not None:
                    adj[dep_idx].append(i)
                    in_degree[i] += 1

        # Kahn's algorithm
        queue = [i for i in range(n) if in_degree[i] == 0]
        order = []
        while queue:
            # Sort queue for deterministic order
            queue.sort()
            node = queue.pop(0)
            order.append(node)
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        # 如果有环，追加剩余步骤
        if len(order) < n:
            remaining = [i for i in range(n) if i not in order]
            order.extend(remaining)

        return order


# ─────────────────────────────────────────────────────────────
# 预置流水线模板
# ─────────────────────────────────────────────────────────────

WORKFLOW_TEMPLATES: dict[str, dict] = {
    "code_review_pipeline": {
        "name": "代码审查流水线",
        "description": "需求 → 设计 → 开发 → 审查 → 测试",
        "steps": [
            {
                "name": "需求分析",
                "description": "分析用户需求，输出用户故事和验收标准",
                "suggested_role": "pm",
                "input_spec": "原始需求文本",
                "output_spec": "用户故事列表 + 验收标准",
                "prompt_template": "分析以下需求，输出用户故事和验收标准：\n\n{input}",
            },
            {
                "name": "方案设计",
                "description": "根据需求分析给出技术方案和架构设计",
                "suggested_role": "architect",
                "input_spec": "需求分析结果",
                "output_spec": "技术方案 + 架构图描述",
                "prompt_template": "根据以下需求分析，给出技术方案和架构设计：\n\n{context}",
            },
            {
                "name": "代码实现",
                "description": "根据设计方案实现代码",
                "suggested_role": "coder",
                "input_spec": "技术方案",
                "output_spec": "可运行的代码",
                "prompt_template": "根据以下技术方案，实现代码：\n\n{context}",
            },
            {
                "name": "代码审查",
                "description": "审查代码安全性、性能和质量",
                "suggested_role": "reviewer",
                "input_spec": "代码实现",
                "output_spec": "审查报告 + 修改建议",
                "prompt_template": "审查以下代码，检查安全性、性能和代码质量：\n\n{context}",
            },
            {
                "name": "测试验证",
                "description": "编写测试用例并验证代码",
                "suggested_role": "tester",
                "input_spec": "代码 + 审查报告",
                "output_spec": "测试报告",
                "prompt_template": "为以下代码编写测试用例并执行：\n\n{context}",
            },
        ],
    },
    "research_pipeline": {
        "name": "技术调研流水线",
        "description": "调研 → 方案对比 → 架构建议",
        "steps": [
            {
                "name": "技术调研",
                "description": "针对主题进行技术调研，收集方案和对比数据",
                "suggested_role": "researcher",
                "input_spec": "调研主题",
                "output_spec": "调研报告 + 方案对比",
                "prompt_template": "针对以下主题进行技术调研：\n\n{input}",
            },
            {
                "name": "架构建议",
                "description": "根据调研结果给出架构选型建议",
                "suggested_role": "architect",
                "input_spec": "调研报告",
                "output_spec": "架构建议 + 实施方案",
                "prompt_template": "根据以下调研结果，给出架构建议：\n\n{context}",
            },
        ],
    },
    "full_dev_pipeline": {
        "name": "完整开发流水线",
        "description": "需求 → 设计 → 开发 → 审查 → 测试 → 部署",
        "steps": [
            {
                "name": "需求拆解",
                "description": "拆解需求为用户故事和任务列表",
                "suggested_role": "pm",
                "input_spec": "原始需求",
                "output_spec": "用户故事 + 任务列表",
            },
            {
                "name": "架构设计",
                "description": "设计技术架构",
                "suggested_role": "architect",
                "input_spec": "需求拆解结果",
                "output_spec": "架构设计文档",
            },
            {
                "name": "编码实现",
                "description": "实现代码",
                "suggested_role": "coder",
                "input_spec": "架构设计",
                "output_spec": "代码实现",
                "max_retries": 2,
            },
            {
                "name": "代码审查",
                "description": "审查代码质量和安全性",
                "suggested_role": "reviewer",
                "input_spec": "代码",
                "output_spec": "审查报告",
            },
            {
                "name": "编写测试",
                "description": "编写并执行测试",
                "suggested_role": "tester",
                "input_spec": "代码 + 审查报告",
                "output_spec": "测试报告",
            },
            {
                "name": "部署准备",
                "description": "准备部署方案",
                "suggested_role": "devops",
                "input_spec": "代码 + 测试报告",
                "output_spec": "Dockerfile + CI/CD 配置",
            },
        ],
    },
}


def list_workflow_templates() -> list[dict]:
    """列出所有可用的预置流水线模板。"""
    return [
        {
            "id": tid,
            "name": t["name"],
            "description": t["description"],
            "step_count": len(t["steps"]),
            "roles": [s.get("suggested_role", s.get("role", ""))
                      for s in t["steps"]],
        }
        for tid, t in WORKFLOW_TEMPLATES.items()
    ]


def get_workflow_template(template_id: str) -> dict | None:
    return WORKFLOW_TEMPLATES.get(template_id)
