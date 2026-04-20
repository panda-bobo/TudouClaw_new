"""
Entry point for the Tudou Claws AI Assistant.

Usage:
    python -m app                      # Start interactive REPL
    python -m app repl                  # Same as above
    python -m app web [--port 8080]     # Start simple web chat
    python -m app portal [--port 9090]  # Start Portal dashboard (master)
    python -m app agent [--port 8081]   # Start standalone agent (worker)

Portal mode (master):
    python -m app portal --port 9090 --secret mykey123
    # Opens dashboard at http://localhost:9090
    # Prints admin token on first launch — save it!

Agent mode (worker):
    python -m app agent --name Coder --role coder --port 8081 \\
        --hub http://portal-host:9090 --secret mykey123
    # Starts standalone agent, auto-registers with portal
"""
import argparse
import sys


def run_node(args):
    """Start TudouClaw in Node mode — connects to Master and executes Agent tasks."""
    import os
    master_url = os.environ.get("TUDOU_MASTER_URL", "")
    master_secret = os.environ.get("TUDOU_MASTER_SECRET", "")
    node_name = os.environ.get("TUDOU_NODE_NAME", f"node-{os.getpid()}")

    if not master_url:
        print("ERROR: TUDOU_MASTER_URL environment variable is required for node mode.")
        print("Example: TUDOU_MASTER_URL=ws://192.168.1.10:9090/ws/node python -m app node")
        sys.exit(1)

    if not master_secret:
        print("ERROR: TUDOU_MASTER_SECRET environment variable is required.")
        sys.exit(1)

    port = getattr(args, 'port', 8081)

    print(f"Starting TudouClaw Node: {node_name}")
    print(f"  Master: {master_url}")
    print(f"  Port: {port}")

    # Import and start the node
    try:
        from .infra.ws_bus import WSBusClient, get_ws_client
        from .infra.node_manager import NodeInfo

        # Initialize WS client connection to master
        client = get_ws_client()
        # The actual connection logic will be expanded as ws_bus matures

        print(f"Node '{node_name}' started. Waiting for Master connection...")
        print("Press Ctrl+C to stop.")

        # For now, start portal in node mode if available
        from .portal import run_portal
        run_portal(port=port, mode="node")
    except ImportError as e:
        print(f"Missing dependency: {e}")
        print("Install websockets: pip install websockets")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\nNode shutting down.")


