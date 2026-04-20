"""
TudouClaw V2 — Task-as-first-class-citizen architecture.

See ``docs/PRD_AGENT_V2.md`` for the top-level design. This package is
deliberately isolated from V1 (``app.agent``, ``app.agent_llm``,
``app.agent_execution``, ``app.hub._core``, ``app.workflow``,
``app.persona``). Imports into those modules are blocked by the
pre-commit check at ``scripts/check_v2_isolation.py``.

V2 IS allowed to import these shared Layer-1 modules (PRD §13.1):
    app.llm                           – LLM providers
    app.skills.registry / .store      – skill metadata & grants
    app.mcp.manager                   – MCP manager
    app.auth.*                        – auth middleware
    app.runtime_paths / app (DEFAULT_DATA_DIR)

The package entry points live here:
    app.v2.core.task           – Task / Plan / Lesson / Artifact / TaskContext
    app.v2.core.task_events    – TaskEvent / TaskEventBus
    app.v2.core.task_store     – SQLite persistence (tudou.db, V2 tables)
    app.v2.core.task_loop      – TaskLoop (6-phase FSM driver)
    app.v2.core.task_executor  – TaskExecutor (Execute-phase tool loop)
    app.v2.agent.agent_v2      – AgentV2 (thin shell; declares capabilities)
    app.v2.bridges.*           – L1 adapters (llm / skill / mcp)
    app.v2.templates.loader    – YAML TaskTemplate loader
"""
__all__ = []
__version__ = "0.1.0-skeleton"
