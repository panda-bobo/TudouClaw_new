"""Phase: task decomposition draft (small-model first-pass).

Bridge contract: ``run(agent, model, payload, timeout_s) ->
(value, tokens_in, tokens_out)``.

Payload shape:
    {
        "intent": "<the user task to decompose>",
        "n": 4,                                    # target sub-task count (default 4)
        "context": "<optional extra context>",     # e.g. PRD excerpt
    }

Returns:
    {
        "sub_tasks": [
            {"id": "s1", "title": "...", "role_hint": "researcher",
             "output_path": "outputs/research.md", "depends_on": []},
            ...
        ],
        "rationale": "<1-line note from small model>",
    }

Why a structured pipe-delimited format (not JSON):
  3B-class models are unreliable at strict JSON. Pipe-delimited lines are
  forgiving — partial output still parses, missing fields fall back to
  defaults. We sacrifice strict typing for resilience.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from ._client import chat_completion

logger = logging.getLogger("tudou.preprocessing.decompose")


# Single-turn user prompt — small models reliable with this style.
_USER_PROMPT_TEMPLATE_ZH = (
    "把下面的任务拆解成 {n} 个 sub-task，每行一条，格式严格如下：\n"
    "  ID | 标题 | 角色 | 输出路径 | 依赖（可选，逗号分隔的 ID）\n"
    "\n"
    "角色 ∈ researcher | coder | designer | writer | general\n"
    "输出路径示例: outputs/research.md / src/auth.py\n"
    "依赖列前面 sub-task 的 ID。\n"
    "\n"
    "## 示例输出（不要复制内容，只学格式）\n"
    "1 | 调研竞品 | researcher | outputs/competitors.md |\n"
    "2 | 撰写大纲 | writer | outputs/outline.md | 1\n"
    "3 | 起草报告 | writer | outputs/draft.md | 1,2\n"
    "\n"
    "## 任务\n"
    "{intent}\n"
    "{context_block}"
    "\n"
    "## 你的拆解（只输出 {n} 行格式化数据，不要解释）"
)


_VALID_ROLES = {"researcher", "coder", "designer", "writer", "general", "advisor"}


def _parse_subtasks(text: str, *, n_expected: int) -> list[dict]:
    """Parse pipe-delimited lines into sub-task dicts.

    Tolerant: skips lines that don't have at least 3 pipe-separated fields,
    fills missing role_hint/output_path with sane defaults, normalises
    depends_on into a list of stable IDs.
    """
    out: list[dict] = []
    if not text:
        return out
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("/"):
            continue
        # Strip leading bullet markers ("- ", "* ", "• ", numerals)
        line = re.sub(r"^[-*•]\s+", "", line)
        # Split on pipe (allow surrounding spaces)
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        # Field 0 = ID (allow numeric like "1" or string like "s1")
        sid = parts[0].lstrip("sS") if parts[0] else ""
        sid = re.sub(r"[^\w]", "", sid) or f"s{len(out)+1}"
        if not sid.startswith("s"):
            sid = "s" + sid
        title = parts[1] if len(parts) > 1 else ""
        if not title:
            continue
        role_hint = (parts[2] if len(parts) > 2 else "").strip().lower()
        if role_hint not in _VALID_ROLES:
            role_hint = "general"
        output_path = parts[3].strip() if len(parts) > 3 else ""
        depends_on_raw = parts[4].strip() if len(parts) > 4 else ""
        depends_on: list[str] = []
        if depends_on_raw:
            for d in re.split(r"[,，\s]+", depends_on_raw):
                d = d.strip().lstrip("sS")
                if d and re.match(r"^\d+$", d):
                    depends_on.append(f"s{d}")
                elif d:
                    depends_on.append(d if d.startswith("s") else f"s{d}")
        out.append({
            "id": sid,
            "title": title[:80],
            "role_hint": role_hint,
            "output_path": output_path[:200],
            "depends_on": depends_on,
        })
        if len(out) >= n_expected + 2:
            # Allow small overshoot; cut hard if model went wild
            break
    return out


def run(*, agent, model: str, payload: dict, timeout_s: float = 10.0):
    intent = (payload or {}).get("intent") or ""
    if not intent:
        return {"sub_tasks": [], "rationale": "no intent"}, 0, 0
    n = max(2, min(int((payload or {}).get("n") or 4), 8))
    context = ((payload or {}).get("context") or "").strip()

    endpoint = getattr(agent, "preprocessor_endpoint", "") or ""

    context_block = ""
    if context:
        # Cap context — small model can't handle huge PRDs efficiently
        context_block = f"\n## 额外背景\n{context[:1500]}\n"

    user_msg = _USER_PROMPT_TEMPLATE_ZH.format(
        n=n, intent=intent[:500], context_block=context_block,
    )
    messages = [{"role": "user", "content": user_msg}]

    content, tin, tout = chat_completion(
        endpoint=endpoint,
        model=model,
        messages=messages,
        temperature=0.0,
        # Output is N lines * ~80 chars = ~640 chars / ~160 tokens.
        # 256 gives headroom without over-allowing rambles.
        max_tokens=max(200, n * 80),
        timeout_s=timeout_s,
    )

    sub_tasks = _parse_subtasks(content or "", n_expected=n)
    if not sub_tasks:
        # Bridge interprets empty result as "no benefit, fall back"
        logger.debug(
            "decompose: model returned 0 parseable sub-tasks (raw len=%d); "
            "falling through to original path",
            len(content or ""),
        )
        return {
            "sub_tasks": [],
            "rationale": "small model output unparseable, fell through",
            "raw": content[:200] if content else "",
        }, tin, tout

    return {
        "sub_tasks": sub_tasks,
        "rationale": f"draft from {model.split(':')[0]}",
    }, tin, tout