def main():
    parser = argparse.ArgumentParser(
        prog="claw",
        description="Tudou Claws — AI Programming Assistant with Multi-Agent Portal",
    )
    sub = parser.add_subparsers(dest="command", help="Command to run")

    # ---- repl ----
    p_repl = sub.add_parser("repl", help="Interactive terminal REPL")
    p_repl.add_argument("--provider", type=str, default=None)
    p_repl.add_argument("--model", type=str, default=None)

    # ---- web ----
    p_web = sub.add_parser("web", help="Simple web chat interface")
    from .defaults import WEB_PORT, PORTAL_PORT, AGENT_PORT
    p_web.add_argument("--port", type=int, default=WEB_PORT)
    p_web.add_argument("--provider", type=str, default=None)
    p_web.add_argument("--model", type=str, default=None)

    # ---- portal (master) ----
    p_portal = sub.add_parser("portal", help="Portal dashboard — manage multi-agent")
    p_portal.add_argument("--port", type=int, default=PORTAL_PORT)
    p_portal.add_argument("--secret", type=str, default="",
                          help="Shared secret for agent registration auth")
    p_portal.add_argument("--admin-token", type=str, default="",
                          help="Set a specific admin token (default: auto-generated)")
    p_portal.add_argument("--node-name", type=str, default="",
                          help="Name for this portal node")
    p_portal.add_argument("--data-dir", type=str, default="",
                          help="Runtime data directory (default: macOS /Users/pangwanchun/.tudou_claw, Linux /home/tudou_claw/.tudou_claw)")
    p_portal.add_argument("--provider", type=str, default=None)
    p_portal.add_argument("--model", type=str, default=None)

    # ---- agent (worker) ----
    p_agent = sub.add_parser("agent", help="Standalone agent server")
    p_agent.add_argument("--name", type=str, default="",
                         help="Agent name (default: from role preset)")
    p_agent.add_argument("--role", type=str, default="general",
                         choices=["general", "coder", "reviewer",
                                  "researcher", "architect", "devops"],
                         help="Agent role (default: general)")
    p_agent.add_argument("--port", type=int, default=AGENT_PORT)
    p_agent.add_argument("--hub", type=str, default="",
                         help="Portal hub URL to register with "
                              "(e.g. http://portal-host:9090)")
    p_agent.add_argument("--secret", type=str, default="",
                         help="Shared secret for hub authentication")
    p_agent.add_argument("--working-dir", type=str, default="",
                         help="Working directory for this agent")
    p_agent.add_argument("--data-dir", type=str, default="",
                         help="Runtime data directory (default: macOS /Users/pangwanchuk/.tudou_claw, Linux /home/tudou_claw/.tudou_claw)")
    p_agent.add_argument("--provider", type=str, default=None,
                         help="Initial LLM provider (can be changed from Portal)")
    p_agent.add_argument("--model", type=str, default=None,
                         help="Initial model (can be changed from Portal)")
    p_agent.add_argument("--personality", type=str, default=None,
                         help="Agent personality (e.g. friendly, formal, strict)")
    p_agent.add_argument("--language", type=str, default=None,
                         help="Response language (e.g. zh-CN, en, ja, auto)")
    p_agent.add_argument("--expertise", type=str, default=None,
                         help="Comma-separated expertise areas")
    p_agent.add_argument("--agent-id", type=str, default="",
                         help="Restore agent from saved workspace by ID")

    # ---- node (distributed worker) ----
    p_node = sub.add_parser("node", help="Start as a distributed Node worker (connects to Master)")
    p_node.add_argument("--port", type=int, default=8081)

    # ---- Backward compat: --web, --portal flags ----
    parser.add_argument("--web", action="store_true",
                        help="(legacy) Start web interface")
    parser.add_argument("--portal", action="store_true",
                        help="(legacy) Start portal dashboard")
    parser.add_argument("--port", type=int, default=None,
                        help="(legacy) Port number")
    parser.add_argument("--provider", type=str, default=None)
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--secret", type=str, default="")
    parser.add_argument("--node-name", type=str, default="")

    args = parser.parse_args()

    # Apply global provider/model defaults from CLI flags.
    # Individual agents can override these via Portal UI at any time.
    from . import llm
    provider = getattr(args, "provider", None)
    model = getattr(args, "model", None)
    if provider:
        cfg = llm.get_config()
        cfg["provider"] = provider
    if model:
        llm.set_model(model)

    # Route to the right command
    command = args.command

    # Handle legacy flags
    if not command:
        if getattr(args, "portal", False):
            command = "portal"
        elif getattr(args, "web", False):
            command = "web"
        else:
            command = "repl"

    if command == "repl":
        from .repl import run_repl
        run_repl()

    elif command == "web":
        port = getattr(args, "port", None) or 8080
        from .web import run_web
        run_web(port=port)

    elif command == "portal":
        port = getattr(args, "port", None) or 9090
        secret = getattr(args, "secret", "")
        admin_token = getattr(args, "admin_token", "")
        node_name = getattr(args, "node_name", "")
        data_dir = getattr(args, "data_dir", "")

        # ── Which backend? ───────────────────────────────────────────
        # Default: FastAPI (app/api/main.py, uvicorn). This is the
        # modern path with typed routes, OpenAPI docs, and ~1233 uses
        # of Depends/HTTPException/BackgroundTasks.
        #
        # Fallback: stdlib BaseHTTPRequestHandler (app/server/portal_*).
        # Set TUDOU_USE_STDLIB=1 to opt back in. Kept so a bad FastAPI
        # deploy is reversible without a code change.
        import os as _os
        use_stdlib = _os.environ.get("TUDOU_USE_STDLIB", "").lower() in (
            "1", "true", "yes", "on")

        if use_stdlib:
            from .portal import run_portal
            run_portal(port=port, node_name=node_name,
                       secret=secret, admin_token=admin_token,
                       data_dir=data_dir)
        else:
            # Plumb CLI flags into env vars the FastAPI lifespan reads.
            if secret:
                _os.environ["TUDOU_ADMIN_SECRET"] = secret
            if admin_token:
                _os.environ["TUDOU_ADMIN_TOKEN"] = admin_token
            if data_dir:
                _os.environ["TUDOU_CLAW_DATA_DIR"] = data_dir
            if node_name:
                _os.environ["TUDOU_NODE_NAME"] = node_name
            _os.environ["TUDOU_PORT"] = str(port)   # banner reads this
            try:
                import uvicorn
            except ImportError:
                print("ERROR: FastAPI path requires uvicorn.")
                print("  Install: pip install uvicorn[standard]")
                print("  Or use stdlib fallback: TUDOU_USE_STDLIB=1 python -m app portal")
                sys.exit(1)
            # Bind to all interfaces so the dashboard is reachable from
            # LAN the same way the old server was.
            from .defaults import BIND_ADDRESS
            uvicorn.run(
                "app.api.main:app",
                host=BIND_ADDRESS,
                port=port,
                log_level="info",
                # reload off in prod — turn on via dev flag if needed
                reload=False,
            )

    elif command == "agent":
        # Build profile overrides from CLI args
        profile_overrides = {}
        if args.personality:
            profile_overrides["personality"] = args.personality
        if args.language:
            profile_overrides["language"] = args.language
        if args.expertise:
            profile_overrides["expertise"] = [
                x.strip() for x in args.expertise.split(",")]

        from .agent_server import run_agent_server
        run_agent_server(
            name=args.name,
            role=args.role,
            port=args.port,
            model=model or "",
            provider=provider or "",
            working_dir=args.working_dir,
            hub_url=args.hub,
            secret=args.secret,
            profile_overrides=profile_overrides or None,
            agent_id=getattr(args, "agent_id", ""),
        )

    elif command == "node":
        run_node(args)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
