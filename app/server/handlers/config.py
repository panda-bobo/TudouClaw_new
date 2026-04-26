"""


╔════════════════════════════════════════════════════════════════════════╗
║  ⚠️  DEPRECATED — LEGACY stdlib handler (extracted from                ║
║     portal_routes_post.py). Active only when TUDOU_USE_STDLIB=1 is     ║
║     set at launch. FastAPI (app/api/routers/*) is authoritative.       ║
║     Do NOT add new routes here.                                        ║
╚════════════════════════════════════════════════════════════════════════╝
Configuration and policy POST handlers.

Extracted from portal_routes_post.py — handles:
  - /api/portal/config
  - /api/portal/role-presets/update
  - /api/portal/role-presets/delete
  - /api/portal/policy
  - /api/portal/approve
"""
import json
import logging
from dataclasses import asdict
from pathlib import Path

from ... import llm
from ...agent import ROLE_PRESETS, AgentProfile
from ..portal_auth import get_client_ip

logger = logging.getLogger("tudou.portal")


# ---------------------------------------------------------------------------
# Role-preset persistence helpers
# ---------------------------------------------------------------------------

def _get_custom_presets_path() -> Path:
    """Return path to persisted custom role presets JSON."""
    home = Path.home() / ".tudou_claw"
    home.mkdir(parents=True, exist_ok=True)
    return home / "role_presets.json"


def _save_custom_role_presets():
    """Persist current ROLE_PRESETS to disk so they survive restarts."""
    from ...agent import ROLE_PRESETS, AgentProfile
    from dataclasses import is_dataclass

    out = {}
    for k, v in ROLE_PRESETS.items():
        entry = dict(v)
        # Convert any nested dataclass to dict. Includes `_v2_preset`
        # (RolePresetV2) injected by preset_to_legacy_dict — without this
        # branch json.dumps raises "Object of type RolePresetV2 is not JSON
        # serializable" and the whole save fails.
        for fk in list(entry.keys()):
            fv = entry[fk]
            if is_dataclass(fv):
                try:
                    entry[fk] = asdict(fv)
                except Exception:
                    entry.pop(fk, None)
        out[k] = entry
    try:
        fp = _get_custom_presets_path()
        fp.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.debug("Saved %d role presets to %s", len(out), fp)
    except Exception as e:
        logger.error("Failed to save role presets: %s", e)


def _load_custom_role_presets():
    """Load persisted role presets from disk, merging into ROLE_PRESETS."""
    from ...agent import ROLE_PRESETS, AgentProfile

    fp = _get_custom_presets_path()
    if not fp.exists():
        return
    try:
        data = json.loads(fp.read_text(encoding="utf-8"))
        for k, v in data.items():
            prof_data = v.get("profile", {})
            if isinstance(prof_data, dict):
                profile = AgentProfile(
                    personality=prof_data.get("personality", ""),
                    communication_style=prof_data.get("communication_style", ""),
                    expertise=prof_data.get("expertise", []),
                    skills=prof_data.get("skills", []),
                    allowed_tools=prof_data.get("allowed_tools") or None,
                    denied_tools=prof_data.get("denied_tools") or None,
                    auto_approve_tools=prof_data.get("auto_approve_tools") or None,
                )
            else:
                profile = prof_data
            ROLE_PRESETS[k] = {
                "name": v.get("name", k),
                "system_prompt": v.get("system_prompt", ""),
                "profile": profile,
            }
        logger.info("Loaded %d role presets from disk", len(data))
    except Exception as e:
        logger.error("Failed to load role presets: %s", e)


# Load custom presets on module import
_load_custom_role_presets()


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

def try_handle(handler, path: str, hub, body: dict, auth, actor_name: str, user_role: str) -> bool:
    """Handle configuration and policy POST endpoints.

    Returns True if the path was handled, False otherwise.
    """

    if path == "/api/portal/config":
        cfg = llm.get_config()
        for k in ("provider", "model", "ollama_url", "openai_base_url",
                   "openai_api_key", "claude_api_key",
                   "unsloth_base_url", "unsloth_api_key"):
            if k in body and body[k]:
                cfg[k] = body[k]
        # global_system_prompt: allow empty string so users can clear it.
        if "global_system_prompt" in body:
            val = body.get("global_system_prompt")
            if isinstance(val, str):
                cfg["global_system_prompt"] = val
        # Persist to disk so changes survive restart
        llm.save_config()
        auth.audit("update_config", actor=actor_name, role=user_role,
                   target="config", ip=get_client_ip(handler))
        handler._json({"ok": True})
        return True

    if path == "/api/portal/role-presets/update":
        # Create or update a role preset
        key = (body.get("key") or "").strip()
        if not key:
            handler._json({"error": "key required"}, 400)
            return True
        name = body.get("name", key)
        system_prompt = body.get("system_prompt", "")
        prof_data = body.get("profile", {})
        profile = AgentProfile(
            personality=prof_data.get("personality", ""),
            communication_style=prof_data.get("communication_style", ""),
            expertise=prof_data.get("expertise", []),
            skills=prof_data.get("skills", []),
            allowed_tools=prof_data.get("allowed_tools") or [],
            denied_tools=prof_data.get("denied_tools") or [],
            auto_approve_tools=prof_data.get("auto_approve_tools") or [],
        )
        ROLE_PRESETS[key] = {
            "name": name,
            "system_prompt": system_prompt,
            "profile": profile,
        }
        # Persist custom presets to disk
        _save_custom_role_presets()
        auth.audit("update_role_preset", actor=actor_name, role=user_role,
                   target=key, ip=get_client_ip(handler))
        handler._json({"ok": True})
        return True

    if path == "/api/portal/role-presets/delete":
        key = (body.get("key") or "").strip()
        if not key:
            handler._json({"error": "key required"}, 400)
            return True
        if key in ROLE_PRESETS:
            del ROLE_PRESETS[key]
            _save_custom_role_presets()
        auth.audit("delete_role_preset", actor=actor_name, role=user_role,
                   target=key, ip=get_client_ip(handler))
        handler._json({"ok": True})
        return True

    if path == "/api/portal/policy":
        auth.tool_policy.update_policy_config(body)
        auth.audit("update_policy", actor=actor_name, role=user_role,
                   target="tool_policy", ip=get_client_ip(handler))
        handler._json({"ok": True})
        return True

    if path == "/api/portal/approve":
        approval_id = body.get("approval_id", "")
        action = body.get("action", "")
        if not approval_id:
            handler._json({"ok": False, "error": "approval_id required"}, 400)
            return True
        ok = False
        scope = body.get("scope", "once")
        if action == "approve":
            ok = auth.tool_policy.approve(approval_id,
                                          decided_by=actor_name,
                                          scope=scope)
        elif action == "deny":
            ok = auth.tool_policy.deny(approval_id, decided_by=actor_name)
        else:
            handler._json({"ok": False, "error": f"unknown action: {action}"}, 400)
            return True
        auth.audit("approval_" + action, actor=actor_name,
                   role=user_role, target=approval_id,
                   ip=get_client_ip(handler), success=ok)
        if not ok:
            handler._json({"ok": False,
                           "error": "approval not found or already decided"}, 404)
            return True
        handler._json({"ok": True})
        return True

    return False
