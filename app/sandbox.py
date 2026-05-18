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
    # MCP credential files — agents must not read these. ``mcp_configs.json``
    # contains (now-encrypted) env values; the master key file
    # ``.mcp_master_key`` would let an agent decrypt them. Both off-limits.
    # Matches read attempts via cat / less / head / tail / grep / wc / od.
    re.compile(r"\b(cat|less|more|head|tail|grep|wc|od|xxd|hexdump|"
                r"awk|sed|jq|python\s+-c\s+['\"]?import\s+json)\s+"
                r"[^|;]*(?:tudou_claw[/\\][^|;]*mcp_configs?\.json"
                r"|\.mcp_master_key)"),
    # Direct outbound SMTP from sandbox bash — closes the
    # 2026-04-29 self-sent-email loophole. Agent should NEVER use
    # mail / sendmail / msmtp / smtplib / curl-to-smtp to reach
    # external SMTP servers; teammates aren't email users.
    re.compile(r"\b(sendmail|msmtp|mailx?)\s+"),
    re.compile(r"\bcurl\s+[^|;]*\bsmtp(s)?://"),
    re.compile(r"\bpython3?\s+-c\s+['\"][^'\"]*\bsmtplib\b"),
]


class SandboxPolicy:
    """Per-execution sandbox policy."""

    __slots__ = ("root", "mode", "allow_list", "agent_id", "agent_name",
                 "allowed_dirs", "readonly_dirs")

    def __init__(self, root: str = "", mode: str = "",
                 allow_list: Optional[list[str]] = None,
                 agent_id: str = "", agent_name: str = "",
                 allowed_dirs: Optional[list[str]] = None,
                 readonly_dirs: Optional[list[str]] = None):
        self.root = self._resolve_root(root)
        self.mode = (mode or _DEFAULT_MODE).lower()
        if self.mode not in ("off", "command_only", "restricted", "strict"):
            self.mode = "restricted"
        self.allow_list = allow_list or []
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.allowed_dirs = [str(Path(d).expanduser().resolve()) for d in (allowed_dirs or [])]
        # Read-only allowed directories. Paths under these resolve OK
        # when ``check_path(..., for_write=False)`` is called (read /
        # list / cd) but are denied when ``for_write=True`` (write_file
        # / edit_file / shell redirects). 2026-04-30: introduced so
        # agents can read sibling skills' manifests as reference
        # without gaining the ability to mutate them.
        self.readonly_dirs = [str(Path(d).expanduser().resolve()) for d in (readonly_dirs or [])]

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

    def check_path(self, path: str, for_write: bool = False) -> tuple[bool, str]:
        """Return (ok, error_message). ok=True if path is inside the jail.

        ``for_write`` distinguishes read vs write operations. Paths under
        ``readonly_dirs`` are allowed when ``for_write=False`` (read /
        list / cd) but rejected when ``for_write=True`` (write_file /
        edit_file). Existing read/write callers default to ``False`` —
        write tools should pass ``True`` explicitly. Backward-compatible:
        a caller that doesn't pass ``for_write`` keeps the old behavior
        (treats every check as read-equivalent).
        """
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
        # Read-only allowed dirs — pass for read, reject for write.
        for ro in self.readonly_dirs:
            if res_str == ro or res_str.startswith(ro + os.sep):
                if for_write:
                    return (False,
                            f"Sandbox violation: '{path}' is in a read-only "
                            f"directory ('{ro}'). Read OK; write blocked. "
                            f"To modify it, request the operator to grant "
                            f"the corresponding skill / workspace.")
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

    def safe_path(self, path: str, for_write: bool = False) -> Path:
        """Resolve a path relative to the sandbox root. Raises on escape.

        See ``check_path`` for the ``for_write`` flag's semantics.
        """
        ok, err = self.check_path(path, for_write=for_write)
        if not ok:
            raise SandboxViolation(err)
        p = Path(path).expanduser()
        if not p.is_absolute() and self.mode not in ("off", "command_only"):
            p = self.root / p
        return p

    # ---------- command validation ----------

    _CD_PATTERN = re.compile(r'\bcd\s+([^\s;&|]+)')

    # System paths that bash NEEDS to execute (binaries, libs, common
    # dev tools). Reading these is OK; we only block reads outside
    # this set + the agent's jail. Curated CONSERVATIVELY — when in
    # doubt, leave it out and let the agent surface a sandbox error
    # asking the admin to add the path.
    #
    # Explicitly NOT included:
    #   /etc          — has /etc/passwd, /etc/shadow, /etc/sudoers
    #                   etc. that should never leak to an agent.
    #                   Tools that need DNS (/etc/hosts) usually go
    #                   through libc, not user-mode reads.
    #   /Users        — anything outside agent's workspace is private
    #                   user data; explicit allowed_dirs adds back per
    #                   agent
    #   /proc, /sys   — kernel info / other processes' info
    #   ~/Library     — user app preferences, cookies, keychain refs
    _SYSTEM_READ_ALLOW = frozenset({
        "/bin", "/sbin", "/usr/bin", "/usr/sbin", "/usr/local/bin",
        "/usr/local/sbin", "/opt/homebrew/bin", "/opt/homebrew/sbin",
        "/usr/lib", "/usr/local/lib", "/opt/homebrew/lib",
        "/usr/share", "/usr/local/share", "/opt/homebrew/share",
        "/Library/Frameworks", "/System/Library",
        "/dev/null", "/dev/random", "/dev/urandom", "/dev/stdin",
        "/dev/stdout", "/dev/stderr", "/dev/tty",
        "/tmp", "/private/tmp", "/var/folders",  # tmpfs
    })

    # Commands that take a file path as their (typically) FIRST positional
    # arg and READ it. Used by _check_path_args_against_jail to find paths
    # worth validating. Bash one-liners like ``cat`` / ``ls`` / ``find``
    # are the 95% case for honest agents accidentally escaping the jail
    # (e.g. ``ls /Users/pangwanchun/...`` to "see what's around").
    _FS_READ_COMMANDS = frozenset({
        "cat", "head", "tail", "less", "more", "wc", "od", "xxd",
        "hexdump", "strings", "file", "stat",
        "ls", "ll", "tree", "find", "fd",
        "grep", "rg", "ag", "ack", "egrep", "fgrep",
        "cp", "mv", "rsync", "scp",  # also write but source is read
        "diff", "cmp", "patch",
        "tar", "zip", "unzip", "gzip", "gunzip", "7z",
        "open",  # macOS open command
        "vim", "vi", "nano", "nvim", "emacs",  # editors
    })

    # Commands that execute a script — first arg is the script path.
    _SCRIPT_EXEC_COMMANDS = frozenset({
        "python", "python3", "python2", "py",
        "bash", "sh", "zsh", "fish",
        "ruby", "perl", "node", "deno", "lua",
        "java", "groovy", "scala",
    })

    @staticmethod
    def _strip_quotes(token: str) -> str:
        """Remove outer matching quotes from a shell token."""
        if len(token) >= 2 and token[0] == token[-1] and token[0] in "'\"":
            return token[1:-1]
        return token

    def _is_path_inside_jail(self, path_str: str) -> bool:
        """Return True if the absolute path is inside the agent's
        workspace OR any of allowed_dirs / readonly_dirs / system
        allow-list. Resolves symlinks defensively (best-effort)."""
        try:
            resolved = str(Path(path_str).expanduser().resolve(strict=False))
        except Exception:
            resolved = path_str
        root_str = str(self.root)
        if resolved == root_str or resolved.startswith(root_str + os.sep):
            return True
        for allowed in self.allowed_dirs:
            if resolved == allowed or resolved.startswith(allowed + os.sep):
                return True
        for ro in self.readonly_dirs:
            if resolved == ro or resolved.startswith(ro + os.sep):
                return True
        for sys_path in self._SYSTEM_READ_ALLOW:
            if resolved == sys_path or resolved.startswith(sys_path + os.sep):
                return True
        # Home directory's own dotfile dirs (e.g. ~/.tudou_claw) get
        # implicit read because agents pull config from here. But NOT
        # the rest of $HOME.
        home = str(Path.home())
        if resolved.startswith(home + "/.tudou_claw"):
            return True
        return False

    def _check_path_args_against_jail(self, command: str) -> tuple[bool, str]:
        """Scan the command for absolute paths used as args to known
        file-reading / file-writing commands. Reject if any escape
        the jail.

        This is a defense-in-depth check on top of cwd jailing — agents
        with restricted mode shouldn't be able to ``cat /etc/passwd`` or
        ``ls /Users/...`` even though those paths are absolute and bypass
        the cwd-relative resolution.

        Best-effort parsing — bash quoting is hard; we tokenize on
        whitespace + handle simple quoted strings. Edge cases (heredoc,
        process substitution, eval $(echo /etc/passwd)) are NOT caught.
        For those, the macOS sandbox-exec wrapper (Layer 2, planned)
        provides kernel-level protection.

        Returns (ok, error_msg). ok=True passes through.
        """
        # Split on shell separators that introduce new commands
        # (semicolon, pipe, &&, ||). Check each segment as if it were
        # a fresh command.
        for segment in re.split(r"\s*[;|&]+\s*", command):
            segment = segment.strip()
            if not segment:
                continue
            tokens = segment.split()
            if not tokens:
                continue
            cmd0 = os.path.basename(self._strip_quotes(tokens[0]))

            # Check redirect targets: > /path  >> /path  < /path  2> /path
            for m in re.finditer(
                    r"(?:^|\s)(?:\d?>>?|<)\s*([^\s;|&]+)", segment):
                tgt = self._strip_quotes(m.group(1))
                if tgt.startswith("/") and not self._is_path_inside_jail(tgt):
                    return (False,
                            f"Sandbox blocked: command redirects to "
                            f"'{tgt}' which is outside agent's "
                            f"workspace '{self.root}'. Use relative "
                            f"paths or write inside your workspace.")

            # Check command-specific path args
            check_args = False
            if cmd0 in self._FS_READ_COMMANDS:
                check_args = True
            elif cmd0 in self._SCRIPT_EXEC_COMMANDS:
                check_args = True

            if check_args:
                for tok in tokens[1:]:
                    raw = self._strip_quotes(tok)
                    # Only check absolute paths (relative ones land in
                    # workspace due to cwd already).
                    if not raw.startswith("/"):
                        continue
                    # Skip flag values like --output=/path/foo (we'd
                    # need to parse arg form — skip for now to reduce
                    # false positives). Bare -X /path style we still
                    # check (it's the arg form most commonly used to
                    # read files: `head -5 /path/foo`).
                    if raw.startswith("-"):
                        continue
                    if not self._is_path_inside_jail(raw):
                        return (False,
                                f"Sandbox blocked: '{cmd0}' tried to "
                                f"access '{raw}' which is outside "
                                f"agent's workspace '{self.root}'. "
                                f"Agents may only read/write inside "
                                f"their working directory + standard "
                                f"system paths (/usr, /bin, etc.).")
        return (True, "")

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
                # cd to read-only allowed dirs — fine, navigation only.
                if not inside:
                    for ro in self.readonly_dirs:
                        if res_str == ro or res_str.startswith(ro + os.sep):
                            inside = True
                            break
                if not inside:
                    return (False,
                            f"Sandbox blocked: 'cd {cd_target}' escapes "
                            f"workspace root '{self.root}'. "
                            f"Agents must stay inside their working directory.")

        # ── Path-escape check (2026-05-17) ─────────────────────────
        # Even in restricted mode, the OLD code only blocked `cd /xxx`
        # outside workspace — agent could still ``cat /etc/passwd`` or
        # ``ls /Users/...`` using absolute paths. Now scan ALL paths
        # that look like file args to known read/write commands and
        # reject if any escape the jail.
        # See _check_path_args_against_jail docstring for limits +
        # planned Layer 2 (macOS sandbox-exec) for kernel-enforced
        # protection against more sophisticated bypasses.
        if self.mode in ("restricted", "strict"):
            ok, err = self._check_path_args_against_jail(command)
            if not ok:
                return (False, err)

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
