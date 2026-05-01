"""MCP-credential encryption (2026-04-29).

Threat we mitigate: an agent's bash / read_file can reach the MCP
config files (``~/.tudou_claw/mcp_configs.json``, the SQLite DB, etc.).
Until today these stored env values like ``SMTP_PASSWORD`` in plaintext.
A research-paralysis loop in 小土 picked up the SMTP credentials from
the JSON, wrote a Python smtplib script, and sent a real email to an
external Gmail address — bypassing every redirect we'd built at the
``mcp_call`` tool layer.

Design:

  * **At-rest** — env values whose KEY matches a sensitive pattern
    (password / secret / token / api_key / credential / etc.) are stored
    as ``"enc:v1:<urlsafe-base64-fernet-ciphertext>"`` on disk.

  * **At-spawn** — only the MCP dispatcher decrypts (just before
    ``subprocess.Popen``) so the plaintext lives in process memory for
    the duration of the MCP child process and never on disk or in any
    log line.

  * **Key** — Fernet symmetric key in
    ``~/.tudou_claw/.mcp_master_key`` with mode ``0600``. Generated on
    first use. Sandbox deny-list (and the bash blacklist regex) MUST
    block agents from reading this file. Loss of the file = all MCP
    credentials become unreadable; user re-enters via UI.

  * **Migration** — old plaintext entries are accepted on read (backward
    compat); the next save through ``encrypt_dict`` rewrites them as
    ciphertext.

Failure mode: if the ``cryptography`` library is missing, encryption
silently no-ops (values stay plaintext). We log a WARNING once so the
admin notices, but functionality continues — the cost of refusing to
operate is worse than the cost of plaintext for a single missing dep.
"""
from __future__ import annotations

import base64
import logging
import os
import re
import secrets as _stdlib_secrets
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("tudou.mcp.secrets")


# Wire-format prefix. Versioning lets us rotate algorithms later without
# breaking on-disk values. v1 = Fernet (AES-128-CBC + HMAC-SHA256).
_PREFIX = "enc:v1:"

# Env-key patterns that mark a value as sensitive. Lowercased substring
# match — covers PASSWORD / SMTP_PASS / API_KEY / SECRET_KEY / TOKEN /
# AUTH_TOKEN / REFRESH_TOKEN / PRIVATE_KEY / CREDENTIAL / etc.
_SENSITIVE_KEY_PATTERNS = (
    "password", "passwd", "pwd",
    "secret",
    "token",
    "api_key", "apikey",
    "credential", "cred",
    "auth",
    "private_key", "privatekey",
    "session", "session_id",
    "access_key", "access_token",
)


def _key_path() -> Path:
    """Return the master-key file path. Honours TUDOU_CLAW_DATA_DIR."""
    base = os.environ.get("TUDOU_CLAW_DATA_DIR") or str(Path.home() / ".tudou_claw")
    return Path(base) / ".mcp_master_key"


_key_lock = threading.Lock()
_cached_key: bytes | None = None
_warned_missing_lib = False


