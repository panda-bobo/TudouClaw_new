"""
app.isolation — Physical worker isolation for tool execution.

Layer 1 design (see docs/ISOLATION.md):
  - Every agent owns a long-running worker subprocess.
  - Worker runs pinned to its own work_dir with HOME / TMPDIR / PWD
    pointing inside that jail. Credentials are scrubbed from env.
  - Worker only performs "safe" in-jail tool calls. Anything that
    crosses the jail boundary (MCP, skill, hub RPC, shared_workspace,
    pip install, unknown bash commands) is bounced back to the main
    process "gatekeeper" via the rpc protocol defined in protocol.py.
  - Capability updates (newly bound MCP, newly granted skill, policy
    changes) are pushed to the worker via `notify` frames, so we do
    not have to restart workers on every admin action.

This package is intentionally dependency-light so agent_worker.py can
import it in a fresh interpreter without dragging in llm/hub/mcp.
"""
from __future__ import annotations

from .protocol import (
    DEFAULT_CHAN_ID,
    FRAME_VERSION,
    Frame,
    FrameKind,
    ProtocolError,
    decode_frame,
    encode_frame,
    read_frame,
    write_frame,
)

__all__ = [
    "DEFAULT_CHAN_ID",
    "FRAME_VERSION",
    "Frame",
    "FrameKind",
    "ProtocolError",
    "decode_frame",
    "encode_frame",
    "read_frame",
    "write_frame",
    # Phase 2: uid isolation + shared file routing
    "get_uid_manager",
    "get_shared_file_router",
]

# Lazy imports for Phase 2 components (avoid circular deps at startup)
def get_uid_manager(data_dir: str = ""):
    from .uid_manager import get_uid_manager as _get
    return _get(data_dir)

def get_shared_file_router(data_dir: str = ""):
    from .shared_file_router import get_shared_file_router as _get
    return _get(data_dir)
