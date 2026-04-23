"""System / exec tools — bash, pip_install, desktop_screenshot.

Grouped together because all three shell out (subprocess) or touch
the host system beyond the normal tool sandbox.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import Any

from .. import sandbox as _sandbox


# _tool_bash clamps user-supplied timeout to this range. 600 s is
# a hard ceiling because a longer subprocess usually means a hung
# process and the agent should loop / split instead.
_BASH_TIMEOUT_MIN_S = 1
_BASH_TIMEOUT_MAX_S = 600
_BASH_TIMEOUT_DEFAULT_S = 30

# pip install: give it 5 minutes — first-time installs of heavy wheels
# (numpy, torch, pptx) can genuinely take that long on slow networks.
_PIP_TIMEOUT_S = 300

# desktop_screenshot fallback subprocess timeouts.
_DESKTOP_CAPTURE_TIMEOUT_S = 10


# ── bash ─────────────────────────────────────────────────────────────

def _tool_bash(command: str, timeout: int = _BASH_TIMEOUT_DEFAULT_S,
               **_: Any) -> str:
    pol = _sandbox.get_current_policy()
    ok, err = pol.check_command(command)
    if not ok:
        return f"Error: {err}"
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
