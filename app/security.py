"""
Security utilities for TudouClaw.

1. MCP credential safety: filter env vars, strip secrets from error messages
2. Context injection scanning: detect invisible unicode and injection patterns
3. Model-specific prompt engineering helpers
"""

import re
import logging

logger = logging.getLogger("tudou.security")

# ── MCP Credential Safety ──

# Environment variables safe to pass to MCP subprocess
SAFE_ENV_KEYS = frozenset({
    "PATH", "HOME", "USER", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "SHELL", "TMPDIR", "TMP", "TEMP",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME",
    "XDG_RUNTIME_DIR", "XDG_STATE_HOME",
    "PYTHONPATH", "NODE_PATH", "GOPATH",
})

# Patterns that indicate secrets in strings
_SECRET_PATTERNS = [
    # ── API keys / LLM tokens ────────────────────────────────────
    re.compile(r'sk-[a-zA-Z0-9]{20,}'),                  # OpenAI-style
    re.compile(r'sk-proj-[A-Za-z0-9_\-]{20,}'),          # OpenAI project key
    re.compile(r'sk-ant-api[a-zA-Z0-9-]{20,}'),          # Anthropic
    re.compile(r'sk-ant-[A-Za-z0-9_\-]{20,}'),           # Anthropic generic
    # ── GitHub family (added evolver coverage: ghu_ ghs_ pat_) ──
    re.compile(r'ghp_[a-zA-Z0-9]{36,}'),
    re.compile(r'gho_[a-zA-Z0-9]{36,}'),
    re.compile(r'ghu_[a-zA-Z0-9]{36,}'),
    re.compile(r'ghs_[a-zA-Z0-9]{36,}'),
    re.compile(r'github_pat_[A-Za-z0-9_]{22,}'),
    # ── Cloud providers ────────────────────────────────────────
    re.compile(r'AKIA[0-9A-Z]{16}'),                     # AWS access key id
    re.compile(r'AccountKey=[^;\s]+', re.IGNORECASE),    # Azure storage
    re.compile(r'client_secret=[A-Za-z0-9~._\-]{8,}', re.IGNORECASE),
    re.compile(r'instrumentationkey=[0-9a-fA-F-]{20,}', re.IGNORECASE),
    # ── Other ecosystems ──────────────────────────────────────
    re.compile(r'glpat-[a-zA-Z0-9-]{20,}'),              # GitLab
    re.compile(r'npm_[A-Za-z0-9]{36,}'),                 # npm
    re.compile(r'xox[baprsv]-[A-Za-z0-9-]{10,}'),        # Slack tokens
    # Discord bot token (3 base64url segments, leading [MNO])
    re.compile(r'\b[MNO][A-Za-z0-9_\-]{23,}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}\b'),
    # ── JWT (header.payload.signature) ────────────────────────
    re.compile(r'eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]{20,}'),
    # ── Generic auth headers / passwords ──────────────────────
    re.compile(r'Bearer\s+[a-zA-Z0-9._\-~+/]{20,}=*'),
    re.compile(r'token[=:]\s*["\']?[A-Za-z0-9._\-~+/]{16,}["\']?', re.IGNORECASE),
    re.compile(r'api[_-]?key[=:]\s*["\']?[A-Za-z0-9._\-~+/]{16,}["\']?', re.IGNORECASE),
    re.compile(r'secret[=:]\s*["\']?[A-Za-z0-9._\-~+/]{16,}["\']?', re.IGNORECASE),
    re.compile(r'password["\s:=]+\S{6,}', re.IGNORECASE),
    # ── Long opaque hex (often API keys) ──────────────────────
    re.compile(r'[a-f0-9]{64}'),
    # ── Private keys (RSA / EC / DSA / OPENSSH) ───────────────
    re.compile(r'-----BEGIN\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----[\s\S]*?-----END\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----'),
    # ── Basic auth credentials in URLs (keep schema + host) ──
    re.compile(r'(?<=://)[^@\s]+:[^@\s]+(?=@)'),
]


