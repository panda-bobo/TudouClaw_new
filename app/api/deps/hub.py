"""Hub singleton dependency — bridges FastAPI with existing TudouClaw core."""
from __future__ import annotations

import os
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ...hub import Hub

logger = logging.getLogger("tudouclaw.api.deps")

_hub_instance: "Hub | None" = None


def init_hub() -> "Hub":
    """Initialize and return the Hub singleton.

    Called once during FastAPI lifespan startup.
    Reuses the existing Hub initialization logic from the old portal server.

    Env var pass-through (parity with legacy ``run_portal``):
      - TUDOU_NODE_NAME  → Hub.node_name  (cosmetic, shown on banner/UI)
      - TUDOU_CLAW_DATA_DIR is read by Hub itself.
    """
    global _hub_instance
    if _hub_instance is not None:
        return _hub_instance

    try:
        from ...hub import Hub
        node_name = os.environ.get("TUDOU_NODE_NAME", "")
        _hub_instance = Hub(node_name=node_name) if node_name else Hub()
        logger.info("Hub initialized: node_id=%s node_name=%s",
                    _hub_instance.node_id, _hub_instance.node_name)
    except Exception as e:
        logger.error("Failed to initialize Hub: %s", e)
        raise
    return _hub_instance


def shutdown_hub():
    """Clean up Hub on shutdown."""
    global _hub_instance
    if _hub_instance is not None:
        try:
            if hasattr(_hub_instance, "shutdown"):
                _hub_instance.shutdown()
            elif hasattr(_hub_instance, "close"):
                _hub_instance.close()
        except Exception as e:
            logger.warning("Hub shutdown error: %s", e)
        _hub_instance = None


def get_hub() -> "Hub":
    """FastAPI dependency: get the Hub singleton.

    Usage:
        @router.get("/api/portal/agents")
        async def list_agents(hub: Hub = Depends(get_hub)):
            ...
    """
    if _hub_instance is None:
        raise RuntimeError("Hub not initialized — did the lifespan fail?")
    return _hub_instance
