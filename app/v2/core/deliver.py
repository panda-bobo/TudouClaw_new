"""
Deliver phase dispatchers (PRD §8.5).

Two dispatch paths:

    1. **Template-driven** — the task's template declares an
       ``expected_artifacts[].delivery`` block specifying an MCP tool or
       skill to call, with an ``args_template`` whose ``{placeholders}``
       are interpolated from ``filled_slots`` plus the artifact being
       delivered. This is the recommended path for e-mail, RAG, and any
       integration that already exists as an MCP or skill.

    2. **Kind-based fallback** — if the template has no matching
       delivery config, we fall back to a built-in dispatcher keyed on
       ``artifact.kind``. Those built-ins cover ``file`` (existence
       check), ``message`` / ``api_call`` (no-op), and explicit
       "not implemented" for ``email`` / ``rag`` (so degraded state is
       loud and visible).

A dispatcher returns ``(ok, handle, note)`` where ``handle`` is the
concrete delivery id on success. On failure we retry ≤ 2 times per
artifact (PRD: "单个 artifact 交付失败重试 ≤ 2；仍失败则 degraded
状态但不阻塞整体") and emit a ``delivery_receipt`` artifact with
``handle="degraded:<artifact_id>"`` so the audit trail is intact.
"""
from __future__ import annotations

import logging
import os
import string
from typing import Callable, Tuple


logger = logging.getLogger("tudouclaw.v2.deliver")


def deliver_artifact(
    artifact,
    task,
    *,
    max_retries: int = 2,
    template: dict | None = None,
) -> Tuple[bool, str, str]:
    """Dispatch one artifact. Returns ``(ok, receipt_handle, note)``.

    ``max_retries`` is 2 per PRD: one initial attempt + 2 retries = 3 total.
    ``template`` is the task's YAML template dict — when it declares a
    matching ``expected_artifacts[].delivery`` block we dispatch through
    that; otherwise the built-in kind-based dispatcher handles it.
    """
    delivery_cfg = _find_delivery_config(template, artifact) if template else None
    fn: Callable = (
        (lambda a, t: _dispatch_template_driven(a, t, delivery_cfg))
        if delivery_cfg
        else _DISPATCH.get(artifact.kind, _dispatch_unknown)
    )

    last_note = ""
    attempts = max_retries + 1
    for _ in range(attempts):
        try:
            ok, handle, note = fn(artifact, task)
        except Exception as e:  # noqa: BLE001
            ok, handle, note = False, "", f"{type(e).__name__}: {e}"
        if ok:
            return True, handle, note
        last_note = note
    return False, "", last_note


# ── template-driven dispatch (recommended) ────────────────────────────


def _find_delivery_config(template: dict, artifact) -> dict | None:
    """Match the artifact against ``template.expected_artifacts[]`` and
    return the first entry's ``delivery`` block, if any.

    Matching strategy (conservative):
      * If the entry's ``kind`` equals ``artifact.kind``, it matches.
      * If ``pattern`` is supplied and ``fnmatch`` matches the artifact's
        handle basename, it also matches.
      * If neither key is specified, every artifact matches (wildcard).
    """
    import fnmatch

    expected = (template.get("expected_artifacts") or []) if template else []
    for entry in expected:
        if not isinstance(entry, dict):
            continue
        if "delivery" not in entry:
            continue
        k = entry.get("kind")
        if k and k != artifact.kind:
            continue
        pat = entry.get("pattern")
        if pat and not fnmatch.fnmatch(os.path.basename(artifact.handle or ""), pat):
            continue
        delivery = entry.get("delivery")
        if isinstance(delivery, dict) and delivery:
            return delivery
    return None


