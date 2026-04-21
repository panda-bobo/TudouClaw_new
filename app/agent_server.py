"""
Standalone Agent HTTP server — runs a single agent as an independent process.

Start on any machine, optionally auto-register with a Portal hub.
Other portals can connect to and manage this agent remotely.

Usage:
    python -m app agent --name Coder --role coder --port 8081
    python -m app agent --port 8081 --hub http://portal-host:9090 --secret mykey
"""
import json
import logging
import os
import socket
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

import requests as http_requests

from . import llm, tools
from .agent import Agent, AgentEvent, AgentStatus, create_agent
from .auth import get_auth, init_auth
from .defaults import IP_DETECT_TARGET, IP_DETECT_PORT, AGENT_PORT, PORTAL_PORT

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent Server
# ---------------------------------------------------------------------------

class AgentServer:
    """Manages a single standalone agent with HTTP API."""

    def __init__(self, agent: Agent, port: int = 8081,
                 hub_url: str = "", secret: str = ""):
        self.agent = agent
        self.port = port
        self.hub_url = hub_url.rstrip("/") if hub_url else ""
        self.secret = secret
        self._heartbeat_thread: threading.Thread | None = None
        self._running = False
        self.node_id = f"agent-{agent.id}"
        from . import DEFAULT_DATA_DIR
        self._data_dir = DEFAULT_DATA_DIR
        self._save_lock = threading.Lock()

        # Init auth only if not already initialized
        import app.auth as _auth_mod
        if _auth_mod._auth is None:
            init_auth(shared_secret=secret)
        elif secret:
            # Ensure shared secret is set even if auth was already initialized
            _auth_mod._auth._shared_secret = secret

    # ---- Per-agent workspace persistence ----

    def _workspace_dir(self) -> str:
        """Return per-agent workspace directory: ~/.tudou_claw/workspaces/{agent_id}/"""
        return os.path.join(self._data_dir, "workspaces", self.agent.id)

    def _agent_file(self) -> str:
        return os.path.join(self._workspace_dir(), "agent.json")

    def _node_file(self) -> str:
        return os.path.join(self._data_dir, "node.json")

    def save_agent(self):
        """Persist agent state to its workspace directory."""
        with self._save_lock:
            ws = self._workspace_dir()
            os.makedirs(ws, exist_ok=True)
            data = self.agent.to_persist_dict()
            try:
                tmp = self._agent_file() + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self._agent_file())
            except Exception as e:
                sys.stderr.write(f"[agent] Failed to save agent: {e}\n")

    def save_node_info(self):
        """Persist node info (id, name, url, agent list) to disk."""
        os.makedirs(self._data_dir, exist_ok=True)
        info = {
            "node_id": self.node_id,
            "name": f"{self.agent.name}@{socket.gethostname()}",
            "port": self.port,
            "hub_url": self.hub_url,
            "secret": self.secret,
            "agent_id": self.agent.id,
        }
        try:
            nf = self._node_file()
            tmp = nf + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(info, f, ensure_ascii=False, indent=2)
            os.replace(tmp, nf)
        except Exception as e:
            sys.stderr.write(f"[agent] Failed to save node info: {e}\n")

    @staticmethod
    def load_agent_from_workspace(agent_id: str, data_dir: str = "") -> Agent | None:
        """Try to restore an agent from its workspace directory."""
        from . import DEFAULT_DATA_DIR
        dd = data_dir or DEFAULT_DATA_DIR
        af = os.path.join(dd, "workspaces", agent_id, "agent.json")
        if not os.path.exists(af):
            return None
        try:
            with open(af, "r", encoding="utf-8") as f:
                data = json.load(f)
            agent = Agent.from_persist_dict(data)
            sys.stderr.write(f"[agent] Restored agent '{agent.name}' from workspace\n")
            return agent
        except Exception as e:
            sys.stderr.write(f"[agent] Failed to load agent from workspace: {e}\n")
            return None

    @staticmethod
    def list_saved_agents(data_dir: str = "") -> list[dict]:
        """List all agents saved in workspace directories."""
        from . import DEFAULT_DATA_DIR
        dd = data_dir or DEFAULT_DATA_DIR
        ws_root = os.path.join(dd, "workspaces")
        if not os.path.isdir(ws_root):
            return []
        result = []
        for name in os.listdir(ws_root):
            af = os.path.join(ws_root, name, "agent.json")
            if os.path.isfile(af):
                try:
                    with open(af, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    result.append({
                        "id": data.get("id", name),
                        "name": data.get("name", "Unknown"),
                        "role": data.get("role", "general"),
                        "model": data.get("model", ""),
                    })
                except Exception:
                    pass
        return result

    def _auto_save_async(self):
        """Save agent in background thread to avoid blocking chat."""
        threading.Thread(target=self.save_agent, daemon=True).start()

    def _get_self_url(self) -> str:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect((IP_DETECT_TARGET, IP_DETECT_PORT))
            ip = s.getsockname()[0]
            s.close()
        except Exception:
            ip = "127.0.0.1"
        return f"http://{ip}:{self.port}"

    # ---- Hub registration ----

    def register_with_hub(self) -> bool:
        if not self.hub_url:
            return False
        payload = {
            "node_id": self.node_id,
            "name": f"{self.agent.name}@{socket.gethostname()}",
            "url": self._get_self_url(),
            "secret": self.secret,
            "agents": [self.agent.to_dict()],
        }
        try:
            resp = http_requests.post(
                f"{self.hub_url}/api/hub/register",
                json=payload, timeout=10,
            )
            resp.raise_for_status()
            return True
        except Exception as e:
            sys.stderr.write(f"[agent] Failed to register with hub: {e}\n")
            return False

    def _heartbeat_loop(self):
        while self._running:
            time.sleep(30)
            if not self._running:
                break
            try:
                payload = {
                    "node_id": self.node_id,
                    "agents": [self.agent.to_dict()],
                }
                http_requests.post(
                    f"{self.hub_url}/api/hub/sync",
                    json=payload, timeout=10,
                )
            except Exception:
                pass

    # ---- Start/Stop ----

    def start(self):
        self._running = True

        # Register with hub
        if self.hub_url:
            ok = self.register_with_hub()
            if ok:
                logger.info("Registered with hub: %s", self.hub_url)
                print(f"  Registered with hub: {self.hub_url}")
                self._heartbeat_thread = threading.Thread(
                    target=self._heartbeat_loop, daemon=True)
                self._heartbeat_thread.start()
            else:
                logger.warning("Failed to register with hub: %s", self.hub_url)
                print(f"  WARNING: Failed to register with hub")

        # Create handler class bound to this server
        server_ref = self

        class Handler(_AgentHandler):
            agent_server = server_ref

        httpd = HTTPServer(("0.0.0.0", self.port), Handler)
        self_url = self._get_self_url()
        cfg = llm.get_config()

        print()
        print(f"  \033[1m\033[32m🤖 Tudou Agent: {self.agent.name}\033[0m")
        print(f"  ─────────────────────────────────────")
        print(f"  URL:      {self_url}")
        print(f"  Role:     {self.agent.role}")
        print(f"  Model:    {self.agent.model or cfg['model']}")
        print(f"  Provider: {self.agent.provider or cfg['provider']}")
        if self.hub_url:
            print(f"  Hub:      {self.hub_url}")
        print(f"  Press Ctrl+C to stop")
        print()

        # Initial save: persist agent + node info to workspace
        self.save_agent()
        self.save_node_info()
        print(f"  Workspace: {self._workspace_dir()}")

        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            self._running = False
            print("\nSaving agent state...")
            self.save_agent()
            self.save_node_info()
            print("Shutting down agent...")
            httpd.shutdown()


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class _AgentHandler(BaseHTTPRequestHandler):
    agent_server: AgentServer = None  # Set by subclass

    def log_message(self, fmt, *args):
        sys.stderr.write(f"[agent] {fmt % args}\n")

    def _json(self, data: dict, status: int = 200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

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
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def _sse_send(self, data: dict):
        line = f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
        try:
            self.wfile.write(line.encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _check_auth(self) -> bool:
        """Verify Bearer token or shared secret."""
        auth = get_auth()
        # Check Bearer token
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth.validate_token(auth_header[7:].strip())
            if token:
                return True
        # Check shared secret in header
        secret = self.headers.get("X-Claw-Secret", "")
        if secret and auth.verify_secret(secret):
            return True
        # If no auth configured, allow (standalone mode)
        if not auth.tokens and not auth._shared_secret:
            return True
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers",
                         "Content-Type, Authorization, X-Claw-Secret")
        self.end_headers()

    def do_GET(self):
        path = urlparse(self.path).path
        srv = self.agent_server

        if path == "/api/health":
            self._json({
                "status": "ok",
                "agent": srv.agent.to_dict(),
            })

        elif path == "/api/agent/info":
            if not self._check_auth():
                self._json({"error": "Unauthorized"}, 401)
                return
            self._json(srv.agent.to_dict())

        elif path == "/api/agent/events":
            if not self._check_auth():
                self._json({"error": "Unauthorized"}, 401)
                return
            self._json({
                "events": [e.to_dict() for e in srv.agent.events[-300:]]
            })

        elif path == "/api/agent/approvals":
            if not self._check_auth():
                self._json({"error": "Unauthorized"}, 401)
                return
            auth = get_auth()
            self._json({
                "pending": auth.tool_policy.list_pending(),
                "history": auth.tool_policy.list_history(50),
            })

        # For hub compatibility: list agents on this node
        elif path == "/api/hub/agents":
            self._json({"agents": [srv.agent.to_dict()]})

        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        srv = self.agent_server
        body = self._read_body()

        if path == "/api/agent/chat":
            if not self._check_auth():
                self._json({"error": "Unauthorized"}, 401)
                return

            user_msg = body.get("message", "").strip()
            if not user_msg:
                self._json({"error": "Empty message"}, 400)
                return

            auth = get_auth()
            auth.audit("agent_chat", actor="remote", target=srv.agent.id,
                       detail=user_msg[:200],
                       ip=self.client_address[0])

            self._sse_start()

            def on_event(evt: AgentEvent):
                if evt.kind == "message" and evt.data.get("role") == "assistant":
                    self._sse_send({"type": "text",
                                    "content": evt.data.get("content", "")})
                elif evt.kind == "tool_call":
                    self._sse_send({
                        "type": "tool_call",
                        "name": evt.data.get("name", ""),
                        "args": json.dumps(
                            evt.data.get("arguments", {}),
                            ensure_ascii=False)[:200],
                    })
                elif evt.kind == "tool_result":
                    self._sse_send({
                        "type": "tool_result",
                        "content": evt.data.get("result", "")[:500],
                    })
                elif evt.kind == "approval":
                    self._sse_send({
                        "type": "approval",
                        "tool": evt.data.get("tool", ""),
                        "status": evt.data.get("status", ""),
                        "reason": evt.data.get("reason", ""),
                    })
                elif evt.kind == "error":
                    self._sse_send({"type": "error",
                                    "content": evt.data.get("error", "")})

            result_holder = [None]

            def _do_chat():
                result_holder[0] = srv.agent.chat(user_msg, on_event=on_event)

            t = threading.Thread(target=_do_chat)
            t.start()
            t.join(timeout=300)

            self._sse_send({"type": "done"})
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

            # Auto-save agent state after chat
            srv._auto_save_async()

        elif path == "/api/agent/clear":
            if not self._check_auth():
                self._json({"error": "Unauthorized"}, 401)
                return
            srv.agent.clear()
            srv._auto_save_async()
            self._json({"ok": True})

        elif path == "/api/agent/delegate":
            if not self._check_auth():
                self._json({"error": "Unauthorized"}, 401)
                return
            task = body.get("task", body.get("content", ""))
            from_agent = body.get("from_agent", "remote")
            if not task:
                self._json({"error": "No task provided"}, 400)
                return

            auth = get_auth()
            auth.audit("agent_delegate", actor=from_agent,
                       target=srv.agent.id, detail=task[:200],
                       ip=self.client_address[0])

            # Run in background and save after completion
            def _run():
                srv.agent.delegate(task, from_agent=from_agent)
                srv._auto_save_async()

            threading.Thread(target=_run, daemon=True).start()
            self._json({"ok": True, "status": "delegated"})

        elif path == "/api/agent/approve":
            if not self._check_auth():
                self._json({"error": "Unauthorized"}, 401)
                return
            approval_id = body.get("approval_id", "")
            action = body.get("action", "approve")  # approve or deny
            auth = get_auth()
            if action == "approve":
                ok = auth.tool_policy.approve(approval_id, decided_by="remote")
            else:
                ok = auth.tool_policy.deny(approval_id, decided_by="remote")
            self._json({"ok": ok})

        elif path == "/api/agent/model":
            if not self._check_auth():
                self._json({"error": "Unauthorized"}, 401)
                return
            srv.agent.provider = body.get("provider", "")
            srv.agent.model = body.get("model", "")
            srv._auto_save_async()
            self._json({"ok": True,
                         "provider": srv.agent.provider,
                         "model": srv.agent.model})

        # Hub deliver (for receiving messages from portal)
        elif path == "/api/hub/deliver":
            task = body.get("content", "")
            from_agent = body.get("from_agent", "hub")
            if task:
                threading.Thread(
                    target=srv.agent.delegate,
                    args=(task, from_agent),
                    daemon=True,
                ).start()
            self._json({"ok": True})

        # Hub apply-config: Receive agent config from Hub
        elif path == "/api/hub/apply-config":
            if not self._check_auth():
                self._json({"error": "Unauthorized"}, 401)
                return
            config = body.get("config", {})
            if not config:
                self._json({"error": "No config provided"}, 400)
                return
            try:
                # Apply config fields to agent (name, role, model, provider, etc.)
                for key, value in config.items():
                    if hasattr(srv.agent, key) and key not in ("id", "events"):
                        setattr(srv.agent, key, value)
                srv._auto_save_async()
                self._json({"ok": True, "message": "Agent config applied"})
            except Exception as e:
                self._json({"error": str(e)}, 500)

        # Hub apply-mcp: Receive MCP config from Hub
        elif path == "/api/hub/apply-mcp":
            if not self._check_auth():
                self._json({"error": "Unauthorized"}, 401)
                return
            mcp_id = body.get("mcp_id", "")
            mcp_config = body.get("config", {})
            if not mcp_id or not mcp_config:
                self._json({"error": "Missing mcp_id or config"}, 400)
                return
            try:
                # Save MCP config locally
                from . import DEFAULT_DATA_DIR
                mcp_dir = os.path.join(DEFAULT_DATA_DIR, "mcps")
                os.makedirs(mcp_dir, exist_ok=True)
                mcp_file = os.path.join(mcp_dir, f"{mcp_id}.json")
                with open(mcp_file, "w") as f:
                    json.dump(mcp_config, f, indent=2)
                self._json({"ok": True, "message": f"MCP {mcp_id} applied"})
            except Exception as e:
                self._json({"error": str(e)}, 500)

        # Hub apply-node-config: Receive node config items from Hub
        elif path == "/api/hub/apply-node-config":
            if not self._check_auth():
                self._json({"error": "Unauthorized"}, 401)
                return
            config_items = body.get("config", {})
            if not config_items:
                self._json({"error": "No config items provided"}, 400)
                return
            try:
                # Save node config locally
                from . import DEFAULT_DATA_DIR as _DDD
                config_dir = _DDD
                os.makedirs(config_dir, exist_ok=True)
                config_file = os.path.join(config_dir, "node_config.json")
                with open(config_file, "w") as f:
                    json.dump(config_items, f, indent=2)
                self._json({"ok": True, "message": "Node config applied"})
            except Exception as e:
                self._json({"error": str(e)}, 500)

        else:
            self.send_error(404)


# ---------------------------------------------------------------------------
# Launcher
# ---------------------------------------------------------------------------

def run_agent_server(
    name: str = "",
    role: str = "general",
    port: int = 8081,
    model: str = "",
    provider: str = "",
    working_dir: str = "",
    hub_url: str = "",
    secret: str = "",
    profile_overrides: dict | None = None,
    agent_id: str = "",
):
    """Start a standalone agent server.

    If *agent_id* is provided (or matches a saved workspace), the agent
    state is restored from ``~/.tudou_claw/workspaces/{agent_id}/agent.json``
    so that data survives node restarts.
    """
    agent: Agent | None = None

    # 1) Try to restore from workspace if agent_id given
    if agent_id:
        agent = AgentServer.load_agent_from_workspace(agent_id)

    # 2) If no explicit id, scan saved agents matching name/role
    if agent is None and name:
        for saved in AgentServer.list_saved_agents():
            if saved["name"] == name:
                agent = AgentServer.load_agent_from_workspace(saved["id"])
                if agent is not None:
                    break

    # 3) Create fresh agent only if nothing found on disk
    if agent is None:
        agent = create_agent(
            name=name, role=role, model=model,
            provider=provider, working_dir=working_dir,
            profile_overrides=profile_overrides,
        )
    else:
        # Apply any overrides to restored agent
        if model:
            agent.model = model
        if provider:
            agent.provider = provider
        if working_dir:
            agent.working_dir = working_dir

    server = AgentServer(
        agent=agent, port=port,
        hub_url=hub_url, secret=secret,
    )
    server.start()
