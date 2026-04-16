"""
app.agent_worker_hooks — Python-API monkey-patches for worker jail.

Layer 1.5: 实际安装写操作拦截 hooks。

拦截以下写类 Python API，拒绝目标路径逃逸 jail root 的操作:
  - os.mkdir / os.makedirs / os.remove / os.unlink / os.rmdir / os.rename
  - shutil.copy / shutil.copy2 / shutil.move / shutil.rmtree / shutil.copytree
  - pathlib.Path.write_text / Path.write_bytes / Path.mkdir / Path.unlink / Path.rename
  - builtins.open (write/append/create modes)

设计原则:
  - 只拦截写类操作，读操作不受影响
  - 拦截失败 (hook 本身出 bug) 不阻止操作 —— fail-open，记录警告
  - 每次写操作都记录到 _audit_log，供事后审计
  - 支持动态更新 allowed_dirs (技能授权后自动扩展)

Trying to install hooks must never prevent the worker from booting:
any exception during install is swallowed and logged to stderr.
"""
from __future__ import annotations

import builtins
import logging
import os
import shutil
import sys
from pathlib import Path, PurePosixPath
from typing import Any

logger = logging.getLogger("tudou.worker_hooks")

# Kept module-level so the hooks can consult the live WorkerState
# for allowed_dirs after capability updates.
_state: Any = None

# Audit log: list of (timestamp, op, path, allowed)
_audit_log: list[tuple[float, str, str, bool]] = []

# Track original functions for clean uninstall
_originals: dict[str, Any] = {}

# Whether hooks are actively installed
_installed: bool = False


# ─────────────────────────────────────────────────────────────
# Core path checking
# ─────────────────────────────────────────────────────────────

def _jail_root() -> str:
    if _state is None:
        return os.environ.get("TUDOU_WORKER_ROOT", os.getcwd())
    return getattr(_state, "work_dir", os.getcwd())


def is_inside_jail(path: str | Path) -> bool:
    """Check if *path* resolves inside the worker's jail root
    OR inside any explicitly-allowed dir on the live state.
    """
    try:
        resolved = Path(path).expanduser().resolve(strict=False)
    except Exception:
        return False

    root = Path(_jail_root()).resolve()
    try:
        resolved.relative_to(root)
        return True
    except ValueError:
        pass

    if _state is None:
        return False

    # Check authorized workspaces
    for d in list(getattr(_state, "authorized_workspaces", []) or []):
        try:
            resolved.relative_to(Path(d).resolve())
            return True
        except ValueError:
            continue

    # Check shared workspace
    shared = getattr(_state, "shared_workspace", None)
    if shared:
        try:
            resolved.relative_to(Path(shared).resolve())
            return True
        except ValueError:
            pass

    # Check skill directories (dynamically added after grant)
    for d in list(getattr(_state, "skill_dirs", []) or []):
        try:
            resolved.relative_to(Path(d).resolve())
            return True
        except ValueError:
            continue

    return False


def _audit(op: str, path: str, allowed: bool) -> None:
    """Record a write-class operation for audit trail."""
    import time
    _audit_log.append((time.time(), op, path, allowed))
    # Keep last 1000 entries to avoid unbounded growth
    if len(_audit_log) > 1000:
        _audit_log[:] = _audit_log[-500:]


def _is_shared_path(path_str: str) -> bool:
    """Check if a path is inside the shared workspace (Phase 2).

    Writes to shared paths are allowed but audited at a higher level.
    When full uid isolation is active, shared writes may be routed
    through the Hub's SharedFileRouter via gate requests.
    """
    if _state is None:
        return False
    shared = getattr(_state, "shared_workspace", None)
    if not shared:
        return False
    try:
        resolved = Path(path_str).expanduser().resolve(strict=False)
        shared_resolved = Path(shared).resolve()
        return (str(resolved) == str(shared_resolved)
                or str(resolved).startswith(str(shared_resolved) + os.sep))
    except Exception:
        return False


