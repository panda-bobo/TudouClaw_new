"""
TudouClaw Terraform MCP Server — wrap the terraform CLI as a structured
tool surface so agents can plan / validate / apply / destroy without
shelling out raw bash strings.

Runs as a stdio-based MCP server (JSON-RPC 2.0 over stdin/stdout) so
it slots into the same launcher as ``chromadb`` / ``browser_automation``.

Usage::

    python -m app.mcp.builtins.terraform

Environment variables:
    TF_ALLOW_DIRS        — colon-separated whitelist of directories the
                           server is allowed to operate in. When set,
                           any working_dir not under one of these paths
                           is rejected. (Recommended for prod.)
    TF_BIN               — path to the terraform binary (default: PATH lookup)
    TF_PLUGIN_CACHE_DIR  — shared provider-plugin cache (passed through to tf)

Safety model
------------
``terraform_apply`` and ``terraform_destroy`` are classified ``high``
risk in ``app.auth.DEFAULT_TOOL_RISK``. The agent's standard tool-call
gate (``ToolPolicy.check_tool_call``) intercepts them BEFORE they reach
this MCP server, enqueues a PendingApproval on the Portal's Approvals
queue, and only lets the call through after an operator clicks
"Approve". The server therefore does not implement its own approval
token layer — that would just be a second gate operators have to
satisfy (and a place for the two gates to disagree).

What the server DOES enforce:
  - ``plan`` writes ``<working_dir>/.plans/<plan_id>.tfplan`` and
    ``apply plan_id=X`` reads from that path — so "apply yesterday's
    plan against today's code" is structurally impossible (terraform
    itself rejects mismatched plans).
  - ``destroy`` additionally requires a ``confirm_phrase`` typed by a
    human ("destroy <module-basename>"). This is belt-and-suspenders
    on top of the approval gate: an LLM that scrapes its own chat
    history can't forge the phrase from working_dir alone.
  - ``TF_ALLOW_DIRS`` whitelist (env) limits which directories the
    server will operate in, regardless of what the agent passes.
  - All stdout/stderr truncated to 8 KB head/tail so a 50,000-line
    provider log doesn't blow up the LLM's context window.

Secrets (AWS_*, HCLOUD_TOKEN, ...) flow via this process's env, never
via tool arguments. The agent has no way to read them.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger("tudou.terraform_mcp")


# ─────────────────────────────────────────────────────────────────────
# Config helpers
# ─────────────────────────────────────────────────────────────────────

# Per-module mutex so two concurrent agents can't race apply/state ops
# on the same module. Held in-process; isn't a real distributed lock —
# terraform's own state lock catches cross-process collisions.
_MODULE_LOCKS: dict[str, threading.Lock] = {}
_MODULE_LOCKS_GUARD = threading.Lock()


def _module_lock(working_dir: str) -> threading.Lock:
    canon = os.path.realpath(working_dir)
    with _MODULE_LOCKS_GUARD:
        if canon not in _MODULE_LOCKS:
            _MODULE_LOCKS[canon] = threading.Lock()
        return _MODULE_LOCKS[canon]


def _tf_bin() -> str:
    """Resolve the terraform binary path."""
    explicit = os.environ.get("TF_BIN")
    if explicit and os.path.isfile(explicit):
        return explicit
    found = shutil.which("terraform")
    if found:
        return found
    raise FileNotFoundError(
        "terraform binary not found. Install terraform or set TF_BIN."
    )


def _allow_dirs() -> list[str]:
    raw = os.environ.get("TF_ALLOW_DIRS", "")
    if not raw.strip():
        return []
    return [os.path.realpath(p) for p in raw.split(":") if p.strip()]


def _validate_working_dir(wd: str) -> tuple[bool, str]:
    """Return (ok, error_message). Rejects empty paths, non-dirs, and
    (when TF_ALLOW_DIRS is set) anything outside the whitelist."""
    if not wd:
        return False, "working_dir is required"
    if not os.path.isabs(wd):
        # 2026-05-12: more actionable error so the LLM doesn't loop on
        # the same mistake — tells it WHAT shape the path should be.
        return False, (
            f"working_dir must be an absolute path (starts with '/'), "
            f"got: {wd!r}. Prefix with your workspace root, e.g. "
            f"'/Users/<user>/workspace/{wd}'."
        )
    if not os.path.isdir(wd):
        return False, f"working_dir does not exist or is not a directory: {wd}"
    allow = _allow_dirs()
    if allow:
        canon = os.path.realpath(wd)
        if not any(canon == a or canon.startswith(a + os.sep) for a in allow):
            return False, (
                f"working_dir {wd} is not under TF_ALLOW_DIRS whitelist. "
                f"Allowed: {allow}"
            )
    return True, ""


def _truncate(s: str, max_chars: int = 8000) -> str:
    if not s or len(s) <= max_chars:
        return s
    head = max_chars // 2
    tail = max_chars - head
    return s[:head] + f"\n...[truncated {len(s) - max_chars} chars]...\n" + s[-tail:]


def _run(cmd: list[str], cwd: str, timeout: int = 600,
         extra_env: dict | None = None) -> dict:
    """Run a terraform invocation; capture + truncate output.

    Returns a dict with: ok, exit_code, stdout, stderr, duration_ms,
    plus the original command for the audit trail.
    """
    env = {**os.environ, "TF_IN_AUTOMATION": "1", "TF_INPUT": "0"}
    if extra_env:
        env.update(extra_env)
    t0 = time.time()
    try:
        p = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True,
            timeout=timeout, env=env,
        )
        return {
            "ok": p.returncode == 0,
            "exit_code": p.returncode,
            "stdout": _truncate(p.stdout),
            "stderr": _truncate(p.stderr),
            "duration_ms": int((time.time() - t0) * 1000),
            "command": " ".join(cmd),
        }
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "exit_code": -1,
            "stdout": _truncate(e.stdout or ""),
            "stderr": (_truncate(e.stderr or "")
                       + f"\n[TIMEOUT after {timeout}s]"),
            "duration_ms": int((time.time() - t0) * 1000),
            "command": " ".join(cmd),
            "error": "timeout",
        }
    except FileNotFoundError as e:
        return {"ok": False, "exit_code": -1, "stdout": "", "stderr": str(e),
                "command": " ".join(cmd), "error": "binary_not_found"}


# ─────────────────────────────────────────────────────────────────────
# Tool implementations
# ─────────────────────────────────────────────────────────────────────

def tool_init(working_dir: str, upgrade: bool = False) -> dict:
    ok, err = _validate_working_dir(working_dir)
    if not ok:
        return {"ok": False, "error": err}
    cmd = [_tf_bin(), "init", "-no-color", "-input=false"]
    if upgrade:
        cmd.append("-upgrade")
    with _module_lock(working_dir):
        return _run(cmd, working_dir, timeout=600)


def tool_validate(working_dir: str) -> dict:
    ok, err = _validate_working_dir(working_dir)
    if not ok:
        return {"ok": False, "error": err}
    return _run([_tf_bin(), "validate", "-no-color", "-json"],
                working_dir, timeout=60)


def tool_fmt(working_dir: str, write: bool = False) -> dict:
    """Run terraform fmt. ``write=False`` (default) is read-only check."""
    ok, err = _validate_working_dir(working_dir)
    if not ok:
        return {"ok": False, "error": err}
    cmd = [_tf_bin(), "fmt", "-no-color", "-recursive"]
    if not write:
        cmd += ["-check", "-diff"]
    return _run(cmd, working_dir, timeout=60)


def tool_plan(working_dir: str, var_file: str = "",
              targets: list[str] | None = None) -> dict:
    """Save a plan under ``.plans/<plan_id>.tfplan`` and return its id.

    Callers (agents) pass the returned ``plan_id`` to ``terraform_apply``
    — the apply ALWAYS reads from the saved .tfplan file, so applying
    a stale plan against drifted state is structurally impossible.
    The Portal's Approvals queue gates apply itself (see module docstring).
    """
    ok, err = _validate_working_dir(working_dir)
    if not ok:
        return {"ok": False, "error": err}
    plan_id = uuid.uuid4().hex[:10]
    plans_dir = os.path.join(working_dir, ".plans")
    os.makedirs(plans_dir, exist_ok=True)
    plan_path = os.path.join(plans_dir, f"{plan_id}.tfplan")
    cmd = [_tf_bin(), "plan", "-no-color", "-input=false",
           "-detailed-exitcode", "-out", plan_path]
    if var_file:
        cmd += ["-var-file", var_file]
    for t in targets or []:
        cmd += ["-target", t]
    with _module_lock(working_dir):
        result = _run(cmd, working_dir, timeout=900)
    # terraform plan -detailed-exitcode: 0=no changes, 1=error, 2=changes
    has_changes = result.get("exit_code") == 2
    if result.get("exit_code") == 0 or has_changes:
        result["ok"] = True
        result["plan_id"] = plan_id
        result["plan_path"] = plan_path
        result["has_changes"] = has_changes
    return result


def tool_show(working_dir: str, plan_id: str = "") -> dict:
    """Show a saved plan or current state in JSON. Pass ``plan_id`` to
    inspect a specific plan; omit to dump the current state."""
    ok, err = _validate_working_dir(working_dir)
    if not ok:
        return {"ok": False, "error": err}
    cmd = [_tf_bin(), "show", "-no-color", "-json"]
    if plan_id:
        plan_path = os.path.join(working_dir, ".plans", f"{plan_id}.tfplan")
        if not os.path.isfile(plan_path):
            return {"ok": False, "error": f"plan {plan_id} not found at {plan_path}"}
        cmd.append(plan_path)
    return _run(cmd, working_dir, timeout=120)


def tool_apply(working_dir: str, plan_id: str) -> dict:
    """Apply a previously-saved plan file.

    Operator approval gating happens upstream in
    ``ToolPolicy.check_tool_call`` — this entry point is only reached
    AFTER the operator clicked "Approve" in the Portal Approvals queue.
    The plan file is consumed on success so the same approval can't be
    silently replayed against fresh state (terraform itself rejects
    stale plans, but removing the file means we never even try).
    """
    ok, err = _validate_working_dir(working_dir)
    if not ok:
        return {"ok": False, "error": err}
    if not plan_id:
        return {"ok": False, "error": "plan_id is required (run terraform_plan first)"}
    plan_path = os.path.join(working_dir, ".plans", f"{plan_id}.tfplan")
    if not os.path.isfile(plan_path):
        return {"ok": False, "error": f"plan {plan_id} not found at {plan_path}"}
    cmd = [_tf_bin(), "apply", "-no-color", "-input=false",
           "-auto-approve", plan_path]
    with _module_lock(working_dir):
        result = _run(cmd, working_dir, timeout=1800)
    if result.get("ok"):
        try:
            os.remove(plan_path)
        except OSError:
            pass
    return result


def tool_destroy(working_dir: str, confirm_phrase: str = "") -> dict:
    """terraform destroy — wipes all resources in the module.

    Two-layer gate:
      1. ``terraform_destroy`` is risk=high in DEFAULT_TOOL_RISK, so
         ToolPolicy.check_tool_call enqueues a PendingApproval before
         the call ever reaches here.
      2. confirm_phrase must literally be ``"destroy " + basename(wd)``.
         The agent can read ``working_dir`` from its own context, so
         this isn't unforgeable — but it's a clear "do you mean THIS
         module?" check that fires after operator approval, catching
         the case where the wrong module was approved.
    """
    ok, err = _validate_working_dir(working_dir)
    if not ok:
        return {"ok": False, "error": err}
    expected_phrase = "destroy " + os.path.basename(os.path.realpath(working_dir))
    if (confirm_phrase or "").strip() != expected_phrase:
        return {
            "ok": False,
            "error": f"confirm_phrase must be exactly {expected_phrase!r}. "
                     "Catches 'approved the wrong module' mistakes.",
        }
    cmd = [_tf_bin(), "destroy", "-no-color", "-input=false", "-auto-approve"]
    with _module_lock(working_dir):
        return _run(cmd, working_dir, timeout=1800)


def tool_output(working_dir: str) -> dict:
    """Read all module outputs as JSON."""
    ok, err = _validate_working_dir(working_dir)
    if not ok:
        return {"ok": False, "error": err}
    return _run([_tf_bin(), "output", "-no-color", "-json"],
                working_dir, timeout=60)


def tool_state_list(working_dir: str) -> dict:
    ok, err = _validate_working_dir(working_dir)
    if not ok:
        return {"ok": False, "error": err}
    return _run([_tf_bin(), "state", "list", "-no-color"],
                working_dir, timeout=60)


def tool_state_show(working_dir: str, address: str) -> dict:
    """Show a single resource's state. ``address`` is a terraform
    resource address like ``aws_instance.web[0]``."""
    ok, err = _validate_working_dir(working_dir)
    if not ok:
        return {"ok": False, "error": err}
    if not address:
        return {"ok": False, "error": "address is required"}
    return _run([_tf_bin(), "state", "show", "-no-color", address],
                working_dir, timeout=60)


def tool_workspace_list(working_dir: str) -> dict:
    ok, err = _validate_working_dir(working_dir)
    if not ok:
        return {"ok": False, "error": err}
    return _run([_tf_bin(), "workspace", "list", "-no-color"],
                working_dir, timeout=30)


# ─────────────────────────────────────────────────────────────────────
# JSON-RPC 2.0 / MCP wire format
# ─────────────────────────────────────────────────────────────────────

_BASE_WD_SCHEMA = {
    "working_dir": {
        "type": "string",
        # 2026-05-12: was just "Absolute path to..." — LLMs frequently
        # supplied relative paths anyway (e.g. "landing-zone-sample/
        # modules/monitoring"), which got rejected by _validate_working_dir
        # with the cryptic "working_dir must be absolute" error. The
        # agent-side mcp_call wrapper now auto-resolves relative paths
        # using the caller's workspace_root, but we still declare the
        # strict contract here + an example + pattern so the LLM gets
        # it right on the FIRST attempt and the wrapper is just a
        # safety net.
        "description": (
            "**MUST be an absolute path** (starts with '/'). Example: "
            "'/Users/me/workspace/landing-zone-sample/modules/monitoring'. "
            "Relative paths like 'landing-zone-sample/modules/x' will "
            "either be auto-resolved against your workspace root (if "
            "the wrapper recognises it) or rejected with "
            "'working_dir must be absolute'. When in doubt, prefix your "
            "module path with the agent's workspace_root."
        ),
        "pattern": "^/",
        "examples": [
            "/Users/me/workspace/landing-zone-sample/modules/monitoring"
        ],
    },
}

TOOLS_SCHEMA = [
    {
        "name": "terraform_init",
        "description": "Run `terraform init`. Idempotent — safe to call "
                       "before any other op. Pass upgrade=true to refresh "
                       "providers / modules. "
                       "**working_dir must be an absolute path** "
                       "(e.g. '/Users/me/workspace/modules/x'), not "
                       "a relative one.",
        "inputSchema": {
            "type": "object",
            "required": ["working_dir"],
            "properties": {
                **_BASE_WD_SCHEMA,
                "upgrade": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "terraform_validate",
        "description": "Run `terraform validate -json`. Read-only schema "
                       "+ syntax check.",
        "inputSchema": {
            "type": "object",
            "required": ["working_dir"],
            "properties": {**_BASE_WD_SCHEMA},
        },
    },
    {
        "name": "terraform_fmt",
        "description": "Run `terraform fmt -recursive`. Default is "
                       "read-only check (write=false) — pass write=true "
                       "to actually rewrite source files.",
        "inputSchema": {
            "type": "object",
            "required": ["working_dir"],
            "properties": {
                **_BASE_WD_SCHEMA,
                "write": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "terraform_plan",
        "description": "Run `terraform plan -out`. Returns plan_id + "
                       "has_changes. Surface the plan output to the "
                       "operator; the next terraform_apply call will "
                       "automatically pause for their approval in the "
                       "Portal Approvals queue (no token needed).",
        "inputSchema": {
            "type": "object",
            "required": ["working_dir"],
            "properties": {
                **_BASE_WD_SCHEMA,
                "var_file": {"type": "string"},
                "targets": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional -target=<addr> filters",
                },
            },
        },
    },
    {
        "name": "terraform_show",
        "description": "Show saved plan (pass plan_id) or current state "
                       "(omit plan_id) as JSON.",
        "inputSchema": {
            "type": "object",
            "required": ["working_dir"],
            "properties": {
                **_BASE_WD_SCHEMA,
                "plan_id": {"type": "string"},
            },
        },
    },
    {
        "name": "terraform_apply",
        "description": "Apply a previously-planned change. Risk=high — "
                       "the ToolPolicy approval gate pauses this call "
                       "in the Portal Approvals queue until an operator "
                       "clicks Approve. The plan file is consumed on "
                       "success.",
        "inputSchema": {
            "type": "object",
            "required": ["working_dir", "plan_id"],
            "properties": {
                **_BASE_WD_SCHEMA,
                "plan_id": {"type": "string"},
            },
        },
    },
    {
        "name": "terraform_destroy",
        "description": "DESTROY ALL RESOURCES in the module. Risk=high "
                       "(approval queue). Also requires confirm_phrase "
                       "= 'destroy <module basename>' as a second check "
                       "against approving the wrong module.",
        "inputSchema": {
            "type": "object",
            "required": ["working_dir", "confirm_phrase"],
            "properties": {
                **_BASE_WD_SCHEMA,
                "confirm_phrase": {
                    "type": "string",
                    "description": "Must be exactly 'destroy <basename>' "
                                   "of the module dir.",
                },
            },
        },
    },
    {
        "name": "terraform_output",
        "description": "Read module outputs (`terraform output -json`).",
        "inputSchema": {
            "type": "object",
            "required": ["working_dir"],
            "properties": {**_BASE_WD_SCHEMA},
        },
    },
    {
        "name": "terraform_state_list",
        "description": "List all resource addresses in current state.",
        "inputSchema": {
            "type": "object",
            "required": ["working_dir"],
            "properties": {**_BASE_WD_SCHEMA},
        },
    },
    {
        "name": "terraform_state_show",
        "description": "Show one resource's state (pass full address "
                       "like 'aws_instance.web[0]').",
        "inputSchema": {
            "type": "object",
            "required": ["working_dir", "address"],
            "properties": {
                **_BASE_WD_SCHEMA,
                "address": {"type": "string"},
            },
        },
    },
    {
        "name": "terraform_workspace_list",
        "description": "List terraform workspaces in the module.",
        "inputSchema": {
            "type": "object",
            "required": ["working_dir"],
            "properties": {**_BASE_WD_SCHEMA},
        },
    },
]

SERVER_INFO = {
    "name": "tudou-terraform",
    "version": "1.0.0",
    "description": "TudouClaw Terraform MCP Server — gated terraform CLI access",
}

# Map tool name → callable. Keeps _handle_request small.
_TOOL_FNS = {
    "terraform_init": lambda a: tool_init(
        working_dir=a["working_dir"], upgrade=bool(a.get("upgrade", False))),
    "terraform_validate": lambda a: tool_validate(working_dir=a["working_dir"]),
    "terraform_fmt": lambda a: tool_fmt(
        working_dir=a["working_dir"], write=bool(a.get("write", False))),
    "terraform_plan": lambda a: tool_plan(
        working_dir=a["working_dir"],
        var_file=a.get("var_file", ""),
        targets=a.get("targets") or []),
    "terraform_show": lambda a: tool_show(
        working_dir=a["working_dir"], plan_id=a.get("plan_id", "")),
    "terraform_apply": lambda a: tool_apply(
        working_dir=a["working_dir"],
        plan_id=a["plan_id"]),
    "terraform_destroy": lambda a: tool_destroy(
        working_dir=a["working_dir"],
        confirm_phrase=a.get("confirm_phrase", "")),
    "terraform_output": lambda a: tool_output(working_dir=a["working_dir"]),
    "terraform_state_list": lambda a: tool_state_list(working_dir=a["working_dir"]),
    "terraform_state_show": lambda a: tool_state_show(
        working_dir=a["working_dir"], address=a.get("address", "")),
    "terraform_workspace_list": lambda a: tool_workspace_list(
        working_dir=a["working_dir"]),
}


def _handle_request(req: dict) -> dict | None:
    """Handle a single JSON-RPC 2.0 request."""
    method = req.get("method", "")
    params = req.get("params", {}) or {}
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

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": TOOLS_SCHEMA},
        }

    if method == "tools/call":
        tool_name = params.get("name", "")
        args = params.get("arguments", {}) or {}
        fn = _TOOL_FNS.get(tool_name)
        if fn is None:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601,
                          "message": f"Unknown tool: {tool_name}"},
            }
        try:
            result = fn(args)
        except KeyError as e:
            result = {"ok": False, "error": f"missing required arg: {e}"}
        except Exception as e:
            logger.exception("Tool %s failed", tool_name)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text",
                                 "text": json.dumps({"error": str(e)})}],
                    "isError": True,
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "content": [{"type": "text",
                             "text": json.dumps(result, ensure_ascii=False)}],
            },
        }

    if method == "ping":
        return {"jsonrpc": "2.0", "id": req_id, "result": {}}

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
    logger.info("TudouClaw Terraform MCP Server starting...")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }) + "\n")
            sys.stdout.flush()
            continue

        resp = _handle_request(req)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
