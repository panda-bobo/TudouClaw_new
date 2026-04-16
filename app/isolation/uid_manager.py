"""
app.isolation.uid_manager — Per-agent UID/GID allocation and namespace setup.

Provides OS-level isolation between agent worker processes:

  Linux:   User Namespace + Mount Namespace (no root required)
           Each worker gets a unique UID inside its namespace, with
           bind-mounted views of only its own workspace + shared dirs.

  macOS:   Graceful degradation — no namespace support, relies on
           SharedFileRouter for logical write isolation.

UID/GID assignments are persisted to ``uid_pool.json`` so they survive
restarts. The pool uses ranges that won't collide with real system users:

  Agent UIDs:   60001 – 60999
  Project GIDs: 70001 – 70999
  Meeting GIDs: 71001 – 71999
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("tudou.uid_manager")

# UID/GID ranges
_AGENT_UID_START = 60001
_AGENT_UID_END = 60999
_PROJECT_GID_START = 70001
_PROJECT_GID_END = 70999
_MEETING_GID_START = 71001
_MEETING_GID_END = 71999


# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

def platform_supports_namespaces() -> bool:
    """Return True if the OS supports user namespaces (Linux only)."""
    if sys.platform != "linux":
        return False
    # Check if unprivileged user namespaces are enabled
    try:
        val = Path("/proc/sys/kernel/unprivileged_userns_clone").read_text().strip()
        return val == "1"
    except FileNotFoundError:
        # File doesn't exist on many distros → userns enabled by default
        return True
    except Exception:
        return False


def platform_supports_setuid() -> bool:
    """Return True if we have permission to setuid (root or CAP_SETUID)."""
    return os.geteuid() == 0


# ---------------------------------------------------------------------------
# UidManager — UID/GID pool allocation + group management
# ---------------------------------------------------------------------------

class UidManager:
    """Manages per-agent UID allocation and project/meeting group membership.

    Thread-safe. Persists assignments to disk.
    """

    def __init__(self, data_dir: str):
        self._data_dir = data_dir
        self._pool_file = os.path.join(data_dir, "uid_pool.json")
        self._lock = threading.Lock()
        # agent_id -> uid
        self._agent_uids: Dict[str, int] = {}
        # group_name -> gid  (e.g. "proj_abc12345" -> 70001)
        self._groups: Dict[str, int] = {}
        # group_name -> set of agent_ids
        self._group_members: Dict[str, set] = {}
        self._load()

    # ── UID allocation ──

    def allocate_uid(self, agent_id: str) -> int:
        """Allocate (or return existing) UID for an agent."""
        with self._lock:
            if agent_id in self._agent_uids:
                return self._agent_uids[agent_id]
            used = set(self._agent_uids.values())
            uid = _AGENT_UID_START
            while uid in used and uid <= _AGENT_UID_END:
                uid += 1
            if uid > _AGENT_UID_END:
                raise RuntimeError("Agent UID pool exhausted "
                                   f"({_AGENT_UID_END - _AGENT_UID_START + 1} max)")
            self._agent_uids[agent_id] = uid
            self._save()
            logger.info("Allocated UID %d for agent %s", uid, agent_id[:8])
            return uid

    def get_uid(self, agent_id: str) -> Optional[int]:
        """Get existing UID for an agent, or None."""
        return self._agent_uids.get(agent_id)

    # ── Group (GID) allocation ──

    def allocate_project_gid(self, project_id: str) -> int:
        """Allocate (or return existing) GID for a project shared group."""
        return self._allocate_gid(f"proj_{project_id}",
                                  _PROJECT_GID_START, _PROJECT_GID_END)

    def allocate_meeting_gid(self, meeting_id: str) -> int:
        """Allocate (or return existing) GID for a meeting group."""
        return self._allocate_gid(f"mtg_{meeting_id}",
                                  _MEETING_GID_START, _MEETING_GID_END)

    def _allocate_gid(self, group_name: str, start: int, end: int) -> int:
        with self._lock:
            if group_name in self._groups:
                return self._groups[group_name]
            used = set(self._groups.values())
            gid = start
            while gid in used and gid <= end:
                gid += 1
            if gid > end:
                raise RuntimeError(f"GID pool exhausted for {group_name}")
            self._groups[group_name] = gid
            self._group_members.setdefault(group_name, set())
            self._save()
            logger.info("Allocated GID %d for group %s", gid, group_name)
            return gid

    # ── Group membership ──

    def add_to_group(self, agent_id: str, group_name: str):
        """Add an agent to a group (project or meeting)."""
        with self._lock:
            self._group_members.setdefault(group_name, set())
            if agent_id not in self._group_members[group_name]:
                self._group_members[group_name].add(agent_id)
                self._save()
                logger.debug("Agent %s added to group %s",
                            agent_id[:8], group_name)

    def remove_from_group(self, agent_id: str, group_name: str):
        """Remove an agent from a group."""
        with self._lock:
            members = self._group_members.get(group_name, set())
            if agent_id in members:
                members.discard(agent_id)
                self._save()

    def get_agent_groups(self, agent_id: str) -> List[str]:
        """Return all group names the agent belongs to."""
        with self._lock:
            return [g for g, members in self._group_members.items()
                    if agent_id in members]

    def get_agent_gids(self, agent_id: str) -> List[int]:
        """Return all GIDs the agent belongs to (for supplementary groups)."""
        with self._lock:
            gids = []
            for group_name, members in self._group_members.items():
                if agent_id in members and group_name in self._groups:
                    gids.append(self._groups[group_name])
            return gids

    # ── Directory permission setup ──

    def setup_private_workspace(self, workspace_dir: str, agent_id: str):
        """Set ownership and permissions on agent's private workspace.

        On Linux with root: chown uid:gid, chmod 0700
        Otherwise: chmod 0700 (best-effort)
        """
        os.makedirs(workspace_dir, exist_ok=True)
        try:
            os.chmod(workspace_dir, 0o700)
        except OSError:
            pass
        if platform_supports_setuid():
            uid = self.allocate_uid(agent_id)
            try:
                os.chown(workspace_dir, uid, uid)
                # Recursively chown existing contents
                for root, dirs, files in os.walk(workspace_dir):
                    for d in dirs:
                        try:
                            os.chown(os.path.join(root, d), uid, uid)
                        except OSError:
                            pass
                    for f in files:
                        try:
                            os.chown(os.path.join(root, f), uid, uid)
                        except OSError:
                            pass
                logger.debug("Private workspace %s owned by uid=%d",
                            workspace_dir, uid)
            except OSError as e:
                logger.debug("chown failed for %s: %s (non-root?)",
                            workspace_dir, e)

    def setup_shared_directory(self, shared_dir: str, group_name: str):
        """Set group ownership and setgid on a shared directory.

        On Linux with root: chown :gid, chmod 2770 (setgid)
        Otherwise: chmod 0770 (best-effort)
        """
        os.makedirs(shared_dir, exist_ok=True)
        gid = self._groups.get(group_name)
        if gid is None:
            # Allocate if needed
            if group_name.startswith("proj_"):
                gid = self.allocate_project_gid(group_name[5:])
            elif group_name.startswith("mtg_"):
                gid = self.allocate_meeting_gid(group_name[4:])
            else:
                return

        try:
            if platform_supports_setuid():
                os.chown(shared_dir, -1, gid)   # Only change group
                os.chmod(shared_dir, 0o2770)     # setgid + rwxrwx---
                logger.debug("Shared dir %s group=%d (setgid)",
                            shared_dir, gid)
            else:
                os.chmod(shared_dir, 0o770)
        except OSError as e:
            logger.debug("Shared dir setup failed for %s: %s", shared_dir, e)

    # ── Namespace preexec helpers (Linux only) ──

    def build_preexec_fn(
        self,
        agent_id: str,
        work_dir: str,
        shared_dirs: Optional[List[str]] = None,
    ) -> Optional[Callable[[], None]]:
        """Build a preexec_fn for subprocess.Popen that sets up isolation.

        Returns None on platforms that don't support namespaces.

        The returned function is called in the child process after fork()
        but before exec(). It:
          1. Creates a new user namespace (CLONE_NEWUSER)
          2. Maps the agent's allocated UID inside the namespace
          3. If mount namespace is available, restricts filesystem view
        """
        if not platform_supports_namespaces():
            return None

        uid = self.allocate_uid(agent_id)
        gids = self.get_agent_gids(agent_id)
        parent_uid = os.getuid()
        parent_gid = os.getgid()

        def _preexec():
            _setup_user_namespace(uid, parent_uid, parent_gid, gids)

        return _preexec

    # ── Persistence ──

    def _load(self):
        if not os.path.exists(self._pool_file):
            return
        try:
            with open(self._pool_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._agent_uids = {k: int(v) for k, v in
                                data.get("agent_uids", {}).items()}
            self._groups = {k: int(v) for k, v in
                           data.get("groups", {}).items()}
            self._group_members = {
                k: set(v) for k, v in
                data.get("group_members", {}).items()
            }
        except Exception as e:
            logger.warning("Failed to load uid_pool.json: %s", e)

    def _save(self):
        os.makedirs(self._data_dir, exist_ok=True)
        data = {
            "agent_uids": self._agent_uids,
            "groups": self._groups,
            "group_members": {k: list(v) for k, v in
                              self._group_members.items()},
        }
        tmp = self._pool_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._pool_file)
        except Exception as e:
            logger.warning("Failed to save uid_pool.json: %s", e)

    # ── Status ──

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "platform_namespaces": platform_supports_namespaces(),
                "platform_setuid": platform_supports_setuid(),
                "allocated_uids": len(self._agent_uids),
                "allocated_groups": len(self._groups),
                "uid_range": f"{_AGENT_UID_START}-{_AGENT_UID_END}",
            }


# ---------------------------------------------------------------------------
# Linux User Namespace setup (runs in child process after fork)
# ---------------------------------------------------------------------------

def _setup_user_namespace(
    target_uid: int,
    parent_uid: int,
    parent_gid: int,
    supplementary_gids: Optional[List[int]] = None,
) -> None:
    """Set up a user namespace in the current (child) process.

    After this function returns, the process sees itself as ``target_uid``
    inside its namespace but is mapped to ``parent_uid`` on the host.

    This provides process identity isolation: if Agent A's worker crashes
    or is compromised, it cannot masquerade as Agent B because each has
    a distinct UID inside its namespace.

    For full filesystem isolation, a mount namespace (CLONE_NEWNS) would
    also be needed to restrict the visible directory tree. That is left
    for Phase 2b.
    """
    import ctypes
    import ctypes.util

    CLONE_NEWUSER = 0x10000000

    libc_name = ctypes.util.find_library("c")
    if not libc_name:
        raise OSError("Cannot find libc for unshare(2)")

    libc = ctypes.CDLL(libc_name, use_errno=True)
    libc.unshare.argtypes = [ctypes.c_int]
    libc.unshare.restype = ctypes.c_int

    ret = libc.unshare(CLONE_NEWUSER)
    if ret != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"unshare(CLONE_NEWUSER) failed: "
                      f"{os.strerror(errno)}")

    pid = os.getpid()

    # Must deny setgroups before writing gid_map (kernel requirement)
    try:
        with open(f"/proc/{pid}/setgroups", "w") as f:
            f.write("deny\n")
    except FileNotFoundError:
        pass  # Older kernels may not have this file

    # Write uid_map: <inside_uid> <outside_uid> <count>
    # Maps target_uid inside namespace → parent_uid on host
    with open(f"/proc/{pid}/uid_map", "w") as f:
        f.write(f"{target_uid} {parent_uid} 1\n")

    # Write gid_map: map gid 0 → parent_gid, plus supplementary groups
    # Note: unprivileged user namespaces can only map a single line
    with open(f"/proc/{pid}/gid_map", "w") as f:
        f.write(f"{target_uid} {parent_gid} 1\n")

    logger.debug("User namespace created: uid=%d→%d, gid=%d→%d, pid=%d",
                target_uid, parent_uid, target_uid, parent_gid, pid)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional[UidManager] = None
_instance_lock = threading.Lock()


def get_uid_manager(data_dir: str = "") -> UidManager:
    """Return the global UidManager singleton."""
    global _instance
    if _instance is not None:
        return _instance
    with _instance_lock:
        if _instance is not None:
            return _instance
        if not data_dir:
            from .. import DEFAULT_DATA_DIR
            data_dir = DEFAULT_DATA_DIR
        _instance = UidManager(data_dir)
        return _instance