def _check_write(op: str, path: str | Path) -> None:
    """Raise PermissionError if path escapes the jail.

    Fail-open on internal errors: if is_inside_jail itself raises,
    we log a warning but allow the operation.

    Phase 2: writes to shared workspace paths are allowed but
    audited with a "SHARED_WRITE" tag for tracking.
    """
    path_str = str(path)
    try:
        allowed = is_inside_jail(path_str)
    except Exception as exc:
        # Fail-open: log but don't block
        logger.warning("agent_worker_hooks: check failed for %s: %s (allowing)", op, exc)
        _audit(op, path_str, True)
        return

    # Phase 2: tag shared workspace writes for audit
    if allowed and _is_shared_path(path_str):
        agent_id = getattr(_state, "agent_id", "?") if _state else "?"
        _audit(f"SHARED:{op}", path_str, True)
        logger.debug("[SHARED_WRITE] agent=%s op=%s path=%s",
                    agent_id[:8] if agent_id else "?", op, path_str)

    _audit(op, path_str, allowed)
    if not allowed:
        raise PermissionError(
            f"[TudouClaw sandbox] {op} 被拦截: 路径 '{path_str}' "
            f"不在允许的目录内 (jail_root={_jail_root()})"
        )


# ─────────────────────────────────────────────────────────────
# Hooked functions
# ─────────────────────────────────────────────────────────────

# ── os module hooks ──

def _hooked_mkdir(path, mode=0o777, *, dir_fd=None):
    _check_write("os.mkdir", path)
    return _originals["os.mkdir"](path, mode, dir_fd=dir_fd)


def _hooked_makedirs(name, mode=0o777, exist_ok=False):
    _check_write("os.makedirs", name)
    return _originals["os.makedirs"](name, mode, exist_ok=exist_ok)


def _hooked_remove(path, *, dir_fd=None):
    _check_write("os.remove", path)
    return _originals["os.remove"](path, dir_fd=dir_fd)


def _hooked_unlink(path, *, dir_fd=None):
    _check_write("os.unlink", path)
    return _originals["os.unlink"](path, dir_fd=dir_fd)


def _hooked_rmdir(path, *, dir_fd=None):
    _check_write("os.rmdir", path)
    return _originals["os.rmdir"](path, dir_fd=dir_fd)