def _dispatch_template_driven(
    artifact,
    task,
    delivery_cfg: dict,
) -> Tuple[bool, str, str]:
    """Dispatch via an MCP tool, a skill, or no-op per template config.

    Config shape::

        delivery:
          via: mcp | skill | none
          tool: <tool_name>              # required for mcp/skill
          args_template: {k: "{slot_name}", ...}   # values may reference
                                                   # {filled_slots.*} and
                                                   # {artifact.handle} etc.

    Returns (ok, handle, note) suitable for deliver_artifact.
    """
    via = str(delivery_cfg.get("via") or "").strip().lower()
    if via == "none":
        # Template explicitly opts out — artifact is considered delivered
        # as soon as it exists.
        h = artifact.handle or f"inline:{artifact.id}"
        return True, h, "delivery disabled by template"

    tool = str(delivery_cfg.get("tool") or "").strip()
    if not tool:
        return False, "", f"template delivery for {artifact.kind!r} has no tool"

    args_raw = delivery_cfg.get("args_template") or {}
    if not isinstance(args_raw, dict):
        return False, "", "args_template must be a dict"

    try:
        args = _interp_args(args_raw, task=task, artifact=artifact)
    except KeyError as e:
        return False, "", f"missing interp key: {e}"

    agent_id = task.agent_id or ""
    if via == "mcp":
        from ..bridges import mcp_bridge
        result = mcp_bridge.invoke_mcp(agent_id, tool, args)
    elif via == "skill":
        from ..bridges import skill_bridge
        result = skill_bridge.invoke_skill(agent_id, tool, args)
    else:
        return False, "", f"unknown delivery.via={via!r}"

    # A delivery tool's job is to SEND — we treat any non-empty string
    # as success and use it as the receipt handle. If the tool returned
    # a structured dict we serialise; the audit trail has full fidelity.
    if result is None or result == "":
        return False, "", f"{via} tool {tool!r} returned empty"
    if isinstance(result, str):
        return True, result[:200], f"{via}:{tool}"
    import json as _json
    try:
        h = _json.dumps(result, ensure_ascii=False)
    except Exception:
        h = str(result)
    return True, h[:200], f"{via}:{tool}"


class _SafeFormatter(string.Formatter):
    """``format_map`` that keeps ``{unknown}`` literal instead of raising."""

    def get_value(self, key, args, kwargs):
        try:
            return super().get_value(key, args, kwargs)
        except (KeyError, IndexError):
            return "{" + str(key) + "}"


_FMT = _SafeFormatter()


def _interp_args(args_raw: dict, *, task, artifact) -> dict:
    """Walk ``args_raw`` recursively and ``{placeholder}``-interpolate
    every string leaf. Exposed keys::

        {any_slot_name}        — from task.context.filled_slots
        {artifact_handle}      — artifact.handle
        {artifact_kind}        — artifact.kind
        {artifact_summary}     — artifact.summary
        {intent}               — task.intent
    """
    env: dict = dict(task.context.filled_slots or {})
    env.update({
        "artifact_handle": artifact.handle or "",
        "artifact_kind":   artifact.kind or "",
        "artifact_summary": artifact.summary or "",
        "intent":          task.intent or "",
    })

    def _walk(v):
        if isinstance(v, str):
            return _FMT.vformat(v, (), env)
        if isinstance(v, list):
            return [_walk(x) for x in v]
        if isinstance(v, dict):
            return {k: _walk(x) for k, x in v.items()}
        return v

    return _walk(args_raw)


# ── per-kind dispatchers (legacy fallback) ────────────────────────────


def _dispatch_file(artifact, task) -> Tuple[bool, str, str]:
    """File artifacts are already on disk after Execute — we just
    verify the path still exists and record size as proof."""
    path = (artifact.handle or "").strip()
    if not path:
        return False, "", "file artifact has empty handle"
    if not os.path.exists(path):
        return False, "", f"file not found: {path!r}"
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return False, "", f"cannot stat: {e}"
    return True, path, f"file present ({size} bytes)"


def _dispatch_email(artifact, task) -> Tuple[bool, str, str]:
    """No built-in email dispatcher — templates should bind an MCP or
    skill via ``delivery.via``. Marked degraded if they don't."""
    return False, "", "email delivery not configured (template must set delivery.via)"


def _dispatch_rag(artifact, task) -> Tuple[bool, str, str]:
    """Same rationale as ``_dispatch_email``."""
    return False, "", "rag delivery not configured (template must set delivery.via)"


def _dispatch_message(artifact, task) -> Tuple[bool, str, str]:
    """Message artifacts are fulfilled by Report (which writes the final
    assistant message into ``task.context.messages``). Deliver just
    confirms the artifact stays addressable."""
    return True, f"inline:{artifact.id}", "delivered inline via Report"


def _dispatch_api_call(artifact, task) -> Tuple[bool, str, str]:
    """API call artifacts already executed during Execute; Deliver is
    a no-op — the receipt handle echoes the original."""
    handle = artifact.handle or f"api:{artifact.id}"
    return True, handle, "api call already executed in Execute"


def _dispatch_unknown(artifact, task) -> Tuple[bool, str, str]:
    return False, "", f"unknown artifact kind {artifact.kind!r}"


_DISPATCH: dict[str, Callable] = {
    "file":      _dispatch_file,
    "email":     _dispatch_email,
    "rag":       _dispatch_rag,
    "rag_entry": _dispatch_rag,
    "message":   _dispatch_message,
    "api_call":  _dispatch_api_call,
}


__all__ = ["deliver_artifact"]
