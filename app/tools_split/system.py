"""System / exec tools — bash, pip_install, desktop_screenshot.

Grouped together because all three shell out (subprocess) or touch
the host system beyond the normal tool sandbox.

Bash supports two modes:
  - foreground (default): synchronous, returns stdout/stderr/exit_code
  - background (run_in_background=True): Popen + return immediately
    with a process_id; logs streamed to a tmp file. Use bash_logs(pid)
    to pull incremental output, bash_kill(pid) to terminate.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any

from .. import sandbox as _sandbox


# _tool_bash clamps user-supplied timeout to this range. 600 s is
# a hard ceiling because a longer subprocess usually means a hung
# process and the agent should loop / split instead.
_BASH_TIMEOUT_MIN_S = 1
_BASH_TIMEOUT_MAX_S = 600
_BASH_TIMEOUT_DEFAULT_S = 30

# Background-job registry. process_id → record. Records persist for
# 1 hour past process exit so the agent can still pull logs after
# completion. {pid: {cmd, log_path, started_at, proc, exit_code,
#                    finished_at, last_read_offset}}
_BACKGROUND_JOBS: dict[int, dict] = {}
_BACKGROUND_JOBS_LOCK = threading.Lock()
_BG_RETAIN_SECONDS = 3600
_BG_LOG_DEFAULT_LINES = 30
_BG_LOG_MAX_LINES = 500

# pip install: give it 5 minutes — first-time installs of heavy wheels
# (numpy, torch, pptx) can genuinely take that long on slow networks.
_PIP_TIMEOUT_S = 300

# desktop_screenshot fallback subprocess timeouts.
_DESKTOP_CAPTURE_TIMEOUT_S = 10


# ── bash ─────────────────────────────────────────────────────────────

def _tool_bash(command: str, timeout: int = _BASH_TIMEOUT_DEFAULT_S,
               run_in_background: bool = False,
               background_log_lines: int = _BG_LOG_DEFAULT_LINES,
               **kwargs: Any) -> str:
    pol = _sandbox.get_current_policy()
    ok, err = pol.check_command(command)
    if not ok:
        return f"Error: {err}"
    # Background mode short-circuits the normal sync path. We still
    # apply sandbox check above (bg jobs shouldn't bypass restricted-
    # mode rules), but skip the read-counter (that's for sync reads
    # the LLM is *waiting* on).
    if run_in_background:
        return _bash_start_background(
            command, pol, kwargs,
            log_lines_to_return=background_log_lines,
        )
    # Day 3 AM (2026-05-05): cross-tool read counter. If the bash
    # command is a read primitive (cat / head / tail / less / sed -n
    # / awk on a file), share the read-valve counter with read_file.
    # Closes the loophole where agents bypass fs.py's read-valve by
    # switching to ``bash cat <path>``.
    try:
        from . import _read_counter as _xc
        from .fs import _get_caller_agent
        caller_id = kwargs.get("_caller_agent_id", "") or ""
        agent = _get_caller_agent(caller_id) if caller_id else None
        if agent is not None:
            extracted = _xc.extract_read_path_from_bash(command)
            if extracted is not None:
                _xtpath, _xtsource = extracted
                # Count BEFORE check so blocked-message uses the
                # incremented number that triggered the block.
                _xtn = _xc.bump_read(agent, _xtpath, source=_xtsource)
                if _xc.is_blocked(agent, _xtpath):
                    return _xc.blocked_message(_xtpath, _xtn, _xtsource)
    except Exception:
        pass
    try:
        timeout = max(_BASH_TIMEOUT_MIN_S,
                      min(int(timeout), _BASH_TIMEOUT_MAX_S))
    except Exception:
        timeout = _BASH_TIMEOUT_DEFAULT_S
    jailed = pol.mode in ("restricted", "strict")
    cwd = str(pol.root) if getattr(pol, "root", None) else os.getcwd()
    env = pol.scrub_env() if jailed else None

    # Auto-inject PYTHONPATH so skill python helpers (e.g. `_pptx_helpers`
    # under pptx-author/) are importable from agent scripts with a plain
    # `from _pptx_helpers import *` — no sys.path preamble. Two sources:
    #   1. <cwd>/skills/*/ — per-agent copies made by add_skill_to_workspace
    #   2. <app_pkg>/skills/builtin/*/*/ — source-dir fallback, so import
    #      works even for agents that haven't "installed" the skill to
    #      workspace yet. Only dirs that contain at least one top-level
    #      .py file get added (SKILL.md-only dirs don't pollute path).
    try:
        extra: list[str] = []
        skills_root = os.path.join(cwd, "skills")
        if os.path.isdir(skills_root):
            for d in sorted(os.listdir(skills_root)):
                p = os.path.join(skills_root, d)
                if os.path.isdir(p):
                    extra.append(p)
        # Builtin source-dir fallback. Resolve once: app/skills/builtin/
        try:
            from .. import skills as _skills_pkg
            builtin_root = os.path.join(
                os.path.dirname(_skills_pkg.__file__), "builtin")
        except Exception:
            builtin_root = ""
        if builtin_root and os.path.isdir(builtin_root):
            for group in sorted(os.listdir(builtin_root)):
                group_p = os.path.join(builtin_root, group)
                if not os.path.isdir(group_p):
                    continue
                for name in sorted(os.listdir(group_p)):
                    sp = os.path.join(group_p, name)
                    if not os.path.isdir(sp):
                        continue
                    # Only add dirs that ship a .py helper at top level.
                    try:
                        has_py = any(
                            f.endswith(".py") and f != "__init__.py"
                            for f in os.listdir(sp)
                            if os.path.isfile(os.path.join(sp, f))
                        )
                    except OSError:
                        has_py = False
                    if has_py:
                        extra.append(sp)
        if extra:
            if env is None:
                env = os.environ.copy()
            existing = env.get("PYTHONPATH", "")
            parts = extra + ([existing] if existing else [])
            env["PYTHONPATH"] = os.pathsep.join(parts)
    except Exception:
        # Never block bash on path-discovery failure.
        pass

    # Switched from subprocess.run → Popen + communicate so we can
    # track the child pid in the abort registry. A user clicking "终止"
    # on a meeting/project/agent flips the registry's abort flag AND
    # sends SIGTERM to every tracked pid — giving us real kill power
    # over the runaway `python build_report.py` script mid-execution.
    from .. import abort_registry
    task_key = abort_registry.current_key()
    proc = None
    try:
        # start_new_session=True so SIGTERM on the pid also kills its
        # grandchildren (python build.py → spawned subprocess etc.).
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        if task_key:
            abort_registry.track_pid(task_key, proc.pid)
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            # Kill the whole process group so runaway python children
            # also die, not just the shell wrapper.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                proc.terminate()
            try:
                proc.communicate(timeout=2)
            except Exception:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except Exception:
                    proc.kill()
            return f"Error: Command timed out after {timeout}s"
        finally:
            if task_key and proc is not None:
                abort_registry.untrack_pid(task_key, proc.pid)

        # If the abort flag was flipped while we were waiting (and the
        # subprocess was killed by the registry's SIGTERM), surface
        # that clearly. Exit code will usually be -signal.SIGTERM (-15)
        # or similar negative on POSIX.
        if task_key and abort_registry.is_aborted(task_key):
            return (f"⏸ ABORTED by user. Command terminated "
                    f"(exit code {returncode}).\n[exit code: {returncode}]")

        output_parts = []
        if stdout:
            output_parts.append(stdout)
        if stderr:
            output_parts.append(f"[stderr]\n{stderr}")
        # Make failure UNMISSABLE. Agents were observed ignoring a
        # bare "[exit code: 1]" line and telling the user "done" even
        # when a python-pptx script SyntaxError'd without producing
        # any output file. Lead with a LOUD ❌ header when returncode
        # != 0 so the LLM's attention lands on it. Success stays quiet.
        if returncode != 0:
            output_parts.insert(0,
                f"❌ COMMAND FAILED (exit code {returncode}). "
                f"DO NOT report success. Read stderr above, fix the root "
                f"cause, and rerun before claiming the task is done."
            )
        output_parts.append(f"[exit code: {returncode}]")
        return "\n".join(output_parts)
    except Exception as e:
        return f"Error executing command: {e}"


# ── bash background helpers ────────────────────────────────────────

def _bash_start_background(command: str, pol, kwargs: dict,
                            *, log_lines_to_return: int) -> str:
    """Start the command in the background, return immediately with the
    process id + first slice of log. Process keeps running; agent uses
    bash_logs(pid) / bash_kill(pid) to interact further.
    """
    jailed = pol.mode in ("restricted", "strict")
    cwd = str(pol.root) if getattr(pol, "root", None) else os.getcwd()
    env = pol.scrub_env() if jailed else None
    # Single log file gets stdout+stderr merged; matches what dev-server
    # output looks like in a normal terminal (where stderr goes to the
    # same TTY).
    fd, log_path = tempfile.mkstemp(prefix="tudou_bash_bg_", suffix=".log")
    os.close(fd)
    try:
        log_fh = open(log_path, "w")
    except OSError as e:
        return f"Error: could not open background log file: {e}"
    try:
        proc = subprocess.Popen(
            command,
            shell=True,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
    except Exception as e:
        log_fh.close()
        try:
            os.remove(log_path)
        except OSError:
            pass
        return f"Error starting background command: {e}"
    # Track in abort registry — same kill-power as foreground bash.
    try:
        from .. import abort_registry
        task_key = abort_registry.current_key()
        if task_key:
            abort_registry.track_pid(task_key, proc.pid)
    except Exception:
        task_key = None
    # Register
    record = {
        "command": command,
        "pid": proc.pid,
        "log_path": log_path,
        "log_fh": log_fh,
        "proc": proc,
        "started_at": time.time(),
        "finished_at": None,
        "exit_code": None,
        "task_key": task_key,
        "cwd": cwd,
    }
    with _BACKGROUND_JOBS_LOCK:
        _BACKGROUND_JOBS[proc.pid] = record
        _gc_finished_jobs_unlocked()

    # Give the process a brief head-start to emit initial output (or
    # crash on startup) before we read the log.
    time.sleep(0.6)
    # If the process already exited (typo, immediate crash) — surface
    # the failure right here instead of returning a pid for a dead
    # process the agent will then have to bash_logs() to discover.
    rc = proc.poll()
    if rc is not None:
        record["exit_code"] = rc
        record["finished_at"] = time.time()
        log_excerpt = _read_log_tail(log_path, log_lines_to_return)
        prefix = ("❌ Background process EXITED IMMEDIATELY "
                  f"(exit code {rc}). Likely a typo or startup error.")
        return (
            f"{prefix}\n"
            f"[pid {proc.pid} · log {log_path}]\n"
            f"--- log (last {log_lines_to_return} lines) ---\n"
            f"{log_excerpt}"
        )
    log_excerpt = _read_log_tail(log_path, log_lines_to_return)
    return (
        f"🟢 Background process started · pid={proc.pid}\n"
        f"command: {command[:160]}\n"
        f"log: {log_path}\n"
        f"Use bash_logs(process_id={proc.pid}) to pull more output, "
        f"bash_kill(process_id={proc.pid}) to terminate.\n"
        f"--- log (first {log_lines_to_return} lines, if any) ---\n"
        f"{log_excerpt or '(no output yet)'}"
    )


def _read_log_tail(path: str, max_lines: int) -> str:
    try:
        with open(path, "r", errors="replace") as fh:
            data = fh.read()
    except OSError:
        return ""
    lines = data.splitlines()
    if len(lines) <= max_lines:
        return data
    return "\n".join(lines[-max_lines:])


def _gc_finished_jobs_unlocked() -> None:
    """Drop background-job records older than _BG_RETAIN_SECONDS past
    finish. Caller must hold _BACKGROUND_JOBS_LOCK.
    """
    now = time.time()
    to_drop: list[int] = []
    for pid, rec in _BACKGROUND_JOBS.items():
        finished_at = rec.get("finished_at")
        if finished_at and (now - finished_at) > _BG_RETAIN_SECONDS:
            to_drop.append(pid)
    for pid in to_drop:
        rec = _BACKGROUND_JOBS.pop(pid, None)
        if rec:
            try:
                fh = rec.get("log_fh")
                if fh and not fh.closed:
                    fh.close()
            except Exception:
                pass
            try:
                lp = rec.get("log_path")
                if lp and os.path.exists(lp):
                    os.remove(lp)
            except OSError:
                pass


def _refresh_job_status(rec: dict) -> None:
    """Poll the subprocess; if it exited, mark finished_at + exit_code
    and close the log file handle."""
    if rec.get("finished_at"):
        return
    proc = rec.get("proc")
    if proc is None:
        return
    rc = proc.poll()
    if rc is not None:
        rec["exit_code"] = rc
        rec["finished_at"] = time.time()
        try:
            fh = rec.get("log_fh")
            if fh and not fh.closed:
                fh.close()
        except Exception:
            pass


def _tool_bash_logs(process_id: int = 0, lines: int = _BG_LOG_DEFAULT_LINES,
                     **_: Any) -> str:
    """Pull recent log lines from a background bash process started
    via bash(run_in_background=True). lines clamped to [1, 500].
    """
    if not process_id:
        return "Error: process_id is required."
    try:
        process_id = int(process_id)
        lines = max(1, min(int(lines), _BG_LOG_MAX_LINES))
    except Exception:
        return "Error: process_id and lines must be integers."
    with _BACKGROUND_JOBS_LOCK:
        rec = _BACKGROUND_JOBS.get(process_id)
        if rec is None:
            return (f"Error: no background process with pid {process_id} "
                    f"(it may have been GC'd; jobs are kept ~1 hour after exit).")
        _refresh_job_status(rec)
        snapshot = dict(rec)  # shallow copy for use outside lock
    log_excerpt = _read_log_tail(snapshot["log_path"], lines)
    finished_at = snapshot.get("finished_at")
    if finished_at:
        rc = snapshot.get("exit_code")
        status = (f"⏹ Process EXITED with code {rc}"
                  + (f" {time.strftime('%H:%M:%S', time.localtime(finished_at))}" if finished_at else ""))
    else:
        elapsed = time.time() - snapshot["started_at"]
        status = f"🟢 Still running ({elapsed:.0f}s elapsed)"
    return (
        f"{status} · pid={process_id}\n"
        f"command: {snapshot['command'][:160]}\n"
        f"--- log (last {lines} lines) ---\n"
        f"{log_excerpt or '(empty)'}"
    )


def _tool_bash_kill(process_id: int = 0, **_: Any) -> str:
    """Terminate a background bash process. SIGTERM first, SIGKILL if
    it doesn't exit within 2s. Idempotent — calling on an already-
    finished pid returns its final status without erroring.
    """
    if not process_id:
        return "Error: process_id is required."
    try:
        process_id = int(process_id)
    except Exception:
        return "Error: process_id must be an integer."
    with _BACKGROUND_JOBS_LOCK:
        rec = _BACKGROUND_JOBS.get(process_id)
        if rec is None:
            return (f"Error: no background process with pid {process_id} "
                    f"(may have been GC'd or never started).")
        _refresh_job_status(rec)
        if rec.get("finished_at"):
            rc = rec.get("exit_code")
            return f"Process {process_id} already exited with code {rc}."
        proc = rec["proc"]
    # Out of lock for the actual signalling — kill can take time.
    try:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except Exception:
            proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                proc.kill()
            proc.wait(timeout=2)
    except Exception as e:
        return f"Error killing process {process_id}: {e}"
    # Final refresh
    with _BACKGROUND_JOBS_LOCK:
        rec = _BACKGROUND_JOBS.get(process_id)
        if rec:
            _refresh_job_status(rec)
            rc = rec.get("exit_code")
        else:
            rc = "unknown"
    return f"⏹ Process {process_id} terminated (final exit code {rc})."


# ── pip_install ──────────────────────────────────────────────────────

def _tool_pip_install(packages: str, upgrade: bool = False, **_: Any) -> str:
    """Install or upgrade Python packages using pip."""
    if not packages or not packages.strip():
        return "Error: packages parameter is required"

    try:
        pkg_list = packages.split()
        cmd = [sys.executable, "-m", "pip", "install"]
        if upgrade:
            cmd.append("--upgrade")
        cmd.extend(pkg_list)
        cmd.append("--break-system-packages")

        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=_PIP_TIMEOUT_S)

        if result.returncode == 0:
            return f"✓ Successfully installed: {', '.join(pkg_list)}"
        return f"Error installing packages: {result.stderr}"
    except Exception as e:
        return f"Error: {e}"


# ── desktop_screenshot ───────────────────────────────────────────────

def _tool_desktop_screenshot(output_path: str = "",
                             region: dict | None = None,
                             **_: Any) -> str:
    """Take a screenshot of the desktop."""
    try:
        from datetime import datetime

        if not output_path:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = f"screenshot_{timestamp}.png"

        pol = _sandbox.get_current_policy()
        output_file = pol.safe_path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        # Strategy 1: mss (cross-platform, preferred).
        try:
            import mss
            import mss.tools
            with mss.mss() as sct:
                monitor = sct.monitors[1]  # Primary monitor
                if region:
                    screenshot = sct.grab({
                        'left': region.get('x', 0),
                        'top': region.get('y', 0),
                        'width': region.get('w', monitor['width']),
                        'height': region.get('h', monitor['height']),
                    })
                else:
                    screenshot = sct.grab(monitor)
                mss.tools.to_png(screenshot.rgb, screenshot.size,
                                 output=str(output_file))
                return f"✓ Screenshot saved: {output_path}"
        except ImportError:
            pass

        # Strategy 2: PIL ImageGrab (macOS/Win only).
        try:
            from PIL import ImageGrab
            if region:
                bbox = (region.get('x', 0), region.get('y', 0),
                        region.get('x', 0) + region.get('w', 1920),
                        region.get('y', 0) + region.get('h', 1080))
                img = ImageGrab.grab(bbox=bbox)
            else:
                img = ImageGrab.grab()
            img.save(str(output_file), 'PNG')
            return f"✓ Screenshot saved: {output_path}"
        except ImportError:
            pass

        # Strategy 3: platform-specific CLIs.
        if os.name == 'posix':
            # Linux: scrot.
            result = subprocess.run(
                ["scrot", str(output_file)],
                capture_output=True, timeout=_DESKTOP_CAPTURE_TIMEOUT_S)
            if result.returncode == 0:
                return f"✓ Screenshot saved: {output_path}"
            # macOS: screencapture.
            result = subprocess.run(
                ["screencapture", "-x", str(output_file)],
                capture_output=True, timeout=_DESKTOP_CAPTURE_TIMEOUT_S)
            if result.returncode == 0:
                return f"✓ Screenshot saved: {output_path}"

        return ("Error: Could not take screenshot "
                "(mss, PIL, scrot, or screencapture required)")
    except Exception as e:
        return f"Error taking screenshot: {e}"
