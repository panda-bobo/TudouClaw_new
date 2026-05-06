"""
Channel — unified messaging interface for connecting agents to external platforms.

Each Channel represents a bidirectional communication link between an agent and
an external messaging platform (Slack, Telegram, Discord, DingTalk, Feishu,
or a generic Webhook).

Architecture:
    External Platform  ←→  ChannelAdapter  ←→  ChannelRouter  ←→  Agent.chat()

Inbound:  Platform webhook/polling → adapter.receive() → agent.chat() → adapter.send()
Outbound: Agent event → adapter.send() → Platform API

Each adapter implements:
    - send(text, metadata)    → push message to the platform
    - receive(raw_payload)    → parse incoming message, return (text, metadata)
    - verify(request)         → verify webhook signature (optional)
"""
from __future__ import annotations
import hashlib
import hmac
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable
from http.server import HTTPServer, BaseHTTPRequestHandler

import logging
import requests

logger = logging.getLogger("tudouclaw.channel")


# ---------------------------------------------------------------------------
# Channel types
# ---------------------------------------------------------------------------

class ChannelType(str, Enum):
    SLACK = "slack"
    TELEGRAM = "telegram"
    DISCORD = "discord"
    DINGTALK = "dingtalk"
    FEISHU = "feishu"
    WEBHOOK = "webhook"       # generic inbound/outbound webhook
    WECHAT_WORK = "wechat_work"


# ---------------------------------------------------------------------------
# Channel config
# ---------------------------------------------------------------------------