# ── Leak scanners (detection without destructive replacement) ──
# Each returns (type, suggested_env_var_for_replacement) so the caller
# can produce actionable remediation hints, not just "found a leak".
# Ported from evolver's sanitize.js LEAK_SCANNERS table.
_LEAK_SCANNERS: list[tuple[str, "re.Pattern", str]] = [
    ("api_key",   re.compile(r'sk-[A-Za-z0-9]{20,}'),                "OPENAI_API_KEY"),
    ("api_key",   re.compile(r'sk-proj-[A-Za-z0-9_\-]{20,}'),         "OPENAI_API_KEY"),
    ("api_key",   re.compile(r'sk-ant-[A-Za-z0-9_\-]{20,}'),          "ANTHROPIC_API_KEY"),
    ("aws_key",   re.compile(r'AKIA[0-9A-Z]{16}'),                    "AWS_ACCESS_KEY_ID"),
    ("github",    re.compile(r'ghp_[A-Za-z0-9]{36,}'),                "GITHUB_TOKEN"),
    ("github",    re.compile(r'github_pat_[A-Za-z0-9_]{22,}'),        "GITHUB_TOKEN"),
    ("npm",       re.compile(r'npm_[A-Za-z0-9]{36,}'),                "NPM_TOKEN"),
    ("slack",     re.compile(r'xox[baprsv]-[A-Za-z0-9-]{10,}'),       "SLACK_TOKEN"),
    ("jwt",       re.compile(r'eyJ[A-Za-z0-9_\-]+\.eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]{20,}'), "JWT_TOKEN"),
    ("azure",     re.compile(r'AccountKey=[^;\s]+', re.IGNORECASE),   "AZURE_STORAGE_KEY"),
    ("azure",     re.compile(r'client_secret=[A-Za-z0-9~._\-]{8,}', re.IGNORECASE), "AZURE_CLIENT_SECRET"),
    ("azure",     re.compile(r'instrumentationkey=[0-9a-fA-F-]{20,}', re.IGNORECASE), "APPINSIGHTS_INSTRUMENTATIONKEY"),
    ("discord",   re.compile(r'\b[MNO][A-Za-z0-9_\-]{23,}\.[A-Za-z0-9_\-]{6}\.[A-Za-z0-9_\-]{27,}\b'), "DISCORD_TOKEN"),
    ("bearer",    re.compile(r'Bearer\s+[A-Za-z0-9._\-~+/]{20,}=*'),  "AUTH_TOKEN"),
    ("private_key", re.compile(r'-----BEGIN\s+(?:RSA\s+|EC\s+|DSA\s+|OPENSSH\s+)?PRIVATE\s+KEY-----'), "PRIVATE_KEY_PATH"),
    # Database connection strings with embedded credentials
    ("db_url",    re.compile(r'(?:mongodb|postgres|postgresql|mysql|redis|amqp)://[^\s"\',;)\}\]]{10,}', re.IGNORECASE), "DATABASE_URL"),
    # Local filesystem paths (privacy — agent traces shouldn't leak operator's home)
    ("local_path", re.compile(r'/home/[a-zA-Z0-9_.\-]+/'),            "HOME"),
    ("local_path", re.compile(r'/Users/[a-zA-Z0-9_.\-]+/'),           "HOME"),
    ("local_path", re.compile(r'[A-Z]:\\Users\\[a-zA-Z0-9_.\-]+\\'), "USERPROFILE"),
    # Internal IP ranges (RFC1918)
    ("internal_ip", re.compile(r'\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3})(?::\d{2,5})?\b'), "SERVICE_HOST"),
    # SSH targets (user@host)
    ("ssh_target", re.compile(r'[a-zA-Z0-9_.\-]+@(?:(?:\d{1,3}\.){3}\d{1,3}|[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})'), "SSH_HOST"),
    # Generic password assignments
    ("password",  re.compile(r'password[=:]\s*["\']?[^\s"\',;)\}\]]{6,}["\']?', re.IGNORECASE), "PASSWORD"),
    # Basic auth in URLs (full match)
    ("basic_auth", re.compile(r'://[^@\s:]+:[^@\s]+@'),               "SERVICE_URL"),
]


