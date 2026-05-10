"""V4 step 2: expert inference pipeline.

When an agent's `expert_specialty` is non-empty, agent.chat() routes
through this module's `answer()` function (per the hook recovered in
the main session).

Flow:
  1. Load corpus chunks from ~/.tudou_claw/expert/<id>/corpus/*/chunks.jsonl
  2. Simple keyword retrieval (V3 step 3 will swap in bge-m3 + vss)
  3. Build a system prompt with retrieved context
  4. Call agent's LLM via app.llm.chat_no_stream
  5. Capture organic trace to traces/YYYY-MM-DD.jsonl
  6. Return the answer string

This makes the cultivation system self-feeding: each Q/A cycle adds
one trace, which (when followed by user 👍/👎 feedback) drives 段位
progression through _cultLevelFromStats.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from .. import _config as _expert_config

logger = logging.getLogger("tudouclaw.expert.pipeline")


# ─────────────────────────────────────────────────────────────────────
# Public entry point — called by agent.chat() reply hook
# ─────────────────────────────────────────────────────────────────────


def answer(agent, user_message, *, on_event: Any = None,
           abort_check: Any = None, source: str = "admin",
           context_id: str = "solo") -> str:
    """RAG-augmented expert reply.

    Returns: str (matches agent.chat() return type so the hook can
    `return _expert_pipeline.answer(...)` directly).

    Falls back to a clear error message rather than raising — the
    recovered hook in agent.py wraps this in try/except, but the
    user sees the message in chat either way, so we return graceful
    text instead of letting exceptions bubble.
    """
    # Normalize user_message — could be str or multimodal list
    if isinstance(user_message, list):
        text = " ".join(
            (p.get("text") or "")
            for p in user_message
            if isinstance(p, dict) and p.get("type") == "text"
        ).strip()
    else:
        text = str(user_message or "").strip()

    if not text:
        return "(空消息)"

    # ── Step 1+2: retrieve top-K chunks ──
    chunks = _retrieve(agent.id, text, k=5)

    # ── Step 3: build system prompt with retrieved context ──
    specialty = getattr(agent, "expert_specialty", "") or "?"
    if chunks:
        context_block = "\n\n".join(
            f"[来源: {c['source_id']}]\n{c['text']}"
            for c in chunks
        )
        sys_prompt = (
            f"你是 {agent.name},一个 {specialty} 领域专家 agent。\n"
            "请基于下面检索到的内部资料回答用户的问题。\n"
            "要求:\n"
            "  1. 优先采用检索到的资料,引用来源用 [来源: source_id] 格式\n"
            "  2. 检索资料未覆盖的部分,可以基于通用知识回答,但要明确标注 (通用知识)\n"
            "  3. 回答要准确、有据,不要编造\n\n"
            f"=== 检索到 {len(chunks)} 段资料 ===\n{context_block}"
        )
    else:
        sys_prompt = (
            f"你是 {agent.name},一个 {specialty} 领域专家 agent。\n"
            "当前内部知识库未检索到与本问题相关的资料。\n"
            "请基于你已有的通用知识回答,但需在开头明确告知:\n"
            "  '⚠️ 内部知识库未覆盖此问题,以下基于通用知识作答。'"
        )

    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user",   "content": text},
    ]

    # ── Step 4: call LLM ──
    answer_text = ""
    try:
        from app import llm
        resp = llm.chat_no_stream(
            messages,
            provider=getattr(agent, "provider", "") or "",
            model=getattr(agent, "model", "") or "",
        )
        msg = (resp or {}).get("message") or {}
        answer_text = (msg.get("content") or "").strip()
    except Exception as e:
        logger.exception("expert pipeline LLM call failed for %s", agent.id)
        answer_text = (
            f"⚠️ 养成系统调用 LLM 失败: {e}\n"
            "你的提问已捕获到 trace 池,但暂时无法回答。请检查 agent 的 provider/model 配置。"
        )

    # ── Step 5: capture organic trace ──
    try:
        _write_trace(agent.id, {
            "ts": time.time(),
            "q": text[:500],
            "a": answer_text,
            "retrieved_count": len(chunks),
            "retrieved_sources": sorted({c["source_id"] for c in chunks}),
            "origin": "organic",
            "source": source,
            "context_id": context_id,
            "specialty": specialty,
        })
    except Exception as e:
        logger.warning("trace write failed for %s: %s", agent.id, e)

    return answer_text


# ─────────────────────────────────────────────────────────────────────
# Internals: retrieval + tokenization + trace persistence
# ─────────────────────────────────────────────────────────────────────


def _retrieve(agent_id: str, query: str, k: int = 5) -> list[dict]:
    """Keyword retrieval (V4 step 2 minimal — V3 step 3 replaces this
    with bge-m3 embedding + sqlite-vss).

    Loads chunks from every ~/.tudou_claw/expert/<id>/corpus/<src>/chunks.jsonl,
    scores by token overlap with `query`, returns top-k.
    """
    corpus_dir = os.path.join(_expert_config.expert_dir_for(agent_id), "corpus")
    if not os.path.isdir(corpus_dir):
        return []
    q_tokens = _tokenize(query)
    if not q_tokens:
        return []
    candidates: list[dict] = []
    try:
        source_dirs = sorted(os.listdir(corpus_dir))
    except OSError:
        return []
    for source_id in source_dirs:
        chunks_jsonl = os.path.join(corpus_dir, source_id, "chunks.jsonl")
        if not os.path.isfile(chunks_jsonl):
            continue
        try:
            with open(chunks_jsonl, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    chunk_text = rec.get("text", "")
                    c_tokens = _tokenize(chunk_text)
                    overlap = len(q_tokens & c_tokens)
                    if overlap > 0:
                        candidates.append({
                            "source_id": source_id,
                            "text": chunk_text,
                            "score": overlap,
                            "metadata": rec.get("metadata", {}),
                        })
        except OSError:
            continue
    candidates.sort(key=lambda x: -x["score"])
    return candidates[:k]


def _tokenize(s: str) -> set:
    """Crude tokenizer: ASCII words + Chinese unigrams + bigrams.

    Good enough for V4 step 2 keyword overlap. V3 step 3 swaps the
    whole retrieval path for embedding-based similarity, at which
    point tokenization moves into the embedder.
    """
    if not s:
        return set()
    s = s.lower()
    tokens: set[str] = set()
    # ASCII words (alphanumeric chunks)
    tokens.update(re.findall(r"[a-z0-9]+", s))
    # Chinese chars: collect both unigrams and bigrams
    cjk = [c for c in s if "一" <= c <= "鿿"]
    tokens.update(cjk)
    for i in range(len(cjk) - 1):
        tokens.add(cjk[i] + cjk[i + 1])
    return tokens


def _write_trace(agent_id: str, trace: dict) -> None:
    """Append trace to today's JSONL file."""
    traces_dir = os.path.join(_expert_config.expert_dir_for(agent_id), "traces")
    os.makedirs(traces_dir, exist_ok=True)
    fname = time.strftime("%Y-%m-%d") + ".jsonl"
    fp = os.path.join(traces_dir, fname)
    with open(fp, "a", encoding="utf-8") as f:
        f.write(json.dumps(trace, ensure_ascii=False) + "\n")
