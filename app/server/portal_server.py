"""
Portal HTTP server initialization and startup.

Security Features:
- Hub-Node Communication: Secrets transmitted via X-Claw-Secret header (not URL params)
  using constant-time comparison (hmac.compare_digest) to prevent timing attacks.
- HTTPS Enforcement: Optional TUDOU_FORCE_HTTPS env var enables HTTP->HTTPS redirects.
- Security Headers: All responses include X-Content-Type-Options, X-Frame-Options, X-XSS-Protection.
- HSTS: Strict-Transport-Security header sent when HTTPS is enforced.
- Authentication: Bearer token and session cookie validation in portal_auth.py.
- Secret Logging: Secrets never logged directly; only boolean indicators (has_secret=true/false).
"""
import json
import logging
import os

from ..defaults import (
    PORTAL_PORT, IP_DETECT_TARGET, IP_DETECT_PORT,
)
import socket
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import urlparse

from .. import llm
from ..auth import get_auth, init_auth
from ..hub import get_hub, init_hub
from ..scheduler import get_scheduler, init_scheduler
from ..mcp.manager import init_mcp_manager
from ..template_library import get_template_library, init_template_library
from ..llm import get_registry, init_registry
from ..channel import init_router

from .portal_routes_get import handle_get
from .portal_routes_post import handle_post, handle_delete

logger = logging.getLogger("tudou.portal")

# Portal mode: 'hub' or 'node'
_portal_mode: str = "hub"

def get_portal_mode() -> str:
    return _portal_mode

def is_hub_mode() -> bool:
    return _portal_mode == "hub"


class _PortalHandler(BaseHTTPRequestHandler):
    """Thin HTTP handler that delegates to route modules."""

    def log_message(self, format, *args):
        pass

    def handle(self):
        """Override to suppress ConnectionResetError noise from aborted requests."""
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            # Client disconnected (e.g. abort button, page refresh) — ignore
            pass

    def _add_security_headers(self):
        """Add standard security headers to all responses."""
        # Prevent MIME-type sniffing attacks
        self.send_header("X-Content-Type-Options", "nosniff")
        # Prevent clickjacking attacks
        self.send_header("X-Frame-Options", "DENY")
        # Enable browser XSS protection
        self.send_header("X-XSS-Protection", "1; mode=block")
        # Enforce HTTPS if configured
        force_https = os.environ.get("TUDOU_FORCE_HTTPS", "false").lower() == "true"
        if force_https:
            self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

    # ---- Response helpers (kept on handler for direct use by route modules) ----
    #
    # 所有 write 都包了 BrokenPipe/ConnectionReset 兜底：客户端提前关连接
    # （刷新、导航、移动端切后台）是常见现象，不是服务端 bug，没必要把
    # 整个 unhandled exception trace 打到日志里。
    def _safe_write(self, body: bytes) -> None:
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    def _json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self._add_security_headers()
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        self._safe_write(body)

    def _html(self, content: str):
        body = content.encode("utf-8")
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self._add_security_headers()
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            return
        self._safe_write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    def _sse_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self._add_security_headers()
        self.end_headers()

    def _sse_send(self, data: dict):
        line = f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        try:
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    # ---- Route dispatch ----
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-Claw-Secret, X-Hub-Secret")
        self._add_security_headers()
        self.end_headers()

    def _check_https_redirect(self) -> bool:
        """Redirect HTTP to HTTPS if TUDOU_FORCE_HTTPS is enabled.
        Returns True if redirect was sent, False to continue processing."""
        force_https = os.environ.get("TUDOU_FORCE_HTTPS", "false").lower() == "true"
        if not force_https:
            return False

        # Only redirect if not already HTTPS
        if self.headers.get("X-Forwarded-Proto", "http").lower() == "https":
            return False

        host = self.headers.get("Host", "localhost")
        path = self.path
        https_url = f"https://{host}{path}"

        self.send_response(301)
        self.send_header("Location", https_url)
        self.send_header("Content-Length", "0")
        self.end_headers()
        logger.debug("HTTPS redirect: %s -> %s", self.path, https_url)
        return True

    def do_GET(self):
        if self._check_https_redirect():
            return
        handle_get(self)

    def do_POST(self):
        if self._check_https_redirect():
            return
        handle_post(self)

    def do_DELETE(self):
        if self._check_https_redirect():
            return
        handle_delete(self)