# Env vars never worth flagging as leaks — universal shell vars
# whose values are not really sensitive.
_ENV_SCAN_SKIP_KEYS = frozenset({
    "PATH", "HOME", "SHELL", "TERM", "LANG", "USER", "LOGNAME",
    "PWD", "OLDPWD", "SHLVL", "HOSTNAME", "DISPLAY", "EDITOR",
    "PAGER", "LESS", "LS_COLORS", "COLORTERM", "TERM_PROGRAM",
    "XDG_SESSION_ID", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
    "SSH_AUTH_SOCK", "SSH_AGENT_PID", "_",
    # Tudou-specific noise
    "TUDOU_DATA_DIR", "TUDOU_HOST", "TUDOU_PORT",
})


def scan_for_leaks(content: str) -> dict:
    """Scan content for sensitive patterns WITHOUT modifying it.

    Returns ``{"found": bool, "leaks": [{"type", "value", "suggestion"}, ...]}``
    where each leak's ``value`` is truncated to 60 chars for safe
    logging. ``suggestion`` is the env-var name the operator should
    use instead of hardcoding.

    Use this when you want to *detect* leaks (e.g. blocking a
    wiki_ingest with embedded keys, alerting in CI) rather than
    silently rewriting them via ``strip_secrets``.
    """
    if not isinstance(content, str) or not content:
        return {"found": False, "leaks": []}
    leaks: list[dict] = []
    seen: set = set()
    for typ, pattern, suggest in _LEAK_SCANNERS:
        for m in pattern.finditer(content):
            val = m.group(0)
            key = (typ, val)
            if key in seen:
                continue
            seen.add(key)
            display = val if len(val) <= 60 else val[:57] + "..."
            leaks.append({
                "type": typ,
                "value": display,
                "suggestion": "os.environ['" + suggest + "']",
            })
    return {"found": bool(leaks), "leaks": leaks}


def detect_env_value_leaks(content: str) -> list[dict]:
    """Reverse detect: any process env vars whose VALUE appears
    verbatim in ``content``? If so, the operator hardcoded it
    instead of reading os.environ.

    Skips short values (<8 chars) and universal shell vars to keep
    false positives low.
    """
    if not isinstance(content, str) or not content:
        return []
    import os as _os
    leaks: list[dict] = []
    for k, v in _os.environ.items():
        if not v or len(v) < 8:
            continue
        if k in _ENV_SCAN_SKIP_KEYS:
            continue
        if v in content:
            display = v if len(v) <= 60 else v[:57] + "..."
            leaks.append({
                "type": "env_value_leak",
                "env_key": k,
                "value": display,
                "suggestion": "os.environ['" + k + "']",
            })
    return leaks


def full_leak_check(content: str) -> dict:
    """Combined pattern-based scan + env value reverse detection.

    Use before sending content to external services (wiki_ingest,
    LangSmith trace, hub upload) to surface anything sensitive that
    needs cleaning up FIRST, rather than leaking and apologising.
    """
    scan = scan_for_leaks(content)
    env_leaks = detect_env_value_leaks(content)
    all_leaks = list(scan["leaks"]) + env_leaks
    return {"found": bool(all_leaks), "leaks": all_leaks}

def filter_env_for_mcp(env: dict) -> dict:
    """Filter environment variables, keeping only safe ones + explicitly configured ones."""
    safe = {}
    for k, v in env.items():
        if k in SAFE_ENV_KEYS or k.startswith("MCP_"):
            safe[k] = v
    return safe

def strip_secrets(text: str) -> str:
    """Remove API keys, tokens, and passwords from text before showing to LLM."""
    result = text
    for pattern in _SECRET_PATTERNS:
        result = pattern.sub("[REDACTED]", result)
    return result


