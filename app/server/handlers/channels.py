"""


╔════════════════════════════════════════════════════════════════════════╗
║  ⚠️  DEPRECATED — LEGACY stdlib handler (extracted from                ║
║     portal_routes_post.py). Active only when TUDOU_USE_STDLIB=1 is     ║
║     set at launch. FastAPI (app/api/routers/*) is authoritative.       ║
║     Do NOT add new routes here.                                        ║
╚════════════════════════════════════════════════════════════════════════╝
channels — handler for channel CRUD and webhook endpoints.

Extracted from portal_routes_post.py.  Handles:

    POST /api/portal/channels              — create a new channel
    POST /api/portal/channels/{id}/update  — update channel config
    POST /api/portal/channels/{id}/webhook — inbound webhook receiver (public)
    POST /api/portal/channels/{id}/test    — send test message to channel
"""
from __future__ import annotations

import logging

from ...channel import get_router, ChannelType
from ..portal_auth import get_client_ip

logger = logging.getLogger("tudou.portal")


# ------------------------------------------------------------------
# Public API
# ------------------------------------------------------------------

def try_handle(handler, path: str, hub, body: dict, auth, actor_name: str, user_role: str) -> bool:
    """Return *True* if *path* was handled by this module, *False* otherwise."""

    # ---- Create channel ----
    if path == "/api/portal/channels":
        router = get_router()
        ch = router.add_channel(
            name=body.get("name", ""),
            channel_type=ChannelType(body.get("channel_type", "webhook")),
            agent_id=body.get("agent_id", ""),
            bot_token=body.get("bot_token", ""),
            signing_secret=body.get("signing_secret", ""),
            webhook_url=body.get("webhook_url", ""),
            app_id=body.get("app_id", ""),
            app_secret=body.get("app_secret", ""),
        )
        auth.audit("add_channel", actor=actor_name, role=user_role,
                   target=ch.id, ip=get_client_ip(handler))
        handler._json(ch.to_dict(mask_secrets=True))
        return True

    # ---- Update channel ----
    if path.startswith("/api/portal/channels/") and path.endswith("/update"):
        channel_id = path.split("/")[4]
        router = get_router()
        kwargs = {}
        for k in ("name", "channel_type", "agent_id", "bot_token",
                   "signing_secret", "webhook_url", "app_id",
                   "app_secret", "enabled"):
            if k in body:
                # Don't overwrite secrets with mask
                if k in ("bot_token", "signing_secret", "app_secret") and body[k] == "********":
                    continue
                kwargs[k] = body[k]
        ch = router.update_channel(channel_id, **kwargs)
        if ch:
            auth.audit("update_channel", actor=actor_name, role=user_role,
                       target=channel_id, ip=get_client_ip(handler))
            handler._json(ch.to_dict(mask_secrets=True))
        else:
            handler._json({"error": "Channel not found"}, 404)
        return True

    # ---- Inbound webhook receiver (public, no auth required) ----
    if path.startswith("/api/portal/channels/") and path.endswith("/webhook"):
        channel_id = path.split("/")[4]
        router = get_router()
        headers_dict = {k: handler.headers.get(k, "") for k in handler.headers}
        result = router.handle_inbound(channel_id, body, headers_dict)
        handler._json(result)
        return True

    # ---- Send test message ----
    if path.startswith("/api/portal/channels/") and path.endswith("/test"):
        channel_id = path.split("/")[4]
        router = get_router()
        ok = router.send_to_channel(
            channel_id,
            "\U0001f954 Tudou Claws test message \u2014 channel is connected!",
            {"channel_id": body.get("channel_id", "test")},
        )
        handler._json({"ok": ok})
        return True

    return False