def run_portal(port: int = PORTAL_PORT, node_name: str = "",
               secret: str = "", admin_token: str = "",
               mode: str = "hub", data_dir: str = ""):
    """Start the Portal server.
    mode: 'hub' = main admin portal, 'node' = remote node portal (limited admin).
    data_dir: Runtime data directory. Follows priority: CLI arg > env var > default.
    """
    global _portal_mode
    _portal_mode = mode  # 'hub' or 'node'

    # Initialize auth system
    # Runtime data directory: CLI arg > env var > default
    if not data_dir:
        data_dir = os.environ.get("TUDOU_DATA_DIR", "")
    if not data_dir:
        from .. import DEFAULT_DATA_DIR
        data_dir = DEFAULT_DATA_DIR
    os.makedirs(data_dir, exist_ok=True)

    # Set env var so child components (Agent._get_agent_home) can find it
    os.environ["TUDOU_CLAW_DATA_DIR"] = data_dir

    # ---- Auto-migrate data from legacy locations ----
    # Priority: old hyphenated dir (~/.tudou-claw) > old app/ source dir
    _config_files = [
        "providers.json", "agents.json", "mcp_configs.json",
        "channels.json", "scheduled_jobs.json", "execution_history.json",
        ".tudou_tokens.json", ".claw_tokens.json",
    ]

    def _migrate_from(src_dir: str, label: str):
        """Copy missing config files from src_dir into data_dir."""
        if not os.path.isdir(src_dir):
            return
        migrated = []
        for fname in _config_files:
            src = os.path.join(src_dir, fname)
            dst = os.path.join(data_dir, fname)
            if os.path.isfile(src) and not os.path.isfile(dst):
                try:
                    import shutil
                    shutil.copy2(src, dst)
                    migrated.append(fname)
                except OSError:
                    pass
        # Migrate workspaces directory
        src_ws = os.path.join(src_dir, "workspaces")
        dst_ws = os.path.join(data_dir, "workspaces")
        if os.path.isdir(src_ws):
            for entry in os.listdir(src_ws):
                src_agent = os.path.join(src_ws, entry)
                dst_agent = os.path.join(dst_ws, entry)
                if os.path.isdir(src_agent) and not os.path.exists(dst_agent):
                    try:
                        import shutil
                        shutil.copytree(src_agent, dst_agent)
                        migrated.append(f"workspaces/{entry}")
                    except OSError:
                        pass
        if migrated:
            sys.stderr.write(
                f"[portal] Migrated {len(migrated)} items from {label} "
                f"({src_dir}): {', '.join(migrated)}\n")

    # 1. Check old hyphenated directory: ~/.tudou-claw
    old_hyphen_dir = os.path.join(os.path.expanduser("~"), ".tudou-claw")
    _migrate_from(old_hyphen_dir, "legacy ~/.tudou-claw")

    # 2. Check app/ source directory (early versions stored data here)
    app_src_dir = os.path.dirname(os.path.abspath(__file__))
    _migrate_from(app_src_dir, "app source dir")

    # ---- Create standard directory layout ----
    for subdir in ("config", "data", "auth", "workspaces", "projects", "logs"):
        os.makedirs(os.path.join(data_dir, subdir), exist_ok=True)
    auth, raw_admin_token = init_auth(
        data_dir=data_dir,
        admin_token=admin_token,
        shared_secret=secret,
    )

    # Initialize provider registry
    registry = init_registry(data_dir=data_dir)

    # Initialize hub
    hub = init_hub(node_name=node_name, data_dir=data_dir)

    # Initialize channel router
    channel_router = init_router(data_dir=data_dir)

    # Initialize scheduler
    scheduler = init_scheduler(data_dir=data_dir)
    scheduler.set_hub(hub)
    scheduler.set_channel_router(channel_router)
    try:
        from .template_library import get_template_library
        scheduler.set_template_library(get_template_library())
    except Exception:
        pass
    scheduler.start()

    # Initialize skill registry (auto-discover SKILL.md files)
    try:
        from ..core.prompt_enhancer import init_prompt_pack_registry
        init_prompt_pack_registry(data_dir=data_dir)
    except Exception as e:
        logger.warning("Skill registry init failed: %s", e)

    # Initialize RolePresetV2 registry
    try:
        from ..role_preset_registry import init_registry as _init_rp_registry
        from ..agent import ROLE_PRESETS as _RP
        _rp_reg = _init_rp_registry()
        _rp_reg.merge_into_legacy(_RP)
        logger.info("RolePresetV2 registry initialized")
    except Exception as _rp_err:
        logger.warning("RolePresetV2 registry init failed: %s", _rp_err)

    # Initialize LLM Tier Router
    try:
        from ..llm_tier_routing import init_router as _init_tier_router
        _init_tier_router(data_dir=data_dir, autofill=True)
        logger.info("LLMTierRouter initialized")
    except Exception as _tr_err:
        logger.warning("LLMTierRouter init failed: %s", _tr_err)

    # Initialize Role SOP Registry
    try:
        from ..role_sop import init_sop as _init_sop
        _init_sop()
        logger.info("RoleSOP registry initialized")
    except Exception as _sop_err:
        logger.warning("RoleSOP init failed: %s", _sop_err)

    # Initialize MCP manager
    mcp_mgr = init_mcp_manager(data_dir=data_dir)

    # ── MCP Call Router ───────────────────────────────────────────
    # Single entry point for every MCP invocation from every caller
    # in this process. Agents, tools.py, the portal "test" button,
    # and any future diagnostic path all reach the dispatcher via
    # this router — authorization, location and error classification
    # happen in exactly one place.
    try:
        from ..mcp.router import MCPCallRouter
        hub.mcp_router = MCPCallRouter(hub=hub)
        logger.info("MCP call router initialized")
    except Exception as _re:
        logger.warning("MCP router init failed: %s", _re)
        hub.mcp_router = None

    # Sync MCP bindings into all loaded agents (fixes stale/empty MCP lists after restart)
    hub.sync_all_agent_mcps()

    def _agent_chat_for_channel(agent_id: str, message: str) -> str:
        """Channel router chat — runs agent.chat() with timeout isolation.

        Each agent's LLM failure is contained; it won't block the channel
        router or affect other agents.
        """
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

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=200)  # Max wait: slightly longer than LLM read timeout
        if error_holder[0]:
            return f"Error: {error_holder[0]}"
        if result_holder[0] is None:
            return f"Error: Agent {agent.name} LLM call timed out"
        return result_holder[0]

    channel_router.set_agent_chat_fn(_agent_chat_for_channel)

    # Resolve network info
    hostname = socket.gethostname()
    local_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect((IP_DETECT_TARGET, IP_DETECT_PORT))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    cfg = llm.get_config()
    force_https = os.environ.get("TUDOU_FORCE_HTTPS", "false").lower() == "true"
    protocol = "https" if force_https else "http"

    print()
    print(f"  \033[1m\033[34m\U0001f954 Tudou Claws Portal\033[0m")
    print(f"  \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500")
    print(f"  Local:    {protocol}://localhost:{port}")
    print(f"  Network:  {protocol}://{local_ip}:{port}")
    print(f"  Node:     {node_name or hostname}")
    print(f"  Data Dir: {data_dir}")
    print(f"  Provider: {cfg['provider']}  Model: {cfg['model']}")
    if force_https:
        print(f"  Security: HTTPS enforced (HTTP -> HTTPS redirect)")
    else:
        print(f"  Security: Standard HTTP (set TUDOU_FORCE_HTTPS=true for HTTPS redirect)")
    if raw_admin_token:
        print()
        print(f"  \033[1m\033[33m\u26a0  Admin Token (save this!):\033[0m")
        print(f"  \033[1m{raw_admin_token}\033[0m")
        # Also save to file so user can always find it
        token_file = os.path.join(data_dir, ".admin_token")
        try:
            with open(token_file, "w") as f:
                f.write(raw_admin_token)
            os.chmod(token_file, 0o600)
            print(f"  \033[2mToken saved to: {token_file}\033[0m")
        except OSError:
            pass
    if secret:
        print(f"  Secret:   {'*' * min(len(secret), 8)}...")
    print()
    print(f"  \033[2mRemote agents can register:\033[0m")
    print(f"  \033[2mpython -m app agent --hub {protocol}://{local_ip}:{port} --secret <secret>\033[0m")
    print()
    print(f"  Press Ctrl+C to stop")
    print()

    # Configure logging
    logging.basicConfig(
        level=logging.DEBUG,
        format="[%(asctime)s] %(levelname)-5s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logger.info("Portal starting: port=%d node=%s ip=%s secret=%s force_https=%s",
                port, node_name or hostname, local_ip, bool(secret), force_https)

    class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = _ThreadedHTTPServer(("0.0.0.0", port), _PortalHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down portal...")
        scheduler.stop()
        server.shutdown()