# ── Context Injection Scanning ──

# Invisible unicode characters that can hide instructions
_INVISIBLE_CHARS = set([
    '\u200b',  # zero-width space
    '\u200c',  # zero-width non-joiner
    '\u200d',  # zero-width joiner
    '\u2060',  # word joiner
    '\u2061',  # function application
    '\u2062',  # invisible times
    '\u2063',  # invisible separator
    '\u2064',  # invisible plus
    '\ufeff',  # BOM / zero-width no-break space
    '\u00ad',  # soft hyphen
    '\u200e',  # LTR mark
    '\u200f',  # RTL mark
    '\u202a',  # LTR embedding
    '\u202b',  # RTL embedding
    '\u202c',  # pop directional
    '\u202d',  # LTR override
    '\u202e',  # RTL override
    '\u2066',  # LTR isolate
    '\u2067',  # RTL isolate
    '\u2068',  # first strong isolate
    '\u2069',  # pop directional isolate
])

_INJECTION_PATTERNS = [
    re.compile(r'ignore\s+(all\s+)?previous\s+instructions', re.IGNORECASE),
    re.compile(r'system\s+prompt\s+override', re.IGNORECASE),
    re.compile(r'you\s+are\s+now\s+', re.IGNORECASE),
    re.compile(r'forget\s+(all\s+)?(your\s+)?instructions', re.IGNORECASE),
    re.compile(r'new\s+instructions?\s*:', re.IGNORECASE),
    re.compile(r'disregard\s+(all\s+)?previous', re.IGNORECASE),
    re.compile(r'override\s+safety', re.IGNORECASE),
    re.compile(r'jailbreak', re.IGNORECASE),
]

def scan_content(text: str, source: str = "") -> tuple[bool, str]:
    """Scan text for injection attacks and invisible characters.

    Returns (is_safe, reason). If is_safe is False, reason describes the threat.
    """
    if not text:
        return True, ""

    # Check for invisible unicode
    found_invisible = [c for c in text if c in _INVISIBLE_CHARS]
    if len(found_invisible) > 3:
        chars = set(f"U+{ord(c):04X}" for c in found_invisible[:5])
        return False, f"Invisible unicode characters detected: {chars} (source: {source})"

    # Check for injection patterns
    for pattern in _INJECTION_PATTERNS:
        match = pattern.search(text)
        if match:
            return False, f"Injection pattern detected: '{match.group()[:50]}' (source: {source})"

    return True, ""

def sanitize_content(text: str) -> str:
    """Remove invisible unicode characters from text."""
    return ''.join(c for c in text if c not in _INVISIBLE_CHARS)


# ── Model-Specific Tool Use Enforcement ──

def get_model_tool_guidance(model: str) -> str:
    """Return model-specific guidance for tool use enforcement.

    Some models tend to describe actions rather than actually calling tools.
    This injects specific steering prompts to fix that.
    """
    model_lower = (model or "").lower()

    if any(k in model_lower for k in ("gpt-4", "gpt-3.5", "gpt-4o")):
        return (
            "\n\nIMPORTANT: When you need to perform an action (read files, "
            "search, execute commands), you MUST use the provided tools by "
            "making function calls. Do NOT describe what you would do — "
            "actually call the tool."
        )

    if any(k in model_lower for k in ("gemini", "gemma")):
        return (
            "\n\nCRITICAL: You have access to tools. When a task requires "
            "reading files, searching, or executing commands, you MUST invoke "
            "the appropriate tool function. Never respond with a description "
            "of what tool you'd use — invoke it directly."
        )

    if any(k in model_lower for k in ("qwen", "deepseek", "minimax")):
        return (
            "\n\n注意: 当你需要读取文件、搜索、执行命令时，必须直接调用对应的工具函数。"
            "不要只描述你会做什么——请直接调用工具执行操作。"
        )

    return ""  # Claude and well-behaved models don't need steering
