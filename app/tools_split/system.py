"""System / exec tools — bash, pip_install, desktop_screenshot.

Grouped together because all three shell out (subprocess) or touch
the host system beyond the normal tool sandbox.
"""
from __future__ import annotations

import os
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
    try:
        jailed = pol.mode in ("restricted", "strict")
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            # Always prefer the sandbox policy root (the agent's
            # working_dir). Falling back to os.getcwd() would run the
            # command in the server-process CWD (the code package
            # directory), causing runtime artefacts to leak into the
            # source tree.
            cwd=str(pol.root) if getattr(pol, "root", None) else os.getcwd(),
            env=pol.scrub_env() if jailed else None,
        )
        output_parts = []
        if result.stdout:
            output_parts.append(result.stdout)
        if result.stderr:
            output_parts.append(f"[stderr]\n{result.stderr}")
        output_parts.append(f"[exit code: {result.returncode}]")
        return "\n".join(output_parts)
    except subprocess.TimeoutExpired:
        return f"Error: Command timed out after {timeout}s"
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