def _load_or_create_key() -> bytes | None:
    """Return raw 32-byte Fernet key. Generates one if absent.

    Returns None if cryptography lib is missing (caller falls back to
    no-op encryption). Caches in process memory after first call.

    Safety: NEVER auto-regenerates a key when the file exists but is
    unreadable. Doing so silently invalidates every previously-
    encrypted value on disk (the 2026-04-29 incident: existing
    ciphertext became `<decrypt-failed>` because the file was lost
    or replaced). When the file is corrupt, refuse to encrypt
    further (return None) and surface a loud ERROR so the admin can
    restore the file from backup or wipe stale ciphertext via the
    documented recovery procedure.
    """
    global _cached_key, _warned_missing_lib
    if _cached_key is not None:
        return _cached_key
    with _key_lock:
        if _cached_key is not None:
            return _cached_key
        try:
            from cryptography.fernet import Fernet  # noqa: F401
        except ImportError:
            if not _warned_missing_lib:
                logger.warning(
                    "MCP secret encryption disabled — `cryptography` not "
                    "installed. Plaintext fallback in use; install via "
                    "`pip install cryptography` to enable at-rest encryption.",
                )
                _warned_missing_lib = True
            return None
        kp = _key_path()
        if kp.exists():
            try:
                _cached_key = kp.read_bytes().strip()
                if not _cached_key:
                    raise ValueError("empty key file")
                return _cached_key
            except Exception as e:
                # CRITICAL: don't silently regenerate. A new key would
                # invalidate every existing ciphertext. Log loudly and
                # return None so callers see encryption-disabled rather
                # than encrypt-with-mismatched-key.
                logger.error(
                    "🚨 MCP master key file %s exists but is UNREADABLE (%s). "
                    "REFUSING to regenerate — auto-regen would orphan "
                    "every previously-encrypted credential. To recover: "
                    "(a) restore the file from backup, OR "
                    "(b) `mv %s{,.broken} && rm -f` then re-enter "
                    "credentials via the UI (existing ciphertext will "
                    "become unreadable). Encryption disabled this session.",
                    kp, e, kp,
                )
                return None
        # Generate fresh key — Fernet.generate_key() returns urlsafe
        # base64-encoded 32 random bytes.
        from cryptography.fernet import Fernet
        new_key = Fernet.generate_key()
        try:
            kp.parent.mkdir(parents=True, exist_ok=True)
            # Atomic-ish write + 0600 perms before content so other
            # processes/users can't read while we're writing.
            tmp = kp.with_suffix(kp.suffix + ".tmp")
            with open(tmp, "wb") as f:
                os.chmod(tmp, 0o600)
                f.write(new_key)
            os.replace(tmp, kp)
            os.chmod(kp, 0o600)
        except Exception as e:
            logger.error(
                "Failed to persist MCP master key to %s: %s — "
                "encryption will work this session only.",
                kp, e,
            )
        _cached_key = new_key
        return _cached_key


def is_encrypted(value: Any) -> bool:
    """True if ``value`` looks like our wire-format ciphertext."""
    return isinstance(value, str) and value.startswith(_PREFIX)


def _is_sensitive_key(key: str) -> bool:
    k = (key or "").lower()
    return any(pat in k for pat in _SENSITIVE_KEY_PATTERNS)


def encrypt_value(plaintext: str) -> str:
    """Encrypt one string. Returns ciphertext (with prefix). On any
    failure (missing lib, key write fail), returns the plaintext
    unchanged with a warning log so the system stays operational."""
    if not isinstance(plaintext, str):
        return plaintext  # only encrypt strings
    if is_encrypted(plaintext):
        return plaintext  # idempotent — already ciphertext
    if not plaintext:
        return plaintext  # empty stays empty
    key = _load_or_create_key()
    if key is None:
        return plaintext
    try:
        from cryptography.fernet import Fernet
        ct = Fernet(key).encrypt(plaintext.encode("utf-8"))
        return _PREFIX + ct.decode("ascii")
    except Exception as e:
        logger.warning("encrypt_value failed: %s; stored as plaintext", e)
        return plaintext


def decrypt_value(value: str) -> str:
    """Decrypt one wire-format string. Plain values pass through.
    On decrypt failure (key mismatch / corrupted), returns the
    placeholder ``"<decrypt-failed>"`` so the calling MCP at least
    sees an obviously-wrong credential rather than silent empty."""
    if not is_encrypted(value):
        return value if isinstance(value, str) else value
    key = _load_or_create_key()
    if key is None:
        return value  # no lib → can't decrypt; let caller see ciphertext
    try:
        from cryptography.fernet import Fernet, InvalidToken
        ct = value[len(_PREFIX):].encode("ascii")
        try:
            return Fernet(key).decrypt(ct).decode("utf-8")
        except InvalidToken:
            logger.error(
                "MCP secret decrypt failed — token mismatch (wrong key? "
                "tampered ciphertext?). Returning placeholder.",
            )
            return "<decrypt-failed>"
    except Exception as e:
        logger.error("decrypt_value failed: %s", e)
        return "<decrypt-failed>"


