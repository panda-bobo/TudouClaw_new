"""
TudouClaw AgentMail MCP Server — AgentMail API 邮件收发。

Runs as a stdio-based MCP server (JSON-RPC 2.0 over stdin/stdout).
Provides: send_email, read_email, list_inbox, download_attachment tools.

Usage:
    python -m app.mcp.builtins.agentmail
    python -m app.tudou_agentmail_mcp          # legacy shim

Environment variables:
    AGENTMAIL_API_KEY   — AgentMail API Key (required, starts with am_)
    AGENTMAIL_INBOX_ID  — Default inbox ID / email address (optional)
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
import os
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("tudou.agentmail_mcp")

# ---------------------------------------------------------------------------
# AgentMail client (lazy init)
# ---------------------------------------------------------------------------

_client = None


def _get_client():
    """Lazily initialize the AgentMail client."""
    global _client
    if _client is not None:
        return _client

    api_key = os.environ.get("AGENTMAIL_API_KEY", "")
    if not api_key:
        raise RuntimeError(
            "AGENTMAIL_API_KEY not set. "
            "Get your API key from https://www.agentmail.to"
        )

    try:
        from agentmail import AgentMail
    except ImportError:
        import subprocess
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "agentmail",
             "--break-system-packages", "-q"],
            stdout=subprocess.DEVNULL,
        )
        from agentmail import AgentMail

    _client = AgentMail(api_key=api_key)
    logger.info("AgentMail client initialized")
    return _client


def _default_inbox() -> str:
    """Return the configured default inbox ID."""
    return os.environ.get("AGENTMAIL_INBOX_ID", "")


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def tool_send_email(
    to: str | list[str],
    subject: str,
    body: str,
    html: str = "",
    cc: str | list[str] | None = None,
    bcc: str | list[str] | None = None,
    inbox_id: str = "",
    attachments: list[dict] | None = None,
) -> dict:
    """Send an email via AgentMail.

    Args:
        to: Recipient(s), single email or list.
        subject: Email subject.
        body: Plain text body.
        html: Optional HTML body.
        cc: CC recipient(s).
        bcc: BCC recipient(s).
        inbox_id: Sender inbox (default from env).
        attachments: List of {path} or {content, filename, content_type}.
            When 'path' is given, the file is read and base64-encoded automatically.
    """
    client = _get_client()
    iid = inbox_id or _default_inbox()
    if not iid:
        return {"error": "No inbox_id specified and AGENTMAIL_INBOX_ID not set"}

    # Normalize recipients to string
    if isinstance(to, list):
        to_str = ", ".join(to) if len(to) > 1 else to[0]
    else:
        to_str = str(to)

    # Build keyword arguments — SDK uses keyword-only params
    send_kwargs: dict[str, Any] = {
        "inbox_id": iid,
        "to": to_str,
        "subject": subject,
        "text": body,
    }
    if html:
        send_kwargs["html"] = html
    if cc:
        send_kwargs["cc"] = ", ".join(cc) if isinstance(cc, list) else cc
    if bcc:
        send_kwargs["bcc"] = ", ".join(bcc) if isinstance(bcc, list) else bcc

    # Process attachments — use SDK's SendAttachment if available
    if attachments:
        att_list = []
        # Try to import SDK's SendAttachment class
        _SendAttachment = None
        try:
            from agentmail.types import SendAttachment as _SendAttachment
        except ImportError:
            pass

        for att in attachments:
            # Be lenient: LLMs naturally pass ["path1.pptx", ...] rather
            # than [{"path": "path1.pptx"}, ...]. Previously the string
            # form was silently dropped (isinstance(dict) check below),
            # producing the nasty "attachments_count: 0" failure mode
            # where the email sent but without any files attached.
            # Normalize: string → {"path": string}.
            if isinstance(att, str):
                att = {"path": att}
            if not isinstance(att, dict):
                continue
            if att.get("path"):
                # Read file from disk and base64-encode
                fpath = att["path"]
                if not os.path.isfile(fpath):
                    logger.warning("Attachment file not found: %s", fpath)
                    continue
                with open(fpath, "rb") as f:
                    raw = f.read()
                fname = os.path.basename(fpath)
                ctype = (att.get("content_type")
                         or mimetypes.guess_type(fpath)[0]
                         or "application/octet-stream")
                encoded = base64.b64encode(raw).decode()
                if _SendAttachment:
                    att_list.append(_SendAttachment(
                        content=encoded, filename=fname, content_type=ctype,
                    ))
                else:
                    att_list.append({
                        "content": encoded,
                        "filename": fname,
                        "content_type": ctype,
                    })
            elif att.get("content"):
                # Already base64-encoded
                if _SendAttachment:
                    att_list.append(_SendAttachment(
                        content=att["content"],
                        filename=att.get("filename", "attachment"),
                        content_type=att.get("content_type", "application/octet-stream"),
                    ))
                else:
                    att_list.append({
                        "content": att["content"],
                        "filename": att.get("filename", "attachment"),
                        "content_type": att.get("content_type", "application/octet-stream"),
                    })
        if att_list:
            send_kwargs["attachments"] = att_list

    logger.info("send_email: inbox_id=%s, to=%s, subject=%s, attachments=%d",
                iid, to_str, subject, len(send_kwargs.get("attachments", [])))

    try:
        msg = client.inboxes.messages.send(**send_kwargs)
    except Exception as e:
        logger.error("AgentMail send failed: %s", e, exc_info=True)
        return {"error": f"AgentMail API error: {e}"}

    msg_id = getattr(msg, "message_id", "") or getattr(msg, "id", "") or ""
    logger.info("send_email OK: message_id=%s", msg_id)
    return {
        "message_id": msg_id,
        "subject": getattr(msg, "subject", subject),
        "sent_to": to_str,
        "attachments_count": len(send_kwargs.get("attachments", [])),
    }


def tool_read_email(
    inbox_id: str = "",
    limit: int = 10,
    labels: str = "",
) -> dict:
    """List recent messages in an inbox.

    Args:
        inbox_id: Inbox to read (default from env).
        limit: Max messages to return (default 10).
        labels: Comma-separated label filter (e.g. "inbox,unread").
    """
    client = _get_client()
    iid = inbox_id or _default_inbox()
    if not iid:
        return {"error": "No inbox_id specified and AGENTMAIL_INBOX_ID not set"}

    list_kwargs: dict[str, Any] = {"inbox_id": iid, "limit": min(limit, 50)}
    if labels:
        list_kwargs["labels"] = [l.strip() for l in labels.split(",") if l.strip()]

    try:
        result = client.inboxes.messages.list(**list_kwargs)
    except Exception as e:
        logger.error("AgentMail list failed: %s", e, exc_info=True)
        return {"error": f"AgentMail API error: {e}"}

    messages = []
    for m in (getattr(result, "messages", None) or []):
        messages.append({
            "message_id": getattr(m, "message_id", "") or getattr(m, "id", ""),
            "from": getattr(m, "from_", "") or getattr(m, "from_address", "") or getattr(m, "sender", ""),
            "subject": getattr(m, "subject", ""),
            "text": (getattr(m, "text", "") or "")[:500],
            "timestamp": str(getattr(m, "timestamp", "") or getattr(m, "created_at", "")),
            "attachments": [
                {"id": getattr(a, "id", ""), "filename": getattr(a, "filename", ""),
                 "content_type": getattr(a, "content_type", "")}
                for a in (getattr(m, "attachments", None) or [])
            ],
        })
    return {"inbox_id": iid, "count": len(messages), "messages": messages}


def tool_list_inbox() -> dict:
    """List all inboxes for the current API key."""
    client = _get_client()
    try:
        result = client.inboxes.list()
    except Exception as e:
        logger.error("AgentMail list_inbox failed: %s", e, exc_info=True)
        return {"error": f"AgentMail API error: {e}"}

    inboxes = []
    for ib in (getattr(result, "inboxes", None) or []):
        inboxes.append({
            "inbox_id": getattr(ib, "inbox_id", "") or getattr(ib, "id", ""),
            "display_name": getattr(ib, "display_name", "") or getattr(ib, "name", ""),
            "email": getattr(ib, "email", "") or getattr(ib, "address", ""),
        })
    return {"count": len(inboxes), "inboxes": inboxes}


def tool_download_attachment(
    inbox_id: str,
    message_id: str,
    attachment_id: str,
    output_path: str = "",
) -> dict:
    """Download an attachment from a message.

    Args:
        inbox_id: Inbox containing the message.
        message_id: Message ID.
        attachment_id: Attachment ID.
        output_path: Where to save (default: current dir / filename).
    """
    client = _get_client()
    try:
        att = client.inboxes.messages.get_attachment(
            inbox_id=inbox_id, message_id=message_id, attachment_id=attachment_id,
        )
    except Exception as e:
        logger.error("AgentMail get_attachment failed: %s", e, exc_info=True)
        return {"error": f"AgentMail API error: {e}"}
    fname = getattr(att, "filename", attachment_id)
    content = getattr(att, "content", None)
    if not content:
        return {"error": "No content in attachment"}

    if isinstance(content, str):
        raw = base64.b64decode(content)
    elif isinstance(content, bytes):
        raw = content
    else:
        raw = str(content).encode()

    out = output_path or fname
    with open(out, "wb") as f:
        f.write(raw)
    return {"saved": out, "size": len(raw), "filename": fname}


# ---------------------------------------------------------------------------
# MCP protocol
# ---------------------------------------------------------------------------

SERVER_INFO = {
    "name": "tudou-agentmail",
    "version": "1.0.0",
}

TOOLS_SCHEMA = [
    {
        "name": "send_email",
        "description": (
            "Send an email via AgentMail. Supports plain text, HTML, CC/BCC, "
            "and file attachments (provide file paths or base64 content)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "to": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "Recipient email(s)",
                },
                "subject": {"type": "string", "description": "Email subject"},
                "body": {"type": "string", "description": "Plain text body"},
                "html": {"type": "string", "description": "Optional HTML body"},
                "cc": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "CC recipient(s)",
                },
                "bcc": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                    "description": "BCC recipient(s)",
                },
                "inbox_id": {
                    "type": "string",
                    "description": "Sender inbox ID (default from env)",
                },
                "attachments": {
                    "type": "array",
                    "description": (
                        "Attachments. Accepts either form: "
                        "['/abs/file.pdf', '/abs/file2.png'] (list of paths), "
                        "OR [{path: '/abs/file.pdf'}] (list of dicts), "
                        "OR [{content: 'base64...', filename: 'f.pdf', content_type: 'application/pdf'}] (inline base64). "
                        "Mixing is OK. Do NOT just mention filenames in the body — "
                        "files that aren't in this array will not be attached."
                    ),
                    "items": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "object"},
                        ],
                    },
                },
            },
            "required": ["to", "subject", "body"],
        },
    },
    {
        "name": "read_email",
        "description": "List recent messages in an inbox.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "inbox_id": {"type": "string", "description": "Inbox ID (default from env)"},
                "limit": {"type": "integer", "description": "Max messages (default 10, max 50)"},
                "labels": {"type": "string", "description": "Comma-separated label filter"},
            },
        },
    },
    {
        "name": "list_inbox",
        "description": "List all available inboxes.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "download_attachment",
        "description": "Download an attachment from a message to a local file.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "inbox_id": {"type": "string"},
                "message_id": {"type": "string"},
                "attachment_id": {"type": "string"},
                "output_path": {"type": "string", "description": "Save path (default: filename)"},
            },
            "required": ["inbox_id", "message_id", "attachment_id"],
        },
    },
]


def _handle_request(req: dict) -> dict | None:
    """Handle a single JSON-RPC 2.0 request."""
    method = req.get("method", "")
    params = req.get("params", {})
    req_id = req.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": SERVER_INFO,
            },
        }

    elif method == "notifications/initialized":
        return None

    elif method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS_SCHEMA},
        }

    elif method == "tools/call":
        tool_name = params.get("name", "")
        args = params.get("arguments", {})
        try:
            if tool_name == "send_email":
                result = tool_send_email(**args)
            elif tool_name == "read_email":
                result = tool_read_email(**args)
            elif tool_name == "list_inbox":
                result = tool_list_inbox()
            elif tool_name == "download_attachment":
                result = tool_download_attachment(**args)
            else:
                return {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"},
                }

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}],
                },
            }
        except Exception as e:
            logger.exception("Tool %s failed", tool_name)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps({"error": str(e)})}],
                    "isError": True,
                },
            }

    elif method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

    else:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Unknown method: {method}"},
        }


def main():
    """Run MCP server on stdin/stdout."""
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(levelname)-5s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stderr,
    )
    logger.info("TudouClaw AgentMail MCP Server starting...")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            resp = {"jsonrpc": "2.0", "id": None,
                    "error": {"code": -32700, "message": "Parse error"}}
            sys.stdout.write(json.dumps(resp) + "\n")
            sys.stdout.flush()
            continue

        resp = _handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
