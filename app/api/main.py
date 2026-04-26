"""
TudouClaw FastAPI Application
============================
Phase-1 backend: replaces BaseHTTPRequestHandler with FastAPI.
Maintains full backward compatibility with existing hub/agent core.

Usage:
    python -m app.api.main              # dev mode, port 9090
    python -m app.api.main --port 8000  # custom port
    uvicorn app.api.main:app --reload   # hot-reload dev
"""
from __future__ import annotations

import os
import socket

from ..defaults import (
    PORTAL_PORT, BIND_ADDRESS, CORS_ORIGINS_DEFAULT,
    IP_DETECT_TARGET, IP_DETECT_PORT,
)
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .middleware.security import SecurityHeadersMiddleware
from .deps.hub import init_hub, shutdown_hub

logger = logging.getLogger("tudouclaw.api")


# ---------------------------------------------------------------------------
# uvicorn access-log filter — drop ALL 2xx responses
# ---------------------------------------------------------------------------
# User requested: "200状态就不用报了，只报异常的就行". Successful requests
# don't need a log line; only 3xx/4xx/5xx are worth seeing.
#
# Escape hatches:
#   - TUDOU_LOG_ACCESS=1          keep all access lines (debug mode)
#   - TUDOU_LOG_ACCESS_KEEP=a,b   comma-sep path substrings to keep even on 2xx

# Compiled regex matches the uvicorn access format:
#   'host:port - "METHOD /path HTTP/1.1" STATUS'
_ACCESS_RE = re.compile(
    r'"(?P<method>[A-Z]+)\s+(?P<path>\S+)\s+HTTP/\d\.\d"\s+(?P<status>\d{3})'
)


class _AccessNoiseFilter(logging.Filter):
    """Drop uvicorn.access lines for ALL 2xx responses.

    User requirement: "200状态就不用报了，只报异常的就行" —
    successful requests don't need log lines; only anomalies (3xx redirects,
    4xx client errors, 5xx server errors) are worth seeing.

    Env controls:
      TUDOU_LOG_ACCESS=1          keep all access lines (debug mode)
      TUDOU_LOG_ACCESS_KEEP=a,b,c comma-sep substrings; any access line whose
                                  path contains one of these is ALWAYS kept
                                  even on 2xx (for investigating a specific
                                  endpoint)
    """

    def __init__(self):
        super().__init__()
        import os as _os
        self._verbose = _os.environ.get("TUDOU_LOG_ACCESS", "0") == "1"
        keep = _os.environ.get("TUDOU_LOG_ACCESS_KEEP", "") or ""
        self._keep_substrings = tuple(
            s.strip() for s in keep.split(",") if s.strip())

    def filter(self, record: logging.LogRecord) -> bool:
        if self._verbose:
            return True
        try:
            msg = record.getMessage()
        except Exception:
            return True
        m = _ACCESS_RE.search(msg)
        if not m:
            return True
        path = m.group("path")
        status = int(m.group("status"))
        # Keep ANY non-2xx — errors, redirects, rate-limit, etc. 都是信号。
        if status < 200 or status >= 300:
            return True
        # 2xx — drop by default. Escape hatch: user-configured substrings.
        for sub in self._keep_substrings:
            if sub and sub in path:
                return True
        return False  # drop all other 2xx


# Install filter as early as possible — uvicorn.access logger is created
# when uvicorn boots. Attaching here (module-level, before uvicorn.run) is
# safe: the logger gets the filter whether we're invoked via `python -m app`
# or a bare `uvicorn app.api.main:app`.
logging.getLogger("uvicorn.access").addFilter(_AccessNoiseFilter())

# ---------------------------------------------------------------------------
# Lifespan: startup / shutdown
# ---------------------------------------------------------------------------

