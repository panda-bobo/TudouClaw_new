"""MCP (Model Context Protocol) router — catalog, node config, global MCPs."""
from __future__ import annotations

import ast
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Body

from ..deps.hub import get_hub
from ..deps.auth import CurrentUser, get_current_user

MAX_MCP_SOURCE_SIZE = 2 * 1024 * 1024  # 2 MB

logger = logging.getLogger("tudouclaw.api.mcp")

router = APIRouter(prefix="/api/portal/mcp", tags=["mcp"])


# ---------------------------------------------------------------------------
# MCP catalog and discovery
# ---------------------------------------------------------------------------

@router.get("/catalog")
async def get_mcp_catalog(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get MCP catalog — matches legacy portal_routes_get."""
    try:
        from ...mcp.manager import MCP_CATALOG
        return {"catalog": {k: v.to_dict() for k, v in MCP_CATALOG.items()}}
    except (ImportError, Exception) as e:
        return {"catalog": {}}


# ---------------------------------------------------------------------------
# Node-level MCP configuration
# ---------------------------------------------------------------------------

@router.get("/nodes")
async def list_mcp_node_configs(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List MCP configuration per node — matches legacy portal_routes_get."""
    try:
        from ...mcp.manager import get_mcp_manager
        mcp_mgr = get_mcp_manager()
        nodes_out = {}
        for nid, cfg in mcp_mgr.node_configs.items():
            nodes_out[nid] = cfg.to_dict()
        return {"nodes": nodes_out, "summary": mcp_mgr.list_all_node_mcps()}
    except (ImportError, Exception) as e:
        return {"nodes": {}, "summary": {}}


@router.get("/global")
async def get_global_mcps(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get globally available MCPs — matches legacy portal_routes_get."""
    try:
        from ...mcp.manager import get_mcp_manager
        mcp_mgr = get_mcp_manager()
        return {"global_mcps": {
            mid: cfg.to_dict()
            for mid, cfg in mcp_mgr.list_global_mcps().items()
        }}
    except (ImportError, Exception) as e:
        return {"global_mcps": {}}


@router.get("/node/{node_id}")
async def get_node_mcp_config(
    node_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get MCP configuration for a specific node.

    Sensitive env values (PASSWORD / TOKEN / API_KEY / ...) are
    replaced with the mask placeholder before returning so the admin
    browser never sees plaintext credentials. The form treats the
    mask as "unchanged" on save (see merge_unchanged_from_existing).
    """
    try:
        from ...mcp.manager import get_mcp_manager
        from ...mcp.secrets import mask_env_for_display
        mcp_mgr = get_mcp_manager()
        node_cfg = mcp_mgr.get_node_mcp_config(node_id)
        out = node_cfg.to_dict()
        # Walk available_mcps[*].env and mask sensitive values for display.
        for _mid, _mcfg in (out.get("available_mcps") or {}).items():
            if isinstance(_mcfg, dict) and isinstance(_mcfg.get("env"), dict):
                _mcfg["env"] = mask_env_for_display(_mcfg["env"])
        return out
    except (ImportError, Exception) as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Agent-level MCP configuration
# ---------------------------------------------------------------------------

@router.get("/node/{node_id}/agent/{agent_id}")
async def get_agent_effective_mcps(
    node_id: str,
    agent_id: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get effective MCPs for an agent — matches legacy portal_routes_get."""
    try:
        from ...mcp.manager import get_mcp_manager
        mcp_mgr = get_mcp_manager()
        mcps = mcp_mgr.get_agent_effective_mcps(node_id, agent_id)
        return {"mcps": [m.to_dict() for m in mcps]}
    except (ImportError, Exception) as e:
        return {"mcps": []}


# ---------------------------------------------------------------------------
# MCP recommendations
# ---------------------------------------------------------------------------

@router.get("/recommend/{role}")
async def get_mcp_recommendations(
    role: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Get recommended MCPs for a specific agent role — matches legacy."""
    try:
        from ...mcp.manager import get_mcp_manager
        mcp_mgr = get_mcp_manager()
        recs = mcp_mgr.resolve_mcp_for_role(role)
        return {"recommendations": [r.to_dict() for r in recs]}
    except (ImportError, Exception) as e:
        return {"recommendations": []}


# ---------------------------------------------------------------------------
# MCP source listing and inspection (admin)
# ---------------------------------------------------------------------------

def _mcp_source_resolver():
    """Return (app_dir, proj_root, resolver_fn) for locating MCP source files."""
    app_dir = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
    proj_root = os.path.normpath(os.path.join(app_dir, ".."))

    def _resolve(command_template: str):
        # Parse "python -m app.mcp.builtins.foo" style templates
        if not command_template:
            return None
        parts = command_template.split()
        for i, p in enumerate(parts):
            if p == "-m" and i + 1 < len(parts):
                dotted = parts[i + 1]
                fpath = os.path.join(proj_root, dotted.replace(".", os.sep) + ".py")
                return (dotted, fpath)
        return None

    return app_dir, proj_root, _resolve


@router.get("/source/list")
async def list_mcp_sources(
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """List editable MCP server source files (admin-only)."""
    if not user.is_super_admin:
        raise HTTPException(403, "admin only")
    try:
        from ...mcp.manager import MCP_CATALOG
        app_dir, proj_root, _resolve_mcp_source = _mcp_source_resolver()
        items: list[dict] = []
        for mcp_id, cap in MCP_CATALOG.items():
            ct = getattr(cap, "command_template", "") or ""
            info = _resolve_mcp_source(ct)
            if info is None:
                continue
            dotted, fpath = info
            if not os.path.isfile(fpath):
                continue
            try:
                st = os.stat(fpath)
                size = st.st_size
                mtime = st.st_mtime
            except OSError:
                size, mtime = 0, 0
            rel = os.path.relpath(fpath, proj_root)
            items.append({
                "mcp_id": mcp_id,
                "name": cap.name,
                "module": dotted,
                "rel_path": rel,
                "size": size,
                "mtime": mtime,
            })
        items.sort(key=lambda x: x["mcp_id"])
        return {"items": items}
    except ImportError:
        return {"items": []}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/source/{name}")
async def get_mcp_source(
    name: str,
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Read source file of a specific MCP server (admin-only)."""
    if not user.is_super_admin:
        raise HTTPException(403, "admin only")
    try:
        from ...mcp.manager import MCP_CATALOG
        cap = MCP_CATALOG.get(name)
        if cap is None:
            raise HTTPException(404, "unknown mcp_id")

        app_dir, _proj_root, _resolve_mcp_source = _mcp_source_resolver()
        info = _resolve_mcp_source(getattr(cap, "command_template", "") or "")
        if info is None:
            raise HTTPException(400, "mcp has no editable python source")

        _dotted, fpath = info
        fpath = os.path.normpath(fpath)
        # Path-traversal defense
        if not fpath.startswith(app_dir + os.sep):
            raise HTTPException(403, "forbidden path")
        if not os.path.isfile(fpath):
            raise HTTPException(404, "source file missing")

        with open(fpath, "r", encoding="utf-8") as f:
            text = f.read()
        return {"mcp_id": name, "source": text, "path": fpath}
    except HTTPException:
        raise
    except ImportError:
        raise HTTPException(404, "MCP catalog not available")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/source/{name}")
async def save_mcp_source(
    name: str,
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Write (save) the source file of a specific MCP server (admin-only)."""
    if not user.is_super_admin:
        raise HTTPException(403, "admin only")
    try:
        from ...mcp.manager import MCP_CATALOG
        cap = MCP_CATALOG.get(name)
        if cap is None:
            raise HTTPException(404, "unknown mcp_id")

        app_dir, _proj_root, _resolve_mcp_source = _mcp_source_resolver()
        info = _resolve_mcp_source(getattr(cap, "command_template", "") or "")
        if info is None:
            raise HTTPException(400, "mcp has no editable python source")

        _dotted, fpath = info
        fpath = os.path.normpath(fpath)
        if not fpath.startswith(app_dir + os.sep):
            raise HTTPException(403, "forbidden path")

        content = body.get("content")
        if not isinstance(content, str):
            raise HTTPException(400, "content (string) required")
        if len(content) > MAX_MCP_SOURCE_SIZE:
            raise HTTPException(413, "content too large (>2MB)")

        # Syntax-check before writing
        try:
            ast.parse(content)
        except SyntaxError as se:
            raise HTTPException(400, f"SyntaxError at line {se.lineno}: {se.msg}")

        # Atomic write: backup + tempfile rename
        import tempfile
        bak_path = fpath + ".bak"
        if os.path.isfile(fpath):
            try:
                import shutil
                shutil.copy2(fpath, bak_path)
            except Exception:
                pass

        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(fpath), suffix=".tmp", prefix=".mcp_")
        try:
            os.write(fd, content.encode("utf-8"))
            os.close(fd)
            os.replace(tmp, fpath)
        except Exception:
            os.close(fd) if not os.get_inheritable(fd) else None
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

        return {"ok": True, "mcp_id": name, "path": fpath, "size": len(content)}
    except HTTPException:
        raise
    except ImportError:
        raise HTTPException(404, "MCP catalog not available")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# MCP management (unified)
# ---------------------------------------------------------------------------

@router.post("/manage")
async def manage_mcps(
    body: dict = Body(...),
    hub=Depends(get_hub),
    user: CurrentUser = Depends(get_current_user),
):
    """Unified MCP management endpoint.

    Supported actions: add_mcp, remove_mcp, bind_agent, unbind_agent,
    generate_from_catalog, validate, add_global_mcp, remove_global_mcp,
    change_scope, test_connection, install, uninstall, enable, disable.
    """
    try:
        action = body.get("action", "")
        node_id = body.get("node_id", hub.node_id if hasattr(hub, "node_id") else "local")

        # --- Node-level MCP management (via mcp_manager) ---
        try:
            from ...mcp.manager import get_mcp_manager
            mcp_mgr = get_mcp_manager()
        except (ImportError, Exception):
            mcp_mgr = None

        if action == "add_mcp" and mcp_mgr:
            from ...agent import MCPServerConfig
            config = MCPServerConfig.from_dict(body.get("config", {}))
            result = mcp_mgr.add_mcp_to_node(node_id, config)
            return result.to_dict() if hasattr(result, "to_dict") else {"ok": True, "result": result}

        elif action == "remove_mcp" and mcp_mgr:
            mcp_id = body.get("mcp_id", "")
            ok = mcp_mgr.remove_mcp_from_node(node_id, mcp_id)
            return {"ok": ok}

        elif action == "bind_agent" and mcp_mgr:
            agent_id = body.get("agent_id", "")
            mcp_id = body.get("mcp_id", "")
            ok = mcp_mgr.bind_mcp_to_agent(node_id, agent_id, mcp_id)
            if ok:
                agent = hub.get_agent(agent_id) if hasattr(hub, "get_agent") else None
                if agent:
                    mcp_mgr.sync_agent_mcps(agent)
                    if hasattr(hub, "_save_agents"):
                        hub._save_agents()
            return {"ok": ok}

        elif action == "unbind_agent" and mcp_mgr:
            agent_id = body.get("agent_id", "")
            mcp_id = body.get("mcp_id", "")
            ok = mcp_mgr.unbind_mcp_from_agent(node_id, agent_id, mcp_id)
            if ok:
                agent = hub.get_agent(agent_id) if hasattr(hub, "get_agent") else None
                if agent:
                    mcp_mgr.sync_agent_mcps(agent)
                    if hasattr(hub, "_save_agents"):
                        hub._save_agents()
            return {"ok": ok}

        elif action == "generate_from_catalog" and mcp_mgr:
            capability_id = body.get("capability_id", "")
            env_values = body.get("env_values", {})
            config = mcp_mgr.generate_mcp_config(capability_id, env_values)
            if config:
                result = mcp_mgr.add_mcp_to_node(node_id, config)
                return result.to_dict() if hasattr(result, "to_dict") else {"ok": True}
            raise HTTPException(404, "Capability not found in catalog")

        elif action == "validate" and mcp_mgr:
            from ...agent import MCPServerConfig
            config = MCPServerConfig.from_dict(body.get("config", {}))
            valid, msg = mcp_mgr.validate_mcp_config(config)
            return {"valid": valid, "message": msg}

        elif action == "add_global_mcp" and mcp_mgr:
            from ...agent import MCPServerConfig
            raw = body.get("config", {}) or {}
            cap_id = body.get("capability_id", "")
            if cap_id and not raw:
                env_values = body.get("env_values", {}) or {}
                cfg = mcp_mgr.generate_mcp_config(cap_id, env_values)
                if cfg is None:
                    raise HTTPException(404, "capability not found")
            else:
                cfg = MCPServerConfig.from_dict(raw)
            result = mcp_mgr.add_global_mcp(cfg)
            return {"ok": True, "mcp": result.to_dict() if hasattr(result, "to_dict") else result}

        elif action == "remove_global_mcp" and mcp_mgr:
            mcp_id = body.get("mcp_id", "")
            ok = mcp_mgr.remove_global_mcp(mcp_id)
            return {"ok": ok}

        elif action == "change_scope" and mcp_mgr:
            mcp_id = body.get("mcp_id", "")
            new_scope = body.get("scope", "")
            target_nodes = body.get("target_nodes", None)
            result = mcp_mgr.change_mcp_scope(mcp_id, new_scope, target_nodes=target_nodes)
            return result

        elif action == "set_env" and mcp_mgr:
            # UI-driven env edit (the form behind GET /node/{id}).
            # Two flavors:
            #   * Node-scope: body has node_id + mcp_id + env (+ delete_keys?)
            #     → updates the MCP's primary `env` dict (what
            #       subprocess.Popen sees).
            #   * Agent-scope: same plus `agent_id`
            #     → updates agent_env_overrides instead (overlay applied
            #       on top of the MCP's env when this agent calls it).
            # Mask-value handling: any value still equal to the UI mask
            # placeholder ("••••••") means "admin didn't touch this
            # field" — keep the existing encrypted value rather than
            # nulling it out.
            mcp_id = (body.get("mcp_id") or "").strip()
            agent_id = (body.get("agent_id") or "").strip()
            new_env = body.get("env") or {}
            delete_keys = body.get("delete_keys") or []
            if not mcp_id:
                raise HTTPException(400, "mcp_id is required")
            try:
                from ...mcp.secrets import (
                    merge_unchanged_from_existing, MASK_PLACEHOLDER,
                )
            except Exception:
                merge_unchanged_from_existing = None
                MASK_PLACEHOLDER = "••••••"

            node_cfg = mcp_mgr.get_node_mcp_config(node_id)
            mcp_cfg = (node_cfg.available_mcps or {}).get(mcp_id)
            if mcp_cfg is None:
                raise HTTPException(404, f"MCP not found on node {node_id}: {mcp_id}")

            if agent_id:
                # Agent-scope override — merge mask placeholders against
                # any existing agent override for this MCP.
                existing = ((node_cfg.agent_env_overrides or {})
                            .get(agent_id, {})
                            .get(mcp_id, {})) or {}
                if merge_unchanged_from_existing:
                    new_env = merge_unchanged_from_existing(new_env, existing)
                # Drop deleted keys
                for k in delete_keys or []:
                    new_env.pop(k, None)
                # Persist
                if hasattr(mcp_mgr, "set_agent_env_override"):
                    for k, v in new_env.items():
                        mcp_mgr.set_agent_env_override(agent_id, mcp_id, k, v)
                    for k in delete_keys or []:
                        mcp_mgr.set_agent_env_override(agent_id, mcp_id, k, None)
                else:
                    # Fallback: write directly + save
                    node_cfg.agent_env_overrides.setdefault(agent_id, {})[mcp_id] = new_env
                    mcp_mgr._save()
                return {"ok": True, "scope": "agent", "agent_id": agent_id, "mcp_id": mcp_id}

            # Node-scope (MCP env edit) — merge mask + delete + encrypt
            # at to_dict() time (the existing chain through to_persist).
            existing_env = dict(getattr(mcp_cfg, "env", None) or {})
            if merge_unchanged_from_existing:
                new_env = merge_unchanged_from_existing(new_env, existing_env)
            for k in delete_keys or []:
                new_env.pop(k, None)
            mcp_cfg.env = new_env
            mcp_mgr._save()
            mcp_mgr.invalidate_tool_manifest(mcp_id) if hasattr(mcp_mgr, "invalidate_tool_manifest") else None
            return {"ok": True, "scope": "node", "mcp_id": mcp_id,
                    "env_keys": sorted(new_env.keys())}

        elif action == "test_connection" and mcp_mgr:
            from ...agent import MCPServerConfig
            cap_id = body.get("capability_id", "")
            raw_cfg = body.get("config", {}) or {}
            if cap_id and not raw_cfg:
                config = mcp_mgr.generate_mcp_config(cap_id, body.get("env_values", {}))
                if config is None:
                    raise HTTPException(404, "capability not found")
            else:
                config = MCPServerConfig.from_dict(raw_cfg)
            if hasattr(mcp_mgr, "test_mcp_connection"):
                result = mcp_mgr.test_mcp_connection(config)
                # test_mcp_connection may return a dict or a (bool, str) tuple
                if isinstance(result, dict):
                    return result
                ok, msg = result
                return {"ok": ok, "message": msg}
            raise HTTPException(501, "MCP manager does not support test_connection")

        elif action == "install" and mcp_mgr:
            cap_id = body.get("capability_id", "") or body.get("mcp_id", "")
            env_values = body.get("env_values", {})
            result = mcp_mgr.install_mcp(node_id, cap_id, env_values)
            return result

        # --- Fallback: hub-level uninstall/enable/disable ---
        mcp_id = body.get("mcp_id", "")
        scope = body.get("scope", "global")

        if action == "install" and hasattr(hub, "install_mcp"):
            result = hub.install_mcp(mcp_id, scope, body)
            return {"ok": True, "result": result}
        elif action == "uninstall" and hasattr(hub, "uninstall_mcp"):
            result = hub.uninstall_mcp(mcp_id, scope, body)
            return {"ok": True, "result": result}
        elif action == "enable" and hasattr(hub, "enable_mcp"):
            result = hub.enable_mcp(mcp_id, scope, body)
            return {"ok": True, "result": result}
        elif action == "disable" and hasattr(hub, "disable_mcp"):
            result = hub.disable_mcp(mcp_id, scope, body)
            return {"ok": True, "result": result}
        else:
            raise HTTPException(400, f"Unknown action: {action}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
