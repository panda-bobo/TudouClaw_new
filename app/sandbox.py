"""
Sandbox — constrain tool execution to a safe environment.

Two layers of protection:

1. Filesystem jail: all file paths accessed by tools (read/write/edit/glob/
   search) must resolve to a path *inside* the sandbox root. Symlink
   escapes are blocked by resolving paths before checking. The sandbox
   root defaults to the agent's working_dir and falls back to
   ~/.tudou_claw/workspaces/{agent_id}/sandbox.

2. Command filtering: `bash` commands are matched against a blacklist of
   destructive patterns (rm -rf /, mkfs, dd of=/dev/*, fork bombs,
   reboot/shutdown, chmod 777 -R /, etc.). Blacklisted commands are
   rejected BEFORE execution. Commands also run with a scrubbed
   environment (no credentials leaking).

Modes (controlled via TUDOU_SANDBOX env var or per-agent profile):
  - "off"           : no sandboxing (legacy behaviour, not recommended)
  - "command_only"  : bash blacklist only (no path jail) — default for
                      non-agent callers, so direct tool use still blocks
                      dangerous shell commands
  - "restricted"    : filesystem jail + command blacklist (agent default)
  - "strict"        : restricted + bash requires command allowlist match

This module is intentionally dependency-free so it can be imported
anywhere without circular issues.
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Sandbox policy
# ---------------------------------------------------------------------------

_DEFAULT_MODE = os.environ.get("TUDOU_SANDBOX", "restricted").lower()
if _DEFAULT_MODE not in ("off", "command_only", "restricted", "strict"):
    _DEFAULT_MODE = "restricted"


# Patterns of dangerous commands that are always blocked in restricted/strict.
# Matched case-insensitively against the whole command string.
_BLACKLIST_PATTERNS: list[re.Pattern] = [
    # ---- DELETE / REMOVE operations (all forms) ----
    # Any rm command (agents are NEVER allowed to delete files)
    re.compile(r"\brm\s"),
    re.compile(r"\brm$"),
    # rmdir, unlink, shred
    re.compile(r"\b(rmdir|unlink|shred)\b"),
    # Python one-liners that delete: os.remove, os.unlink, shutil.rmtree, pathlib.unlink
    re.compile(r"\bos\.(remove|unlink)\b"),
    re.compile(r"\bshutil\.(rmtree|move)\b"),
    re.compile(r"\.unlink\("),
    re.compile(r"\.rmdir\("),
    # find ... -delete
    re.compile(r"\bfind\b.*-delete\b"),
    re.compile(r"\bfind\b.*-exec\s+rm\b"),
    # Trash / move to /dev/null
    re.compile(r">\s*/dev/null\s*2>&1\s*$"),

    # ---- Filesystem-destructive ----
    re.compile(r"\bmkfs(\.|\s)"),
    re.compile(r"\bdd\s+.*of=/dev/"),
    re.compile(r"\b(fdisk|parted|wipefs)\b"),
    # System control
    re.compile(r"\b(shutdown|reboot|halt|poweroff|init\s+0|init\s+6)\b"),
    # Fork bomb
    re.compile(r":\(\)\s*\{\s*:\|:&\s*\}\s*;\s*:"),
    # Chmod/chown wide
    re.compile(r"\bchmod\s+(-R\s+)?[0-7]{3,4}\s+/(\s|$)"),
    re.compile(r"\bchown\s+(-R\s+)?\S+\s+/(\s|$)"),
    # Pipe-to-shell installs (credential theft vector)
    re.compile(r"\bcurl\s+[^|]*\|\s*(sudo\s+)?(ba)?sh\b"),
    re.compile(r"\bwget\s+[^|]*\|\s*(sudo\s+)?(ba)?sh\b"),
    # Write to raw devices
    re.compile(r">\s*/dev/(sd[a-z]|nvme|hd[a-z]|xvd)"),
    # History / cred exfil
    re.compile(r"\bcat\s+.*\.ssh/id_"),
    re.compile(r"\bcat\s+.*\.aws/credentials"),
    re.compile(r"\bcat\s+.*\.env(\s|$)"),
]


class SandboxPolicy:
    """Per-execution sandbox policy."""

    __slots__ = ("root", "mode", "allow_list", "agent_id", "agent_name", "allowed_dirs")

    def __init__(self, root: str = "", mode: str = "",
                 allow_list: Optional[list[str]] = None,
                 agent_id: str = "", agent_name: str = "",
                 allowed_dirs: Optional[list[str]] = None):
        self.root = self._resolve_root(root)
        self.mode = (mode or _DEFAULT_MODE).lower()
        if self.mode not in ("off", "command_only", "restricted", "strict"):
            self.mode = "restricted"
        self.allow_list = allow_list or []
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.allowed_dirs = [str(Path(d).expanduser().resolve()) for d in (allowed_dirs or [])]

    @staticmethod
    def _resolve_root(root: str) -> Path:
        if root:
            p = Path(root).expanduser()
        else:
            p = Path.cwd()
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        try:
            return p.resolve()
        except Exception:
            return p

    # ---------- path validation ----------

    def check_path(self, path: str) -> tuple[bool, str]:
        """Return (ok, error_message). ok=True if path is inside the jail."""
        if self.mode in ("off", "command_only"):
            return (True, "")
        if not path:
            return (False, "Empty path")
        try:
            p = Path(path).expanduser()
            if not p.is_absolute():
                p = self.root / p
            # Resolve to defeat symlink traversal. strict=False so
            # non-existent paths still resolve (for write_file).
            resolved = p.resolve(strict=False)
        except Exception as e:
            return (False, f"Path resolution failed: {e}")

        root_str = str(self.root)
        res_str = str(resolved)
        if res_str == root_str or res_str.startswith(root_str + os.sep):
            return (True, "")
        # Check additional allowed directories (authorized workspaces)
        for allowed in self.allowed_dirs:
            if res_str == allowed or res_str.startswith(allowed + os.sep):
                return (True, "")
        # Compute a hint: what relative path the caller probably meant.
        # If they passed an absolute path that starts with '/' but a same-
        # named file would land under root, suggest the relative form.
        try:
            basename = os.path.basename(path) or path
        except Exception:
            basename = path
        hint = ""
        if path.startswith("/") or path.startswith("\\"):
            hint = (f" Did you mean a relative path? "
                    f"Try '{basename}' (lands in workspace root) "
                    f"or 'workspace/{basename}' instead of '{path}'.")
        return (False,
                f"Sandbox violation: '{path}' escapes jail root '{self.root}'. "
                f"All file access must stay inside the agent's working directory "
                f"or authorized workspaces.{hint}")

    def safe_path(self, path: str) -> Path:
        """Resolve a path relative to the sandbox root. Raises on escape."""
        ok, err = self.check_path(path)
        if not ok:
            raise SandboxViolation(err)
        p = Path(path).expanduser()
        if not p.is_absolute() and self.mode not in ("off", "command_only"):
            p = self.root / p
        return p

    # ---------- command validation ----------

    _CD_PATTERN = re.compile(r'\bcd\s+([^\s;&|]+)')

    def check_command(self, command: str) -> tuple[bool, str]:
        """Return (ok, error_message). ok=True if command is safe to run."""
        if self.mode == "off":
            return (True, "")
        if not command or not command.strip():
            return (False, "Empty command")

        cmd_lower = command.lower()
        for pat in _BLACKLIST_PATTERNS:
            if pat.search(cmd_lower):
                return (False,
                        f"Sandbox blocked command: matches blacklist pattern "
                        f"'{pat.pattern}'. Dangerous operations must be "
                        f"performed manually outside the agent.")

        # Block `cd` to directories outside the workspace jail
        if self.mode in ("restricted", "strict"):
            for m in self._CD_PATTERN.finditer(command):
                cd_target = m.group(1).strip("'\"")
                target_path = Path(cd_target).expanduser()
                if not target_path.is_absolute():
                    target_path = self.root / target_path
                try:
                    resolved = target_path.resolve(strict=False)
                except Exception:
                    resolved = target_path
                root_str = str(self.root)
                res_str = str(resolved)
                inside = (res_str == root_str
                          or res_str.startswith(root_str + os.sep))
                if not inside:
                    for allowed in self.allowed_dirs:
                        if res_str == allowed or res_str.startswith(allowed + os.sep):
                            inside = True
                            break
                if not inside:
                    return (False,
                            f"Sandbox blocked: 'cd {cd_target}' escapes "
                            f"workspace root '{self.root}'. "
                            f"Agents must stay inside their working directory.")

        if self.mode == "strict" and self.allow_list:
            # In strict mode, first token of the command must be in allow_list
            first_token = command.strip().split()[0] if command.strip() else ""
            # Strip path prefix
            first_token = os.path.basename(first_token)
            if first_token not in self.allow_list:
                return (False,
                        f"Strict sandbox: command '{first_token}' not in "
                        f"allow_list={self.allow_list}")

        return (True, "")

    def scrub_env(self, env: Optional[dict] = None) -> dict:
        """Return an environment dict with sensitive credentials removed."""
        base = dict(env or os.environ)
        # Remove common credential env vars
        blocked_prefixes = ("AWS_", "AZURE_", "GCP_", "GOOGLE_APPLICATION_",
                            "GITHUB_TOKEN", "GH_TOKEN", "NPM_TOKEN",
                            "DOCKER_", "KUBE", "SSH_AUTH_SOCK")
        blocked_exact = {"SUDO_PASSWORD", "SUDO_ASKPASS",
                         "LD_PRELOAD", "LD_LIBRARY_PATH"}
        for k in list(base.keys()):
            if k in blocked_exact:
                base.pop(k, None)
                continue
            for prefix in blocked_prefixes:
                if k.startswith(prefix):
                    base.pop(k, None)
                    break
        # Preserve PATH, HOME, USER, LANG etc.
        base.setdefault("PATH", os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"))
        return base

    def describe(self) -> str:
        return (f"Sandbox(mode={self.mode}, root={self.root}, "
                f"agent={self.agent_name or self.agent_id or 'unknown'})")


class SandboxViolation(Exception):
    """Raised when a tool call tries to access resources outside the jail."""
    pass


# ---------------------------------------------------------------------------
# Current-policy registry (thread-local so concurrent agents don't clash)
# ---------------------------------------------------------------------------

_tls = threading.local()


def get_current_policy() -> SandboxPolicy:
    """Return the currently active sandbox policy for this thread.
    When no explicit policy was installed via sandbox_scope (e.g. direct
    calls from tests or internal code), returns a 'command-only' policy
    where the bash blacklist is still enforced (so destructive shell
    commands are never run), but file-path jailing is relaxed. Agent-
    initiated tool calls always enter a sandbox_scope first with a full
    jail rooted at the agent's working_dir."""
    pol = getattr(_tls, "policy", None)
    if pol is None:
        pol = SandboxPolicy(root=os.getcwd(), mode="command_only")
    return pol


def set_current_policy(policy: Optional[SandboxPolicy]) -> Optional[SandboxPolicy]:
    """Install a policy for this thread. Returns the previous policy."""
    prev = getattr(_tls, "policy", None)
    _tls.policy = policy
    return prev


class sandbox_scope:
    """Context manager that installs a SandboxPolicy for the current thread."""

    def __init__(self, policy: SandboxPolicy):
        self.policy = policy
        self._prev: Optional[SandboxPolicy] = None

    def __enter__(self) -> SandboxPolicy:
        self._prev = set_current_policy(self.policy)
        return self.policy

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        set_current_policy(self._prev)


def default_mode() -> str:
    return _DEFAULT_MODE