def encrypt_env_dict(env: dict | None) -> dict:
    """Walk an env dict; encrypt VALUES whose KEY is sensitive.
    Non-sensitive keys (HOST / PORT / DEBUG / etc.) stay plaintext —
    encrypting them adds no security benefit and makes ops debugging
    harder."""
    if not isinstance(env, dict):
        return {}
    out = {}
    for k, v in env.items():
        if _is_sensitive_key(k) and isinstance(v, str) and v and not is_encrypted(v):
            out[k] = encrypt_value(v)
        else:
            out[k] = v
    return out


def decrypt_env_dict(env: dict | None) -> dict:
    """Walk an env dict; decrypt any encrypted values. Used by the MCP
    dispatcher right before subprocess.Popen, so plaintext credentials
    only exist in the spawned child's memory + Popen kwargs (never on
    disk, never in any log)."""
    if not isinstance(env, dict):
        return {}
    out = {}
    for k, v in env.items():
        if is_encrypted(v):
            out[k] = decrypt_value(v)
        else:
            out[k] = v
    return out


def regenerate_key_for_test() -> None:
    """Test-only — clears cached key + deletes file so the next
    operation generates a new one. Never call from production code."""
    global _cached_key
    with _key_lock:
        _cached_key = None
        try:
            kp = _key_path()
            if kp.exists():
                kp.unlink()
        except Exception:
            pass


# UI mask placeholder. Sent to the admin browser instead of either the
# plaintext (security risk) or the ciphertext (useless to display, also
# security-by-obscurity violation). When the admin re-saves an MCP, any
# field still equal to this placeholder is treated as "unchanged — keep
# existing encrypted value on the server side".
MASK_PLACEHOLDER = "••••••"


def mask_env_for_display(env: dict | None) -> dict:
    """Return a copy of env with sensitive values replaced by the mask
    placeholder. Used by GET endpoints feeding the admin UI."""
    if not isinstance(env, dict):
        return {}
    out = {}
    for k, v in env.items():
        if _is_sensitive_key(k) and isinstance(v, str) and v:
            # Either ciphertext or stale plaintext — neither should be
            # exposed to the browser. Show mask only.
            out[k] = MASK_PLACEHOLDER
        else:
            out[k] = v
    return out


def merge_unchanged_from_existing(new_env: dict | None,
                                    existing_env: dict | None) -> dict:
    """Save-side helper: when admin POSTs an env dict, any value still
    equal to the mask placeholder means "the form field wasn't touched"
    — keep whatever the server has on disk.

    Returns a merged dict ready to be passed into ``encrypt_env_dict``
    (which is idempotent, so already-encrypted existing values pass
    through cleanly)."""
    new_env = dict(new_env or {})
    existing_env = dict(existing_env or {})
    out = {}
    # Start from new — any keys admin explicitly typed
    for k, v in new_env.items():
        if v == MASK_PLACEHOLDER and k in existing_env:
            # Admin didn't touch this field — keep server value as-is
            out[k] = existing_env[k]
        else:
            out[k] = v
    # Preserve keys that exist on server but were dropped by client
    # (some forms only POST changed fields; safer to preserve unknowns).
    for k, v in existing_env.items():
        out.setdefault(k, v)
    return out


def migrate_plaintext_env(env: dict | None) -> tuple[dict, int]:
    """One-shot migration: walk env, encrypt any sensitive plaintext.
    Returns (new_env, count_migrated). Idempotent — already-encrypted
    values pass through. Used at startup to fix existing config files
    written before encryption support landed."""
    if not isinstance(env, dict):
        return ({}, 0)
    out = {}
    n = 0
    for k, v in env.items():
        if (_is_sensitive_key(k) and isinstance(v, str) and v
                and not is_encrypted(v)):
            out[k] = encrypt_value(v)
            if is_encrypted(out[k]):
                n += 1
        else:
            out[k] = v
    return (out, n)


__all__ = [
    "encrypt_value", "decrypt_value",
    "encrypt_env_dict", "decrypt_env_dict",
    "mask_env_for_display", "merge_unchanged_from_existing",
    "migrate_plaintext_env",
    "is_encrypted",
    "MASK_PLACEHOLDER",
]
