"""
Centralized default configuration values for TudouClaw.

Previously hardcoded across dozens of modules, these constants are now
collected here so that:

  1. Every "magic number" has a descriptive name.
  2. Deployment-specific values can be tuned in **one** file.
  3. ``config.yaml`` and ``TUDOU_*`` environment variables can still
     override the most common settings via the existing ``_load_config()``
     pipeline in ``app.llm``.

Grouping follows domain, not module — if you are looking for a constant
to tweak, scan the section headers below.
"""

from __future__ import annotations

# ── Network ──────────────────────────────────────────────────────────────

PORTAL_PORT = 9090
WEB_PORT = 8080
AGENT_PORT = 8081
WS_BUS_PORT = 9900
BIND_ADDRESS = "0.0.0.0"
CORS_ORIGINS_DEFAULT = (
    "http://localhost:5173,"
    "http://localhost:3000,"
    "http://127.0.0.1:5173"
)

# Used by _print_banner() helpers to discover the host's LAN IP.
IP_DETECT_TARGET = "8.8.8.8"
IP_DETECT_PORT = 80

# Addresses treated as "local" for authentication restrictions.
LOCAL_ADDRESSES = ("127.0.0.1", "::1", "localhost")

# ── Service URLs (defaults when no config.yaml / env var is set) ─────────

OLLAMA_URL = "http://localhost:11434"
OPENAI_BASE_URL = "https://api.openai.com/v1"
UNSLOTH_BASE_URL = "http://localhost:8888/v1"
WS_MASTER_URL = "ws://localhost:9900"

# ── LLM Model Defaults ──────────────────────────────────────────────────

DEFAULT_PROVIDER = ""
DEFAULT_MODEL = ""
# (2026-04-28) Removed hardcoded "qwen3:8b" / "ollama" defaults. Agents
# MUST explicitly bind a provider+model via Settings — no silent fall-through
# to a global default. Chat will fail loudly with a clear "no model bound"
# error rather than hitting Ollama with a model name that may not exist.
DEFAULT_EMBEDDING_MODEL = "all-MiniLM-L6-v2"
SKILLFORGE_MODEL = "claude-opus-4"

# ── Timeouts (seconds) ──────────────────────────────────────────────────

APPROVAL_TIMEOUT = 300
DELEGATION_TIMEOUT = 300
TASK_EXECUTION_TIMEOUT = 300
WORKFLOW_TIMEOUT = 600
HTTP_REQUEST_TIMEOUT = 30
SQLITE_CONNECT_TIMEOUT = 30
WS_PING_TIMEOUT = 5
WS_ACK_TIMEOUT = 5.0

# ── Retry / Concurrency ─────────────────────────────────────────────────

LLM_MAX_RETRIES = 5
LLM_BACKOFF_FACTOR = 2.0
LLM_POOL_CONNECTIONS = 4
LLM_POOL_MAXSIZE = 8
MAX_PARALLEL_WORKERS = 6
MAX_TASK_DEPTH = 16
MAX_TASK_RETAINED = 64
MAX_DELEGATION_CONCURRENCY = 4
MAX_DELEGATION_QUEUE = 32

# ── Upload / File Size Limits (bytes) ────────────────────────────────────

MAX_CONTENT_UPLOAD = 2 * 1024 * 1024        # 2 MB
MAX_DATA_UPLOAD = 10 * 1024 * 1024           # 10 MB
MAX_FILE_SERVE = 25 * 1024 * 1024            # 25 MB
MAX_JSON_FRAME = 64 * 1024 * 1024            # 64 MB
MAX_HASH_BYTES = 100 * 1024 * 1024           # 100 MB
HASH_CHUNK_SIZE = 1024 * 1024                # 1 MB
LOG_MAX_MB = 50

# ── Character / Truncation Limits ────────────────────────────────────────

MAX_SKILL_RESULT_CHARS = 15000
MAX_JSON_RESULT_CHARS = 8000
MAX_TOOL_RESULT_CHARS = 30000
MAX_HTTP_RESPONSE_CHARS = 15000
CONTENT_PREVIEW_CHARS = 3000

# ── Session / Rate Limiting / Retention ──────────────────────────────────

SESSION_TTL = 86400                          # 1 day
RATE_LIMIT_RPS = 120
RATE_LIMIT_BURST = 200
AUDIT_LOG_MAX_BUFFER = 500
AUDIT_LOG_RETENTION = 8000
MAX_CHANGELOG_SIZE = 1000
EVENT_HISTORY_SIZE = 5000
MESSAGE_LOAD_LIMIT = 3000

# ── Scan Parameters ──────────────────────────────────────────────────────

SCAN_MAX_DEPTH = 6
SCAN_MAX_FILES = 2000
PROJECT_SCAN_MAX_FILES = 5000

# ── LLM Budget Thresholds ───────────────────────────────────────────────

BUDGET_WARNING_THRESHOLD = 0.7
BUDGET_ATTENTION_THRESHOLD = 0.9

# Context-window usage warnings (fraction of context consumed).
CONTEXT_WARN_HIGH = 0.90
CONTEXT_WARN_MEDIUM = 0.75
CONTEXT_WARN_LOW = 0.50
