"""Channels router — channel CRUD, webhook receiver, test messaging."""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Body, Request

from ..deps.hub import get_hub
from ..deps.auth import CurrentUser, get_current_user

logger = logging.getLogger("tudouclaw.api.channels")

router = APIRouter(prefix="/api/portal", tags=["channels"])


# ---------------------------------------------------------------------------
# Channel listing
# ---------------------------------------------------------------------------

@router.get("/channels")
async def list_channels(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List all channels — matches legacy portal_routes_get."""
    try:
        from ...channel import get_router as get_ch_router
        ch_router = get_ch_router()
        channels = ch_router.list_channels()
        return {"channels": [ch.to_dict(mask_secrets=True) for ch in channels]}
    except ImportError:
        return {"channels": []}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/channels/events")
async def get_channel_events(
    limit: int = Query(100, ge=1, le=1000),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get channel event log."""
    try:
        from ...channel import get_router as get_ch_router
        ch_router = get_ch_router()
        events = ch_router.get_event_log(limit=limit)
        return {"events": events}
    except ImportError:
        return {"events": []}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/channels/{channel_id}")
async def get_channel(
    channel_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get a single channel by ID."""
    try:
        from ...channel import get_router as get_ch_router
        ch_router = get_ch_router()
        ch = ch_router.get_channel(channel_id)
        if not ch:
            raise HTTPException(404, "Channel not found")
        return ch.to_dict(mask_secrets=True)
    except HTTPException:
        raise
    except ImportError:
        raise HTTPException(501, "Channel module not available")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Channel CRUD
# ---------------------------------------------------------------------------

@router.post("/channels")
async def create_channel(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Create a new channel.

    Required fields: ``name`` (non-empty), ``channel_type``, ``agent_id``.
    A channel without a name/agent is junk and clutters the dashboard;
    reject them at the gate instead of letting the DB fill with ghosts.
    """
    name = (body.get("name") or "").strip()
    agent_id = (body.get("agent_id") or "").strip()
    channel_type_raw = (body.get("channel_type") or "").strip()
    if not name:
        raise HTTPException(400, "name is required (non-empty)")
    if not agent_id:
        raise HTTPException(400, "agent_id is required — bind the channel to an agent")
    if not channel_type_raw:
        raise HTTPException(400, "channel_type is required (webhook|slack|telegram|...)")
    try:
        from ...channel import get_router as get_ch_router, ChannelType
        ch_router = get_ch_router()
        try:
            ctype = ChannelType(channel_type_raw)
        except ValueError:
            raise HTTPException(400, f"unknown channel_type: {channel_type_raw!r}")
        ch = ch_router.add_channel(
            name=name,
            channel_type=ctype,
            agent_id=agent_id,
            bot_token=body.get("bot_token", ""),
            signing_secret=body.get("signing_secret", ""),
            webhook_url=body.get("webhook_url", ""),
            app_id=body.get("app_id", ""),
            app_secret=body.get("app_secret", ""),
        )
        return ch.to_dict(mask_secrets=True)
    except ImportError:
        raise HTTPException(501, "Channel module not available")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/channels/{channel_id}/update")
async def update_channel(
    channel_id: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Update channel configuration."""
    try:
        from ...channel import get_router as get_ch_router
        ch_router = get_ch_router()
        kwargs = {}
        for k in ("name", "channel_type", "agent_id", "bot_token",
                   "signing_secret", "webhook_url", "app_id",
                   "app_secret", "enabled"):
            if k in body:
                if k in ("bot_token", "signing_secret", "app_secret") and body[k] == "********":
                    continue
                kwargs[k] = body[k]
        ch = ch_router.update_channel(channel_id, **kwargs)
        if not ch:
            raise HTTPException(404, "Channel not found")
        return ch.to_dict(mask_secrets=True)
    except HTTPException:
        raise
    except ImportError:
        raise HTTPException(501, "Channel module not available")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/channels/{channel_id}")
async def delete_channel(
    channel_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Delete a channel."""
    try:
        from ...channel import get_router as get_ch_router
        ch_router = get_ch_router()
        ok = ch_router.remove_channel(channel_id)
        return {"ok": ok}
    except ImportError:
        raise HTTPException(501, "Channel module not available")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Webhook and test
# ---------------------------------------------------------------------------

@router.post("/channels/{channel_id}/webhook")
async def channel_webhook(
    channel_id: str,
    body: dict = Body(...),
    request: Request = None,
):
    """Inbound webhook receiver (public, no auth required)."""
    try:
        from ...channel import get_router as get_ch_router
        ch_router = get_ch_router()
        headers_dict = dict(request.headers) if request else {}
        result = ch_router.handle_inbound(channel_id, body, headers_dict)
        return result
    except ImportError:
        raise HTTPException(501, "Channel module not available")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/channels/{channel_id}/test")
async def test_channel(
    channel_id: str,
    body: dict = Body(default={}),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Test a channel connection.

    If the adapter has ``test_connection()``, calls it first.
    Then, for polling-mode channels, ensures the poller is running.
    """
    try:
        from ...channel import get_router as get_ch_router
        ch_router = get_ch_router()
        adapter = ch_router._adapters.get(channel_id)
        ch = ch_router.get_channel(channel_id)
        if not adapter or not ch:
            raise HTTPException(404, "Channel not found")

        # Platform-specific connection test (e.g. Telegram getMe)
        result = adapter.test_connection()
        if not result.get("ok", True):
            return {"ok": False, "success": False, "error": result.get("error", "unknown")}

        # Ensure polling is running for polling-mode channels
        if ch.mode == "polling" and adapter.supports_polling:
            if not adapter.is_polling and ch_router._agent_chat_fn:
                adapter.start_polling(ch_router.handle_inbound)
            poll_status = "active" if adapter.is_polling else "waiting (agent chat not bound)"
        else:
            poll_status = None

        resp: dict = {
            "ok": True, "success": True,
            "mode": ch.mode,
        }
        # Include platform-specific info
        if result.get("bot"):
            resp["message"] = f"Bot @{result['bot']} ({result.get('name', '')}) connected!"
        elif result.get("message"):
            resp["message"] = result["message"]
        else:
            resp["message"] = "Connection OK"
        if poll_status:
            resp["polling"] = poll_status
        return resp
    except ImportError:
        raise HTTPException(501, "Channel module not available")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