@dataclass
class ChannelConfig:
    """Configuration for one channel connection."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str = ""
    channel_type: ChannelType = ChannelType.WEBHOOK
    agent_id: str = ""            # bound agent
    enabled: bool = True

    # Auth & endpoint config (varies by type)
    bot_token: str = ""           # Slack bot token / Telegram bot token / etc.
    signing_secret: str = ""      # webhook verification secret
    webhook_url: str = ""         # outbound webhook URL (for sending messages out)
    app_id: str = ""              # DingTalk app_id / Feishu app_id
    app_secret: str = ""          # DingTalk app_secret / Feishu app_secret

    # Inbound mode: "webhook" (platform pushes to us) or "polling" (we pull from platform)
    mode: str = "polling"  # "webhook" | "polling"

    # Filtering
    allowed_channels: list[str] = field(default_factory=list)  # e.g. Slack channel IDs
    allowed_users: list[str] = field(default_factory=list)

    created_at: float = field(default_factory=time.time)

    def to_dict(self, mask_secrets: bool = False) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "channel_type": self.channel_type.value,
            "agent_id": self.agent_id,
            "enabled": self.enabled,
            "mode": self.mode,
            "webhook_url": self.webhook_url,
            "app_id": self.app_id,
            "allowed_channels": self.allowed_channels,
            "allowed_users": self.allowed_users,
            "created_at": self.created_at,
        }
        if mask_secrets:
            d["bot_token"] = "********" if self.bot_token else ""
            d["signing_secret"] = "********" if self.signing_secret else ""
            d["app_secret"] = "********" if self.app_secret else ""
        else:
            d["bot_token"] = self.bot_token
            d["signing_secret"] = self.signing_secret
            d["app_secret"] = self.app_secret
        return d

    @staticmethod
    def from_dict(d: dict) -> ChannelConfig:
        return ChannelConfig(
            id=d.get("id", ""),
            name=d.get("name", ""),
            channel_type=ChannelType(d.get("channel_type", "webhook")),
            agent_id=d.get("agent_id", ""),
            enabled=d.get("enabled", True),
            bot_token=d.get("bot_token", ""),
            signing_secret=d.get("signing_secret", ""),
            webhook_url=d.get("webhook_url", ""),
            app_id=d.get("app_id", ""),
            app_secret=d.get("app_secret", ""),
            mode=d.get("mode", "polling"),
            allowed_channels=d.get("allowed_channels", []),
            allowed_users=d.get("allowed_users", []),
            created_at=d.get("created_at", 0),
        )


# ---------------------------------------------------------------------------
# Message abstraction
# ---------------------------------------------------------------------------

@dataclass
class ChannelMessage:
    """A normalised message from any platform."""
    text: str = ""
    sender_id: str = ""
    sender_name: str = ""
    channel_id: str = ""         # platform-specific channel/group ID
    channel_name: str = ""
    platform: str = ""           # "slack", "telegram", etc.
    raw: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    reply_to: str = ""           # message ID for threading

    def to_dict(self) -> dict:
        return {
            "text": self.text, "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "channel_id": self.channel_id,
            "channel_name": self.channel_name,
            "platform": self.platform,
            "timestamp": self.timestamp,
            "reply_to": self.reply_to,
        }


# ---------------------------------------------------------------------------
# Adapters — one per platform
# ---------------------------------------------------------------------------

class BaseAdapter:
    """Base class for channel adapters.

    Every adapter supports two inbound modes controlled by ``config.mode``:
    - **webhook** — the platform pushes updates to our HTTP endpoint.
    - **polling** — we periodically pull updates from the platform API.

    Subclasses that want polling override ``_poll_loop()``.
    ``start_polling`` / ``stop_polling`` are provided by the base class.
    """

    def __init__(self, config: ChannelConfig):
        self.config = config
        self._poll_thread: threading.Thread | None = None
        self._poll_stop = threading.Event()
        self._inbound_handler: Callable | None = None

    def send(self, text: str, metadata: dict | None = None) -> bool:
        """Send a message to the platform. Returns True on success."""
        raise NotImplementedError

    def parse_inbound(self, payload: dict, headers: dict | None = None) -> ChannelMessage | None:
        """Parse an inbound webhook payload into a ChannelMessage."""
        raise NotImplementedError

    def verify_signature(self, body: bytes, headers: dict) -> bool:
        """Verify webhook signature. Default: always True."""
        return True

    def test_connection(self) -> dict:
        """Test if the adapter can connect to the platform.
        Override for platform-specific health checks.
        """
        return {"ok": True, "message": "No test implemented for this platform"}

    # ---- Polling (generic) ----

    @property
    def supports_polling(self) -> bool:
        """Whether this adapter has a ``_poll_loop`` implementation."""
        return type(self)._poll_loop is not BaseAdapter._poll_loop

    @property
    def is_polling(self) -> bool:
        return self._poll_thread is not None and self._poll_thread.is_alive()

    def start_polling(self, handler: Callable):
        """Start background polling thread.

        Args:
            handler: ``(channel_config_id, payload, headers) -> dict``
        """
        if not self.supports_polling:
            logger.debug("Adapter %s does not support polling", type(self).__name__)
            return
        if self.is_polling:
            return
        self._inbound_handler = handler
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True,
            name=f"poll-{self.config.channel_type.value}-{self.config.id[:8]}",
        )
        self._poll_thread.start()
        logger.info("Polling started: %s channel '%s' (%s)",
                     self.config.channel_type.value, self.config.name, self.config.id)

    def stop_polling(self):
        self._poll_stop.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
            self._poll_thread = None
        logger.info("Polling stopped: %s channel '%s'",
                     self.config.channel_type.value, self.config.name)

    def _poll_loop(self):
        """Override in subclass to implement platform-specific long-polling."""
        pass


class SlackAdapter(BaseAdapter):
    """Slack Bot adapter using Web API + Events API."""

    def send(self, text: str, metadata: dict | None = None) -> bool:
        meta = metadata or {}
        channel = meta.get("channel_id", "")
        thread_ts = meta.get("reply_to", "")
        if not channel or not self.config.bot_token:
            return False
        payload: dict = {
            "channel": channel,
            "text": text,
        }
        if thread_ts:
            payload["thread_ts"] = thread_ts
        try:
            resp = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {self.config.bot_token}",
                    "Content-Type": "application/json",
                },
                json=payload, timeout=15,
            )
            data = resp.json()
            return data.get("ok", False)
        except Exception:
            return False

    def parse_inbound(self, payload: dict, headers: dict | None = None) -> ChannelMessage | None:
        # Slack Events API
        if payload.get("type") == "url_verification":
            return None  # challenge, handled separately

        event = payload.get("event", {})
        if event.get("type") not in ("message", "app_mention"):
            return None
        if event.get("bot_id"):
            return None  # ignore bot messages

        return ChannelMessage(
            text=event.get("text", ""),
            sender_id=event.get("user", ""),
            channel_id=event.get("channel", ""),
            platform="slack",
            raw=payload,
            reply_to=event.get("thread_ts", event.get("ts", "")),
        )

    def verify_signature(self, body: bytes, headers: dict) -> bool:
        if not self.config.signing_secret:
            return True
        timestamp = headers.get("X-Slack-Request-Timestamp", "")
        sig_header = headers.get("X-Slack-Signature", "")
        if not timestamp or not sig_header:
            return False
        basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
        computed = "v0=" + hmac.new(
            self.config.signing_secret.encode(),
            basestring.encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(computed, sig_header)


class TelegramAdapter(BaseAdapter):
    """Telegram Bot API adapter — supports both webhook and polling mode."""

    def __init__(self, config: ChannelConfig):
        super().__init__(config)
        self._offset: int = 0  # Telegram getUpdates offset

    def send(self, text: str, metadata: dict | None = None) -> bool:
        meta = metadata or {}
        chat_id = meta.get("channel_id", "")
        reply_to = meta.get("reply_to", "")
        if not chat_id or not self.config.bot_token:
            return False
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown",
        }
        if reply_to:
            payload["reply_to_message_id"] = reply_to
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{self.config.bot_token}/sendMessage",
                json=payload, timeout=15,
            )
            return resp.json().get("ok", False)
        except Exception:
            return False

    def parse_inbound(self, payload: dict, headers: dict | None = None) -> ChannelMessage | None:
        msg = payload.get("message", {})
        if not msg:
            return None
        text = msg.get("text", "")
        if not text:
            return None
        user = msg.get("from", {})
        chat = msg.get("chat", {})
        return ChannelMessage(
            text=text,
            sender_id=str(user.get("id", "")),
            sender_name=user.get("first_name", "") + " " + user.get("last_name", ""),
            channel_id=str(chat.get("id", "")),
            channel_name=chat.get("title", ""),
            platform="telegram",
            raw=payload,
            reply_to=str(msg.get("message_id", "")),
        )

    def _poll_loop(self):
        """Long-poll Telegram ``getUpdates``."""
        api_base = f"https://api.telegram.org/bot{self.config.bot_token}"
        # Delete any existing webhook so getUpdates works
        try:
            requests.post(f"{api_base}/deleteWebhook", timeout=10)
        except Exception:
            pass

        while not self._poll_stop.is_set():
            try:
                resp = requests.get(
                    f"{api_base}/getUpdates",
                    params={"offset": self._offset, "timeout": 30},
                    timeout=35,
                )
                data = resp.json()
                if not data.get("ok"):
                    logger.warning("Telegram getUpdates error: %s", data)
                    self._poll_stop.wait(5)
                    continue
                for update in data.get("result", []):
                    self._offset = update["update_id"] + 1
                    if self._inbound_handler:
                        try:
                            self._inbound_handler(self.config.id, update, {})
                        except Exception as e:
                            logger.error("Poll handler error: %s", e)
            except (requests.exceptions.Timeout,
                    requests.exceptions.ConnectionError,
                    ConnectionResetError,
                    OSError):
                # Network hiccups (timeout, reset, DNS) — normal for long-polling, just retry
                self._poll_stop.wait(3)
                continue
            except Exception as e:
                logger.warning("Telegram poll error (will retry): %s", e)
                self._poll_stop.wait(5)

    def test_connection(self) -> dict:
        """Call getMe to verify the bot token is valid."""
        if not self.config.bot_token:
            return {"ok": False, "error": "No bot token configured"}
        try:
            resp = requests.get(
                f"https://api.telegram.org/bot{self.config.bot_token}/getMe",
                timeout=10,
            )
            data = resp.json()
            if data.get("ok"):
                bot = data["result"]
                return {"ok": True, "bot": bot.get("username", ""),
                        "name": bot.get("first_name", "")}
            return {"ok": False, "error": data.get("description", "unknown")}
        except Exception as e:
            return {"ok": False, "error": str(e)}


class DiscordAdapter(BaseAdapter):
    """Discord Bot adapter (webhook-based)."""

    def send(self, text: str, metadata: dict | None = None) -> bool:
        meta = metadata or {}
        # Use Discord webhook URL for simplicity
        webhook = self.config.webhook_url
        if not webhook:
            return False
        try:
            resp = requests.post(webhook, json={"content": text}, timeout=15)
            return resp.status_code in (200, 204)
        except Exception:
            return False

    def parse_inbound(self, payload: dict, headers: dict | None = None) -> ChannelMessage | None:
        # Discord Interaction / Gateway event
        if payload.get("type") == 1:  # PING
            return None
        content = payload.get("content", "") or payload.get("data", {}).get("content", "")
        if not content:
            return None
        author = payload.get("author", payload.get("member", {}).get("user", {}))
        return ChannelMessage(
            text=content,
            sender_id=str(author.get("id", "")),
            sender_name=author.get("username", ""),
            channel_id=str(payload.get("channel_id", "")),
            platform="discord",
            raw=payload,
        )


class DingTalkAdapter(BaseAdapter):
    """DingTalk (钉钉) Robot adapter."""

    def send(self, text: str, metadata: dict | None = None) -> bool:
        webhook = self.config.webhook_url
        if not webhook:
            return False
        # DingTalk custom robot webhook
        payload = {
            "msgtype": "text",
            "text": {"content": text},
        }
        try:
            # Sign if secret is set
            headers = {"Content-Type": "application/json"}
            if self.config.signing_secret:
                timestamp = str(int(time.time() * 1000))
                string_to_sign = f"{timestamp}\n{self.config.signing_secret}"
                hmac_code = hmac.new(
                    self.config.signing_secret.encode(),
                    string_to_sign.encode(), hashlib.sha256
                ).digest()
                import base64
                sign = base64.b64encode(hmac_code).decode()
                from urllib.parse import quote_plus
                url = f"{webhook}&timestamp={timestamp}&sign={quote_plus(sign)}"
            else:
                url = webhook
            resp = requests.post(url, headers=headers, json=payload, timeout=15)
            return resp.json().get("errcode", -1) == 0
        except Exception:
            return False

    def parse_inbound(self, payload: dict, headers: dict | None = None) -> ChannelMessage | None:
        # DingTalk outgoing robot webhook
        text = payload.get("text", {}).get("content", "").strip()
        if not text:
            return None
        sender = payload.get("senderNick", "")
        sender_id = payload.get("senderId", "")
        conversation_id = payload.get("conversationId", "")
        return ChannelMessage(
            text=text,
            sender_id=sender_id,
            sender_name=sender,
            channel_id=conversation_id,
            platform="dingtalk",
            raw=payload,
        )


class FeishuAdapter(BaseAdapter):
    """Feishu (飞书) / Lark Bot adapter."""

    def _get_tenant_token(self) -> str:
        """Get tenant access token for Feishu API."""
        if not self.config.app_id or not self.config.app_secret:
            return ""
        try:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.config.app_id, "app_secret": self.config.app_secret},
                timeout=10,
            )
            return resp.json().get("tenant_access_token", "")
        except Exception:
            return ""

    def send(self, text: str, metadata: dict | None = None) -> bool:
        meta = metadata or {}
        chat_id = meta.get("channel_id", "")
        if not chat_id:
            return False
        token = self._get_tenant_token()
        if not token:
            # Fallback to webhook
            if self.config.webhook_url:
                try:
                    resp = requests.post(self.config.webhook_url,
                                         json={"msg_type": "text",
                                               "content": {"text": text}},
                                         timeout=15)
                    return resp.json().get("code", -1) == 0
                except Exception:
                    return False
            return False
        try:
            resp = requests.post(
                "https://open.feishu.cn/open-apis/im/v1/messages",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=utf-8",
                },
                params={"receive_id_type": "chat_id"},
                json={
                    "receive_id": chat_id,
                    "msg_type": "text",
                    "content": json.dumps({"text": text}),
                },
                timeout=15,
            )
            return resp.json().get("code", -1) == 0
        except Exception:
            return False

    def parse_inbound(self, payload: dict, headers: dict | None = None) -> ChannelMessage | None:
        # Feishu Event v2.0
        if payload.get("type") == "url_verification":
            return None  # challenge
        event = payload.get("event", {})
        msg = event.get("message", {})
        content_str = msg.get("content", "{}")
        try:
            content = json.loads(content_str)
        except json.JSONDecodeError:
            content = {}
        text = content.get("text", "")
        if not text:
            return None
        sender = event.get("sender", {}).get("sender_id", {})
        return ChannelMessage(
            text=text,
            sender_id=sender.get("user_id", ""),
            sender_name=sender.get("open_id", ""),
            channel_id=msg.get("chat_id", ""),
            platform="feishu",
            raw=payload,
            reply_to=msg.get("message_id", ""),
        )


class WebhookAdapter(BaseAdapter):
    """Generic webhook adapter — simple JSON in/out."""

    def send(self, text: str, metadata: dict | None = None) -> bool:
        if not self.config.webhook_url:
            return False
        payload = {
            "text": text,
            "agent_id": self.config.agent_id,
            "timestamp": time.time(),
        }
        if metadata:
            payload["metadata"] = metadata
        try:
            headers = {"Content-Type": "application/json"}
            if self.config.bot_token:
                headers["Authorization"] = f"Bearer {self.config.bot_token}"
            resp = requests.post(self.config.webhook_url, headers=headers,
                                 json=payload, timeout=15)
            return resp.status_code < 400
        except Exception:
            return False

    def parse_inbound(self, payload: dict, headers: dict | None = None) -> ChannelMessage | None:
        text = payload.get("text", "") or payload.get("message", "")
        if not text:
            return None
        return ChannelMessage(
            text=text,
            sender_id=payload.get("sender_id", payload.get("user_id", "")),
            sender_name=payload.get("sender_name", payload.get("user_name", "")),
            channel_id=payload.get("channel_id", ""),
            platform="webhook",
            raw=payload,
        )

    def verify_signature(self, body: bytes, headers: dict) -> bool:
        if not self.config.signing_secret:
            return True
        sig = headers.get("X-Webhook-Signature", "")
        computed = hmac.new(
            self.config.signing_secret.encode(),
            body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(computed, sig)


# Adapter factory
_ADAPTERS: dict[ChannelType, type] = {
    ChannelType.SLACK: SlackAdapter,
    ChannelType.TELEGRAM: TelegramAdapter,
    ChannelType.DISCORD: DiscordAdapter,
    ChannelType.DINGTALK: DingTalkAdapter,
    ChannelType.FEISHU: FeishuAdapter,
    ChannelType.WEBHOOK: WebhookAdapter,
    ChannelType.WECHAT_WORK: WebhookAdapter,  # WeChat Work uses webhook too
}


def create_adapter(config: ChannelConfig) -> BaseAdapter:
    cls = _ADAPTERS.get(config.channel_type, WebhookAdapter)
    return cls(config)


# ---------------------------------------------------------------------------
# Channel Router — manages all channels and routes messages
# ---------------------------------------------------------------------------

class ChannelRouter:
    """Central router: receives inbound messages, routes to agents, sends replies."""

    def __init__(self, data_dir: str = ""):
        self._channels: dict[str, ChannelConfig] = {}
        self._adapters: dict[str, BaseAdapter] = {}
        self._lock = threading.Lock()
        from . import DEFAULT_DATA_DIR
        self._data_dir = data_dir or DEFAULT_DATA_DIR
        self._file = os.path.join(self._data_dir, "channels.json")
        self._agent_chat_fn: Callable | None = None  # set by portal
        self._event_log: list[dict] = []
        self._load()

    def set_agent_chat_fn(self, fn: Callable):
        """Set the function to call when a message arrives for an agent.
        Signature: fn(agent_id: str, message: str) -> str
        """
        self._agent_chat_fn = fn
        # Now that we have a chat function, start polling for channels that need it
        self._start_pollers()

    def _start_pollers(self):
        """Start polling threads for channels configured in polling mode."""
        for ch_id, ch in self._channels.items():
            if not ch.enabled or ch.mode != "polling":
                continue
            adapter = self._adapters.get(ch_id)
            if adapter and adapter.supports_polling and not adapter.is_polling:
                adapter.start_polling(self.handle_inbound)

    def _stop_pollers(self):
        """Stop all polling threads."""
        for adapter in self._adapters.values():
            if isinstance(adapter, TelegramAdapter) and adapter.is_polling:
                adapter.stop_polling()

    # ---- Persistence ----

    def _get_db(self):
        try:
            from .infra.database import get_database
            return get_database()
        except Exception:
            return None

    def _load(self):
        db = self._get_db()
        if db and db.count("channels") > 0:
            try:
                for d in db.load_channels():
                    ch = ChannelConfig.from_dict(d)
                    self._channels[ch.id] = ch
                    self._adapters[ch.id] = create_adapter(ch)
                return
            except Exception:
                pass
        if not os.path.exists(self._file):
            return
        try:
            with open(self._file, "r", encoding="utf-8") as f:
                data = json.load(f)
            for d in data.get("channels", []):
                ch = ChannelConfig.from_dict(d)
                self._channels[ch.id] = ch
                self._adapters[ch.id] = create_adapter(ch)
        except Exception:
            pass

    def _save(self):
        os.makedirs(self._data_dir, exist_ok=True)
        db = self._get_db()
        if db:
            try:
                for ch in self._channels.values():
                    db.save_channel(ch.to_dict())
            except Exception:
                pass
        data = {"channels": [ch.to_dict() for ch in self._channels.values()]}
        try:
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---- CRUD ----

    def list_channels(self, agent_id: str = "") -> list[ChannelConfig]:
        with self._lock:
            channels = list(self._channels.values())
        if agent_id:
            channels = [ch for ch in channels if ch.agent_id == agent_id]
        return channels

    def get_channel(self, channel_id: str) -> ChannelConfig | None:
        return self._channels.get(channel_id)

    def add_channel(self, **kwargs) -> ChannelConfig:
        if "channel_type" in kwargs and isinstance(kwargs["channel_type"], str):
            kwargs["channel_type"] = ChannelType(kwargs["channel_type"])
        ch = ChannelConfig(**kwargs)
        with self._lock:
            self._channels[ch.id] = ch
            self._adapters[ch.id] = create_adapter(ch)
            self._save()
        # Auto-start polling if mode=polling and chat function is ready
        if ch.mode == "polling" and ch.enabled and self._agent_chat_fn:
            adapter = self._adapters.get(ch.id)
            if adapter and adapter.supports_polling:
                adapter.start_polling(self.handle_inbound)
        return ch

    def update_channel(self, channel_id: str, **kwargs) -> ChannelConfig | None:
        with self._lock:
            ch = self._channels.get(channel_id)
            if not ch:
                return None
            # Stop old poller before recreating adapter
            old_adapter = self._adapters.get(ch.id)
            if old_adapter and old_adapter.is_polling:
                old_adapter.stop_polling()
            for k, v in kwargs.items():
                if k == "channel_type":
                    ch.channel_type = ChannelType(v) if isinstance(v, str) else v
                elif hasattr(ch, k):
                    setattr(ch, k, v)
            self._adapters[ch.id] = create_adapter(ch)
            self._save()
        # Restart polling if needed
        if ch.mode == "polling" and ch.enabled and self._agent_chat_fn:
            adapter = self._adapters.get(ch.id)
            if adapter and adapter.supports_polling:
                adapter.start_polling(self.handle_inbound)
        return ch

    def remove_channel(self, channel_id: str) -> bool:
        with self._lock:
            if channel_id in self._channels:
                # Stop poller
                adapter = self._adapters.get(channel_id)
                if adapter and adapter.is_polling:
                    adapter.stop_polling()
                del self._channels[channel_id]
                self._adapters.pop(channel_id, None)
                self._save()
                return True
        return False

    # ---- Message handling ----

    def handle_inbound(self, channel_id: str, payload: dict,
                       headers: dict | None = None) -> dict:
        """Process an inbound webhook payload for a specific channel.
        Returns {"ok": bool, "reply": str, ...}
        """
        ch = self._channels.get(channel_id)
        if not ch or not ch.enabled:
            return {"ok": False, "error": "Channel not found or disabled"}

        adapter = self._adapters.get(channel_id)
        if not adapter:
            return {"ok": False, "error": "No adapter"}

        # Parse message
        msg = adapter.parse_inbound(payload, headers)
        if not msg:
            # Could be a challenge or ping
            if payload.get("type") == "url_verification":
                return {"ok": True, "challenge": payload.get("challenge", "")}
            return {"ok": True, "skipped": True}

        # Access control
        if ch.allowed_users and msg.sender_id not in ch.allowed_users:
            return {"ok": False, "error": "User not allowed"}
        if ch.allowed_channels and msg.channel_id not in ch.allowed_channels:
            return {"ok": False, "error": "Channel not allowed"}

        # Log
        self._log_event("inbound", ch, msg)

        # Route to agent
        reply = ""
        if ch.agent_id and self._agent_chat_fn:
            try:
                # Phase 2 P2-1 (2026-05-06): channels are user-facing
                # one-shot ("ask once, get answer"). Use auto-continue
                # so the per-response cap doesn't surface intermediate
                # status JSONs to end users; render_status_for_user
                # also strips them from the final response if present.
                from .core.agent_runner import (
                    run_with_auto_continue, render_status_for_user,
                )
                reply = run_with_auto_continue(
                    chat_fn=lambda p: self._agent_chat_fn(ch.agent_id, p),
                    initial_prompt=msg.text,
                    max_rounds=4,
                    log_label=f"channel[{ch.channel_id[:8]}]",
                )
                reply = render_status_for_user(reply)
            except Exception as e:
                reply = f"Error: {e}"

        # Send reply back
        if reply:
            metadata = {
                "channel_id": msg.channel_id,
                "reply_to": msg.reply_to,
            }
            adapter.send(reply, metadata)
            self._log_event("outbound", ch, msg, reply=reply)

        return {"ok": True, "reply": reply}

    def send_to_channel(self, channel_id: str, text: str,
                        metadata: dict | None = None) -> bool:
        """Proactively send a message to a channel."""
        adapter = self._adapters.get(channel_id)
        if not adapter:
            return False
        return adapter.send(text, metadata)

    def _log_event(self, direction: str, ch: ChannelConfig,
                   msg: ChannelMessage, reply: str = ""):
        entry = {
            "direction": direction,
            "channel_id": ch.id,
            "channel_name": ch.name,
            "platform": ch.channel_type.value,
            "agent_id": ch.agent_id,
            "sender": msg.sender_name or msg.sender_id,
            "text": msg.text[:200],
            "reply": reply[:200] if reply else "",
            "timestamp": time.time(),
        }
        self._event_log.append(entry)
        if len(self._event_log) > 2000:
            self._event_log = self._event_log[-1500:]

    def get_event_log(self, limit: int = 100) -> list[dict]:
        return self._event_log[-limit:]


# ---------------------------------------------------------------------------
# Module singleton
# ---------------------------------------------------------------------------

_router: ChannelRouter | None = None
_router_lock = threading.Lock()


def get_router() -> ChannelRouter:
    global _router
    if _router is None:
        with _router_lock:
            if _router is None:
                from . import DEFAULT_DATA_DIR
                _router = ChannelRouter(data_dir=DEFAULT_DATA_DIR)
    return _router


def init_router(data_dir: str = "") -> ChannelRouter:
    global _router
    with _router_lock:
        _router = ChannelRouter(data_dir=data_dir)
    return _router
