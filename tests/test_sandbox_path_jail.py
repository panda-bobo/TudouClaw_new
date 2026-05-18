"""Tests for the Layer-1 path-escape check added to
``SandboxPolicy.check_command`` on 2026-05-17.

Background (@user "要让 agent cmd 只能看到自己的目录"): even in
``restricted`` mode, the previous check only blocked ``cd /xxx``
escapes. Absolute-path reads like ``cat /etc/passwd`` /
``ls /Users/pangwanchun/...`` slipped through because they don't
do a ``cd`` — they just access the absolute path directly. Agents
honest-mistake or curious would casually browse the host filesystem.

The new ``_check_path_args_against_jail`` parses command tokens and
rejects when:
  - A file-reading command (cat/ls/find/grep/...) has an absolute
    path arg outside the jail
  - A redirect target (> /path / >> /path / < /path) is outside
    the jail
  - A script-exec command (python/bash/...) tries to run a script
    outside the jail

Legitimate cases that MUST still pass:
  - Reading the agent's own workspace files (relative or absolute)
  - Running ``python script.py`` where script.py is in workspace
  - System binaries (/usr/bin/python3, /bin/bash) — these are exec'd
    but not READ as data; the path-arg check skips the command-name
    token (tokens[0])
  - Output to /tmp (in _SYSTEM_READ_ALLOW for temp scratch space)
  - Reading ~/.tudou_claw config dirs (allowed because that's where
    skills + memory + workspace data live)

The check is best-effort and doesn't catch every bypass — heredoc,
process substitution, eval, python -c "open(...)" — for those, Layer 2
(macOS sandbox-exec wrap) provides kernel-level protection. This file
locks the behavior of the Layer 1 honest-mistake catcher.
"""
from __future__ import annotations
import os
import tempfile
import pytest


@pytest.fixture
def policy():
    """SandboxPolicy with restricted mode rooted at a temp workspace."""
    from app.sandbox import SandboxPolicy
    with tempfile.TemporaryDirectory(prefix="agent_test_jail_") as tmp:
        p = SandboxPolicy(
            root=tmp,
            mode="restricted",
        )
        yield p


# ─────────────────────────────────────────────────────────────────
# Escapes that MUST be blocked
# ─────────────────────────────────────────────────────────────────

def test_cat_etc_passwd_blocked(policy):
    ok, err = policy.check_command("cat /etc/passwd")
    assert not ok, "cat of /etc/passwd must be blocked"
    assert "outside" in err.lower() or "blocked" in err.lower()


def test_ls_arbitrary_user_dir_blocked(policy):
    ok, err = policy.check_command("ls /Users/somebody/secrets")
    assert not ok, "ls outside workspace must be blocked"


def test_find_root_blocked(policy):
    ok, err = policy.check_command("find / -name foo")
    assert not ok, "find on / must be blocked"


def test_grep_recursive_outside_blocked(policy):
    ok, err = policy.check_command("grep -r pattern /Users/other")
    assert not ok


def test_redirect_outside_blocked(policy):
    ok, err = policy.check_command("echo hello > /tmp/../etc/foo")
    # Resolves to /etc/foo which is outside — should block
    assert not ok


def test_redirect_to_home_blocked(policy):
    ok, err = policy.check_command(
        "echo hello > /Users/pangwanchun/Documents/leak.md")
    assert not ok


def test_script_exec_outside_blocked(policy):
    ok, err = policy.check_command(
        "python /Users/pangwanchun/private/secret.py")
    assert not ok


def test_cp_source_outside_blocked(policy):
    # cp is in _FS_READ_COMMANDS because its first arg is a read
    ok, err = policy.check_command(
        "cp /etc/passwd ./my_copy.txt")
    assert not ok


# ─────────────────────────────────────────────────────────────────
# Legitimate operations that MUST pass
# ─────────────────────────────────────────────────────────────────

def test_relative_path_inside_workspace_ok(policy):
    ok, err = policy.check_command("cat README.md")
    assert ok, err
    ok, err = policy.check_command("ls .")
    assert ok, err


def test_absolute_path_inside_workspace_ok(policy):
    p = os.path.join(str(policy.root), "myfile.txt")
    ok, err = policy.check_command(f"cat {p}")
    assert ok, err


def test_system_bin_paths_ok(policy):
    # Reading from /usr/bin / /bin is fine — that's where tools live
    ok, err = policy.check_command("ls /usr/bin")
    assert ok, err
    ok, err = policy.check_command("which python3")  # no path arg
    assert ok, err


def test_etc_is_NOT_allowed_by_default(policy):
    """Explicit guard: /etc must NOT be in the allow list, otherwise
    /etc/passwd / /etc/shadow / /etc/sudoers leak. Admin can add it
    via per-agent allowed_dirs if a specific scenario needs it."""
    ok, err = policy.check_command("cat /etc/hosts")
    assert not ok, "/etc must not be allowed by default — sensitive"


def test_tmp_redirect_ok(policy):
    ok, err = policy.check_command("echo hi > /tmp/scratch.txt")
    assert ok, err


def test_tudou_claw_config_dir_ok(policy):
    # ~/.tudou_claw is implicitly allowed
    home = os.path.expanduser("~")
    ok, err = policy.check_command(
        f"cat {home}/.tudou_claw/some_config.json")
    assert ok, err


def test_python_with_relative_script_ok(policy):
    ok, err = policy.check_command("python ./build.py")
    assert ok, err


def test_command_with_pipes_ok(policy):
    # Each segment validated separately; both reading workspace files
    p = os.path.join(str(policy.root), "data.txt")
    ok, err = policy.check_command(f"cat {p} | head -5")
    assert ok, err


def test_command_with_pipes_one_segment_blocked(policy):
    # ANY segment outside jail blocks the whole pipe
    ok, err = policy.check_command("cat /etc/passwd | head -5")
    assert not ok


# ─────────────────────────────────────────────────────────────────
# Mode behavior
# ─────────────────────────────────────────────────────────────────

def test_command_only_mode_does_not_check_paths():
    """command_only mode = blacklist only, no path jailing — by design."""
    from app.sandbox import SandboxPolicy
    p = SandboxPolicy(root="/tmp/jail", mode="command_only")
    ok, err = p.check_command("cat /etc/passwd")
    assert ok, "command_only mode should pass — only blacklist enforced"


def test_off_mode_passes_everything():
    from app.sandbox import SandboxPolicy
    p = SandboxPolicy(root="/tmp/jail", mode="off")
    ok, err = p.check_command("cat /etc/passwd")
    assert ok


def test_blacklist_still_enforced_under_restricted(policy):
    """rm -rf / and friends still blocked regardless of path."""
    ok, err = policy.check_command("rm -rf /")
    assert not ok
    ok, err = policy.check_command("rm -rf /tmp/anything")
    assert not ok  # rm blanket-blocked