def _hooked_rename(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
    _check_write("os.rename.src", src)
    _check_write("os.rename.dst", dst)
    return _originals["os.rename"](src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)


# ── shutil module hooks ──

def _hooked_shutil_copy(src, dst, **kwargs):
    _check_write("shutil.copy", dst)
    return _originals["shutil.copy"](src, dst, **kwargs)


def _hooked_shutil_copy2(src, dst, **kwargs):
    _check_write("shutil.copy2", dst)
    return _originals["shutil.copy2"](src, dst, **kwargs)


def _hooked_shutil_move(src, dst, **kwargs):
    _check_write("shutil.move.src", src)
    _check_write("shutil.move.dst", dst)
    return _originals["shutil.move"](src, dst, **kwargs)


def _hooked_shutil_rmtree(path, *args, **kwargs):
    _check_write("shutil.rmtree", path)
    return _originals["shutil.rmtree"](path, *args, **kwargs)


def _hooked_shutil_copytree(src, dst, *args, **kwargs):
    _check_write("shutil.copytree", dst)
    return _originals["shutil.copytree"](src, dst, *args, **kwargs)


# ── builtins.open hook ──

_WRITE_MODES = frozenset({"w", "a", "x", "w+", "a+", "r+", "wb", "ab", "xb",
                           "w+b", "a+b", "r+b", "wt", "at", "xt", "w+t",
                           "a+t", "r+t"})


def _hooked_open(file, mode="r", *args, **kwargs):
    # Only intercept write modes
    if isinstance(mode, str) and mode in _WRITE_MODES:
        _check_write("open", file)
    return _originals["builtins.open"](file, mode, *args, **kwargs)


# ── pathlib.Path method hooks ──
# pathlib is trickier: we monkey-patch the class methods.

def _make_path_hook(method_name: str, orig_fn):
    """Create a hooked version of a Path method that checks jail before writing."""
    def hooked(self_path, *args, **kwargs):
        _check_write(f"Path.{method_name}", self_path)
        return orig_fn(self_path, *args, **kwargs)
    hooked.__name__ = method_name
    hooked.__qualname__ = f"Path.{method_name}"
    return hooked


# ─────────────────────────────────────────────────────────────
# Install / Uninstall
# ─────────────────────────────────────────────────────────────

def install(state: Any) -> None:
    """Install write-class Python API hooks. Layer 1.5 — real implementation."""
    global _state, _installed
    _state = state

    if _installed:
        sys.stderr.write("[agent_worker_hooks] already installed, skipping\n")
        return

    try:
        # ── Save originals ──
        _originals["os.mkdir"] = os.mkdir
        _originals["os.makedirs"] = os.makedirs
        _originals["os.remove"] = os.remove
        _originals["os.unlink"] = os.unlink
        _originals["os.rmdir"] = os.rmdir
        _originals["os.rename"] = os.rename
        _originals["shutil.copy"] = shutil.copy
        _originals["shutil.copy2"] = shutil.copy2
        _originals["shutil.move"] = shutil.move
        _originals["shutil.rmtree"] = shutil.rmtree
        _originals["shutil.copytree"] = shutil.copytree
        _originals["builtins.open"] = builtins.open

        # ── Install os hooks ──
        os.mkdir = _hooked_mkdir
        os.makedirs = _hooked_makedirs
        os.remove = _hooked_remove
        os.unlink = _hooked_unlink
        os.rmdir = _hooked_rmdir
        os.rename = _hooked_rename

        # ── Install shutil hooks ──
        shutil.copy = _hooked_shutil_copy
        shutil.copy2 = _hooked_shutil_copy2
        shutil.move = _hooked_shutil_move
        shutil.rmtree = _hooked_shutil_rmtree
        shutil.copytree = _hooked_shutil_copytree

        # ── Install builtins.open hook ──
        builtins.open = _hooked_open

        # ── Install pathlib.Path hooks ──
        for method_name in ("write_text", "write_bytes", "mkdir",
                            "unlink", "rename", "rmdir", "touch"):
            orig = getattr(Path, method_name, None)
            if orig is not None:
                _originals[f"Path.{method_name}"] = orig
                setattr(Path, method_name, _make_path_hook(method_name, orig))

        _installed = True
        sys.stderr.write(
            f"[agent_worker_hooks] Layer 1.5 installed; "
            f"root={_jail_root()}, hooks={len(_originals)}\n"
        )
    except Exception as exc:
        # Must never prevent worker from booting
        sys.stderr.write(f"[agent_worker_hooks] install failed: {exc}\n")


def uninstall() -> None:
    """Restore all original functions. Useful for testing."""
    global _installed
    if not _installed:
        return

    try:
        # Restore os functions
        for attr in ("mkdir", "makedirs", "remove", "unlink", "rmdir", "rename"):
            key = f"os.{attr}"
            if key in _originals:
                setattr(os, attr, _originals[key])

        # Restore shutil functions
        for attr in ("copy", "copy2", "move", "rmtree", "copytree"):
            key = f"shutil.{attr}"
            if key in _originals:
                setattr(shutil, attr, _originals[key])

        # Restore builtins.open
        if "builtins.open" in _originals:
            builtins.open = _originals["builtins.open"]

        # Restore pathlib.Path methods
        for method_name in ("write_text", "write_bytes", "mkdir",
                            "unlink", "rename", "rmdir", "touch"):
            key = f"Path.{method_name}"
            if key in _originals:
                setattr(Path, method_name, _originals[key])

        _originals.clear()
        _installed = False
        sys.stderr.write("[agent_worker_hooks] uninstalled\n")
    except Exception as exc:
        sys.stderr.write(f"[agent_worker_hooks] uninstall failed: {exc}\n")


def add_allowed_dir(path: str) -> None:
    """Dynamically add a directory to the allowed list.

    Used when a skill is granted to an agent at runtime — the skill's
    workspace directory needs to be writable.
    """
    if _state is not None:
        dirs = getattr(_state, "skill_dirs", None)
        if dirs is None:
            _state.skill_dirs = [path]
        elif path not in dirs:
            dirs.append(path)


def get_audit_log() -> list[tuple[float, str, str, bool]]:
    """Return a copy of the audit log for inspection."""
    return list(_audit_log)