def _print_banner(hub, raw_admin_token: str = ""):
    """Print startup banner with admin token and JWT, matching old portal style."""
    hostname = socket.gethostname()
    local_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((IP_DETECT_TARGET, IP_DETECT_PORT))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    port = int(os.environ.get("TUDOU_PORT", str(PORTAL_PORT)))
    protocol = "https" if os.environ.get("TUDOU_FORCE_HTTPS", "false").lower() == "true" else "http"

    print()
    print(f"  \033[1m\033[34m\U0001f954 TudouClaw API (FastAPI)\033[0m")
    print(f"  \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
    print(f"  Local:    {protocol}://localhost:{port}")
    print(f"  Network:  {protocol}://{local_ip}:{port}")
    print(f"  API Docs: {protocol}://localhost:{port}/api/docs")
    print(f"  Node:     {hostname}")

    if raw_admin_token:
        # Generate a JWT access token for direct API use
        try:
            from .deps.auth import create_access_token
            jwt_token = create_access_token(
                user_id="admin",
                role="superAdmin",
                extra={"token_login": True},
            )
        except Exception:
            jwt_token = ""

        print()
        print(f"  \033[1m\033[33m\u26a0  Admin Token (for login page):\033[0m")
        print(f"  \033[1m{raw_admin_token}\033[0m")
        if jwt_token:
            print()
            print(f"  \033[1m\033[33m\u26a0  JWT Bearer Token (for API calls):\033[0m")
            print(f"  \033[1m{jwt_token}\033[0m")
    else:
        print()
        print(f"  \033[33m\u26a0  No admin token found. Set TUDOU_ADMIN_SECRET env.\033[0m")

    print()
    print(f"  Press Ctrl+C to stop")
    print()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize Hub on startup, clean up on shutdown."""
    logger.info("TudouClaw FastAPI starting up ...")

    # Initialize auth system (must happen before Hub if auth depends on data_dir)
    data_dir = os.environ.get("TUDOU_DATA_DIR", "")
    if not data_dir:
        from .. import DEFAULT_DATA_DIR
        data_dir = os.environ.get("TUDOU_CLAW_DATA_DIR", DEFAULT_DATA_DIR)
    os.makedirs(data_dir, exist_ok=True)
    os.environ.setdefault("TUDOU_CLAW_DATA_DIR", data_dir)

    shared_secret = os.environ.get("TUDOU_ADMIN_SECRET", "")
    explicit_token = os.environ.get("TUDOU_ADMIN_TOKEN", "")

    # Reuse persisted token from previous run so it survives restarts.
    # Only generate a fresh one when no persisted token exists.
    token_file = os.path.join(data_dir, ".admin_token")
    if not explicit_token:
        try:
            if os.path.isfile(token_file):
                with open(token_file, "r") as f:
                    explicit_token = f.read().strip()
        except OSError:
            pass

    try:
        from ..auth import init_auth
        auth_mgr, raw_token = init_auth(
            data_dir=data_dir,
            admin_token=explicit_token,       # persisted or auto-generate
            shared_secret=shared_secret,      # inter-node shared secret
        )
        app.state.raw_admin_token = raw_token

        # Persist token to file so it survives restarts
        try:
            with open(token_file, "w") as f:
                f.write(raw_token)
            os.chmod(token_file, 0o600)
        except OSError:
            pass
    except Exception as e:
        logger.warning("Auth init failed: %s", e)
        app.state.raw_admin_token = ""

    hub = init_hub()

    # ── MCP Manager + Router ─────────────────────────────────────────
    # The Hub constructor does NOT set up the MCP call pipeline. We must
    # initialise the MCP manager (loads global_mcps.json / mcp_configs.json),
    # create the MCPCallRouter (single entry point for every MCP call),
    # and sync MCP bindings into all loaded agents.
    try:
        from ..mcp.manager import init_mcp_manager
        init_mcp_manager(data_dir=data_dir)
        logger.info("MCP manager initialized")
    except Exception as _me:
        logger.warning("MCP manager init failed: %s", _me)

    try:
        from ..mcp.router import MCPCallRouter
        hub.mcp_router = MCPCallRouter(hub=hub)
        logger.info("MCP call router initialized")
    except Exception as _re:
        logger.warning("MCP router init failed: %s", _re)
        hub.mcp_router = None

    # Sync MCP bindings into all loaded agents so agent.profile.mcp_servers
    # reflects the authoritative binding table from the MCP manager.
    try:
        hub.sync_all_agent_mcps()
        logger.info("MCP bindings synced to all agents")
    except Exception as _se:
        logger.warning("MCP sync failed: %s", _se)

    # ── Skill Registry (PromptPack / BM25 matching) ─────────────
    try:
        from ..core.prompt_enhancer import init_prompt_pack_registry
        _skill_scan_dirs = []
        for _rel in ("data/skills_installed", "data/skill_catalog/imported"):
            _abs = os.path.join(os.getcwd(), _rel)
            if os.path.isdir(_abs):
                _skill_scan_dirs.append(_abs)
        init_prompt_pack_registry(data_dir=data_dir, extra_scan_dirs=_skill_scan_dirs)
    except Exception as _sr:
        logger.warning("Skill registry init failed: %s", _sr)

    # ── RolePresetV2 Registry (7-dimensional role specs) ────────
    try:
        from ..role_preset_registry import init_registry as _init_rp_registry
        from ..agent import ROLE_PRESETS as _RP
        _rp_reg = _init_rp_registry()
        _merged = _rp_reg.merge_into_legacy(_RP)
        logger.info("RolePresetV2 loaded: %d V2 presets merged into ROLE_PRESETS", _merged)
        # ── Push role-level command_patterns into ToolPolicy ──
        # Each preset's `command_patterns` becomes a scope="role:<id>"
        # entry so the rule chain applies them to agents of that role.
        # Runs after both auth and registry are initialized.
        try:
            from ..auth import get_auth as _get_auth
            _tp = _get_auth().tool_policy
            _cp_n = _rp_reg.register_command_patterns_to_policy(_tp)
            logger.info("Role command_patterns registered: %d entries", _cp_n)
        except Exception as _cp_err:
            logger.warning("Role command_patterns registration failed: %s",
                           _cp_err)
    except Exception as _rp_err:
        logger.warning("RolePresetV2 registry init failed: %s", _rp_err)

    # ── LLM Tier Router (capability tier → provider/model) ─────
    try:
        from ..llm_tier_routing import init_router as _init_tier_router
        _tier_router = _init_tier_router(data_dir=data_dir, autofill=True)
        logger.info("LLMTierRouter initialized: %d tier mappings",
                    len(_tier_router.all()))
    except Exception as _tr_err:
        logger.warning("LLMTierRouter init failed: %s", _tr_err)

    # ── Role SOP Registry (stage-machine workflows for roles) ───
    try:
        from ..role_sop import init_sop as _init_sop
        _sop_reg, _ = _init_sop()
        logger.info("RoleSOP loaded: %d SOP templates", len(_sop_reg.all()))
    except Exception as _sop_err:
        logger.warning("RoleSOP init failed: %s", _sop_err)

    # ── SkillForge ─────────────────────────────────────────────────
    try:
        from ..skills._skill_forge import get_skill_forge
        get_skill_forge(data_dir=data_dir)
        logger.info("SkillForge initialized")
    except Exception as _sf:
        logger.warning("SkillForge init failed: %s", _sf)

    # ── Channel Router — bind agent chat function + start pollers ──
    try:
        from ..channel import get_router as _get_ch_router
        _ch_router = _get_ch_router()

        def _agent_chat_for_channel(agent_id: str, message: str) -> str:
            import threading as _th
            agent = hub.get_agent(agent_id)
            if not agent:
                return f"Agent {agent_id} not found"
            result_holder = [None]
            error_holder = [None]
            def _run():
                try:
                    result_holder[0] = agent.chat(message) or ""
                except Exception as e:
                    error_holder[0] = str(e)
            t = _th.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=200)
            if error_holder[0]:
                return f"Error: {error_holder[0]}"
            if result_holder[0] is None:
                return "Error: Agent LLM call timed out"
            return result_holder[0]

        _ch_router.set_agent_chat_fn(_agent_chat_for_channel)
        logger.info("Channel router bound to agent chat (%d channels)",
                     len(_ch_router.list_channels()))
    except Exception as _che:
        logger.warning("Channel router init failed: %s", _che)

    # ── Scheduler ──────────────────────────────────────────────────
    scheduler = None
    try:
        from ..scheduler import init_scheduler
        scheduler = init_scheduler(data_dir=data_dir)
        scheduler.set_hub(hub)
        # Channel router (optional — for channel-based notifications)
        try:
            from ..channel import get_router as _get_ch_router2
            scheduler.set_channel_router(_get_ch_router2())
        except Exception:
            pass
        # Template library (optional — for preset jobs)
        try:
            from ..server.template_library import get_template_library
            scheduler.set_template_library(get_template_library())
        except Exception:
            pass
        scheduler.start()
        logger.info("Scheduler initialized and started")
    except Exception as _sche:
        logger.warning("Scheduler init failed: %s", _sche)

    # V2 crash recovery: any task still marked RUNNING in the DB is
    # orphaned (previous process's daemon thread is dead). Restart them.
    try:
        from app.v2.core.task_store import get_store as _get_v2_store
        from app.v2.core.task_events import TaskEventBus as _V2Bus
        from app.v2.core import task_controller as _v2_ctrl
        _store = _get_v2_store()
        _bus = _V2Bus(_store)
        restarted = _v2_ctrl.recover_orphaned_tasks(_store, _bus)
        if restarted:
            logger.info("V2 crash recovery: restarted %d task(s): %s",
                        len(restarted), restarted)
    except Exception as _v2e:
        logger.warning("V2 crash recovery failed: %s", _v2e)

    _print_banner(hub, app.state.raw_admin_token)
    yield
    logger.info("TudouClaw FastAPI shutting down ...")
    # Stop scheduler before hub shutdown
    if scheduler is not None:
        try:
            scheduler.stop()
        except Exception:
            pass
    shutdown_hub()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------

def create_app() -> FastAPI:
    app = FastAPI(
        title="TudouClaw API",
        description="Multi-Agent AI Coordination Hub",
        version="2.0.0",
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
        lifespan=lifespan,
    )

    # ── CORS ──────────────────────────────────────────────────────────
    # In dev, Vue dev server runs on a different port (e.g. 5173)
    cors_origins = os.environ.get(
        "TUDOU_CORS_ORIGINS",
        CORS_ORIGINS_DEFAULT,
    ).split(",")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Security headers ──────────────────────────────────────────────
    app.add_middleware(SecurityHeadersMiddleware)

    # ── Register routers ──────────────────────────────────────────────
    from .routers import (
        auth,
        agents,
        chat,
        projects,
        workflows,
        skills,
        mcp,
        providers,
        config,
        nodes,
        scheduler,
        experience,
        meetings,
        admin,
        knowledge,
        hub_sync,
        health,
        channels,
        personas,
        i18n,
        attachment,
        audio,
        pages,
        llm_tiers,
        role_presets_v2,
        progress,
        inbox as inbox_router,
        checkpoints as checkpoints_router,
        memory_refs as memory_refs_router,
        v2 as v2_router,
    )

    # ── API routers ──────────────────────────────────────────────────
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(agents.router)
    app.include_router(chat.router)
    app.include_router(projects.router)
    app.include_router(workflows.router)
    app.include_router(skills.router)
    app.include_router(mcp.router)
    app.include_router(providers.router)
    app.include_router(config.router)
    app.include_router(nodes.router)
    app.include_router(scheduler.router)
    app.include_router(experience.router)
    app.include_router(meetings.router)
    app.include_router(admin.router)
    app.include_router(llm_tiers.router)
    app.include_router(role_presets_v2.router)
    app.include_router(knowledge.router)
    app.include_router(hub_sync.router)
    app.include_router(channels.router)
    app.include_router(personas.router)
    app.include_router(i18n.router)
    app.include_router(attachment.router)
    app.include_router(audio.router)
    app.include_router(progress.router)
    app.include_router(inbox_router.router)
    app.include_router(checkpoints_router.router)
    app.include_router(memory_refs_router.router)
    app.include_router(v2_router.router)

    # ── Static files (JS/CSS used by portal templates) ───────────────
    server_static = os.path.join(os.path.dirname(__file__), "..", "server", "static")
    app_static = os.path.join(os.path.dirname(__file__), "..", "static")
    if os.path.isdir(server_static):
        app.mount("/static/js", StaticFiles(directory=os.path.join(server_static, "js")), name="legacy-js")
    if os.path.isdir(app_static):
        app.mount("/static", StaticFiles(directory=app_static), name="legacy-static")

    # Project shared workspaces — what the Deliverables UI links to
    # (/workspace/shared/<project_id>/<file>). Deliberately only expose
    # `workspaces/shared/`, NOT the full `workspaces/` tree, so each agent's
    # private workspace stays unreachable from the browser.
    try:
        from .. import DEFAULT_DATA_DIR as _DDD
        _data_dir = os.environ.get("TUDOU_CLAW_DATA_DIR") or _DDD
        _shared_root = os.path.join(_data_dir, "workspaces", "shared")
        os.makedirs(_shared_root, exist_ok=True)
        app.mount("/workspace/shared",
                  StaticFiles(directory=_shared_root),
                  name="workspace-shared")
    except Exception as _e:
        import logging as _lg
        _lg.getLogger(__name__).warning(
            "failed to mount /workspace/shared: %s", _e)

    # ── Page routes (must be registered AFTER static mounts) ─────────
    app.include_router(pages.router)

    return app


app = create_app()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import uvicorn

    parser = argparse.ArgumentParser(description="TudouClaw API Server")
    parser.add_argument("--host", default=BIND_ADDRESS, help="Bind address")
    parser.add_argument("--port", type=int, default=PORTAL_PORT, help="Port")
    parser.add_argument("--reload", action="store_true", help="Hot reload (dev)")
    args = parser.parse_args()

    uvicorn.run(
        "app.api.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
