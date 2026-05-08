"""Knowledge / learning tools — save_experience, knowledge_lookup,
share_knowledge, learn_from_peers.

All four read/write either the role-based Experience Library or the
shared Knowledge Base, and need to resolve caller-agent attribution
through the hub.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from .. import knowledge as _knowledge
from ._common import _get_hub

logger = logging.getLogger(__name__)


# ── Query-intent classifier (HCS RAG-inspired, 2026-05-05) ────────────
#
# Detects whether the user's RAG query is asking for an aggregate
# count / overview, vs. detailed content. Used by knowledge_lookup to
# auto-route ``mode="search"`` to ``mode="outline"`` when the user
# clearly wants a roll-up — saves the agent from doing 5+ wrong
# vector-search calls before figuring out it should be counting.
#
# Conservative: false-negatives (failing to route) just keep the
# original behaviour. False-positives (wrongly routing detail
# questions to outline) are bad — that's why we have the detail-keyword
# blocker as Phase 2.

# Strong patterns that almost always indicate aggregate intent.
_STAT_PATTERNS = [
    re.compile(r"(?:每个|各个|各).*(?:多少|数量|几个|个数)"),     # "每个云服务有多少..."
    re.compile(r"(?:多少|几个)\s*个?\s*(?:用例|测试|服务|文档|场景|章节|条目|案例)"),
    re.compile(r"(?:包含|含有|有)\s*多少"),
    re.compile(r"统计.*?(?:用例|服务|文档|场景|案例|条数|数量)"),
    re.compile(r"(?:用例|服务|文档|场景|案例)\s*(?:数量|个数|总数|多少)"),
    re.compile(r"(?:总共|一共|合计)\s*(?:有|多少|几个)"),
]

# Weaker keywords; require ≥2 to trigger overview mode by themselves.
_OVERVIEW_KEYWORDS = (
    "包含", "哪些", "有什么", "总结", "概述", "总共",
    "全部", "所有", "目录", "清单", "汇总", "统计",
    "覆盖", "涵盖", "什么内容", "什么文档", "内容概览",
)

# Single-keyword strong signals: stat keyword + any overview keyword → stat.
_STAT_KEYWORDS = ("统计", "多少个", "总共", "一共", "数量", "个数", "多少")

# Detail signals — block stat classification (user wants the actual content).
_DETAIL_KEYWORDS = (
    "详细", "具体", "介绍", "说明", "描述", "每个的内容",
    "分别是什么", "怎么做", "如何", "步骤", "用例内容",
    "列出内容", "完整列出",
)

# Detail patterns that should override stat patterns (e.g. "9 个用例分别是什么").
_DETAIL_PATTERNS = [
    re.compile(r"\d+\s*个.*(?:是什么|有哪些|分别|具体)"),
    re.compile(r"(?:详细|具体).*(?:说明|介绍|描述|列出)"),
    re.compile(r"(?:每个|每一个).*(?:内容|步骤|用例|是什么)"),
    re.compile(r"(?:介绍|说明).*(?:内容|用例|步骤|流程)"),
]


# ─── Domain inference (Phase 3 / Domain Step 4) ──────────────────────
# Lightweight keyword-based mapping from query text → domain tags.
# Defensive vocabulary — keeps the LIST of domains identical to the
# wiki layer so search() boosts cleanly. Multi-domain queries are
# allowed (e.g. "PCI compliance audit" → security + payments-compliance).
#
# Cost: zero LLM calls (pure regex / membership check). Misses (no
# keyword match) → empty list → search behaves exactly as before.
_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "security": (
        "安全", "漏洞", "vuln", "exploit", "auth", "认证", "授权",
        "csrf", "xss", "sql injection", "渗透", "pen test",
        "secret", "credential", "api key", "ssh key",
    ),
    "payments-compliance": (
        "pci", "支付", "信用卡", "credit card", "payment", "compliance",
        "合规", "audit", "审计", "saq",
    ),
    "regulatory": (
        "gdpr", "soc2", "soc 2", "hipaa", "iso27001", "regulator",
        "regulation", "法规", "监管",
    ),
    "project-management": (
        "milestone", "里程碑", "项目管理", "pm", "scrum", "kanban",
        "sprint", "敏捷", "复盘", "retrospective", "ooda", "wbs",
        "gantt", "甘特",
    ),
    "frontend": (
        "frontend", "react", "vue", "css", "html", "tailwind",
        "responsive", "ui", "ux", "前端",
    ),
    "backend": (
        "backend", "api", "rest", "graphql", "database", "数据库",
        "sql", "orm", "redis", "cache", "后端",
    ),
    "devops": (
        "deploy", "deployment", "ci", "cd", "docker", "kubernetes",
        "k8s", "terraform", "ansible", "monitoring", "prometheus",
        "grafana", "运维", "部署",
    ),
    "testing": (
        "test", "testing", "unit test", "integration test", "e2e",
        "fixture", "mock", "pytest", "jest", "测试",
    ),
    "data-analysis": (
        "etl", "pipeline", "spark", "pandas", "dataframe", "sql query",
        "数据分析", "可视化", "visualization", "dashboard",
    ),
    "writing-content": (
        "blog", "article", "文档", "documentation", "seo", "marketing copy",
        "营销文案", "技术写作",
    ),
    "marketing": (
        "marketing", "promotion", "营销", "推广", "gumroad", "marketplace",
    ),
    "design": (
        "design", "figma", "sketch", "mockup", "设计", "色彩",
    ),
    "customer-support": (
        "客服", "support ticket", "退款", "refund", "complaint", "投诉",
    ),
    "legal-compliance": (
        "contract", "合同", "intellectual property", "ip", "知识产权",
        "privacy policy", "隐私协议", "tos", "条款",
    ),
}


def _infer_domains_from_query(query: str) -> list[str]:
    """Cheap keyword-based domain classifier. Returns 0-N domains.

    Used by knowledge_lookup to boost wiki hits whose ``domains``
    intersect what the user is asking about. Falls back to empty
    list (= no boost) for queries that don't match any domain
    vocabulary.

    Short keywords (≤3 ASCII chars like "ci", "cd", "ip") use
    word-boundary regex to avoid bogus matches inside larger words
    ("PCI" containing "ci" → falsely tagged devops). Chinese /
    longer English keywords use plain substring match (Chinese
    doesn't have word boundaries; longer English words rarely
    embed inside other words).
    """
    if not query or not isinstance(query, str):
        return []
    import re as _re_dom
    q_lower = query.lower()
    matched: list[str] = []
    for domain, kws in _DOMAIN_KEYWORDS.items():
        hit = False
        for kw in kws:
            if not kw:
                continue
            if len(kw) <= 3 and kw.isascii():
                # Word boundary — avoid "ci" matching "PCI"
                if _re_dom.search(rf"\b{_re_dom.escape(kw)}\b", q_lower):
                    hit = True
                    break
            else:
                if kw in q_lower:
                    hit = True
                    break
        if hit:
            matched.append(domain)
    return matched


def _classify_rag_intent(query: str) -> str:
    """Return one of: 'stat' (overview/aggregate), 'detail' (specific
    content asked), 'normal' (default — don't reroute).

    Order matters:
      1. Strong stat patterns — short-circuit to 'stat'
      2. Stat keyword + overview keyword combo → 'stat'
      3. Detail keywords / patterns — short-circuit to 'detail'
         (means: even if a stat keyword leaks in, the user wants content)
      4. ≥2 overview keywords → 'stat'
      5. Otherwise → 'normal'
    """
    if not query or not isinstance(query, str):
        return "normal"
    q = query.strip()
    # Phase 1: strong stat patterns.
    for pat in _STAT_PATTERNS:
        if pat.search(q):
            return "stat"
    # Phase 2: stat keyword + ≥1 overview keyword combo.
    has_stat = any(kw in q for kw in _STAT_KEYWORDS)
    overview_count = sum(1 for kw in _OVERVIEW_KEYWORDS if kw in q)
    if has_stat and overview_count >= 1:
        return "stat"
    # Phase 3: detail blocks override.
    for kw in _DETAIL_KEYWORDS:
        if kw in q:
            return "detail"
    for pat in _DETAIL_PATTERNS:
        if pat.search(q):
            return "detail"
    # Phase 4: 2+ overview keywords.
    if overview_count >= 2:
        return "stat"
    return "normal"


# Prelude prepended to every knowledge_lookup result. Reminds the LLM
# of four rules that previously caused user-visible quality issues:
#   1. Don't fall back to "see original doc" / "refer to source" —
#      those land as "knowledge base is broken" complaints.
#   2. Tables: keep ALL rows and columns; don't summarize away cells.
#   3. Cite the source_file + heading_path, not just "chunk N".
#   4. When multiple chunks cover the same topic, integrate before
#      answering (don't pick one and ignore the rest).
_RAG_RESULT_PRELUDE = (
    "[knowledge_lookup protocol]\n"
    "Retrieval results follow. Answer directly from this content. Hard rules:\n"
    "1. **NEVER** reply with \"please consult the document\" / \"refer to the "
    "original\" / \"see the source for details\" — you **must** answer from "
    "the retrieved content. If information is incomplete, give what you have "
    "and then state explicitly what is missing.\n"
    "2. Tables: render **all rows and columns** as a Markdown table. Do not "
    "elide cells with \"...\" / \"etc.\" / \"and so on\".\n"
    "3. Cite as `[source_file §heading_path]`, not \"chunk 1\".\n"
    "4. When multiple chunks cover the same topic from different angles, "
    "**synthesize** before answering — don't quote one chunk and drop the "
    "rest.\n\n"
)


# knowledge_lookup: max results per RAG tier before merging.
# v2 (Pack): raised 5 → 8 → 15.
# Real usage on a 7000-chunk multi-document KB (Huawei Cloud Stack
# acceptance docs, 2026-05-04) showed top-8 only surfaced ~4-5
# unique source files even when the user wanted a roll-up across
# 7+ docs. Aggregate-style queries ("how many?", "list all…")
# benefit hugely from a wider net before reranking. Top-15 keeps
# the LLM context manageable (15 × 1500 char cap = ~22 KB) while
# letting the cross-encoder rerank correctly resort by relevance.
_RAG_TOP_K = 15

# v2 (Pack): cap each RAG chunk's content at this many chars when
# packing into tool_result. Protects LLM context from anomalous
# giant chunks. Typical chunk is ~800 chars so this barely clips.
_MAX_CHUNK_CHARS_PER_HIT = 1500

# knowledge_lookup: cap on partial-match list returned to the LLM when
# there's no exact title match (legacy Shared-pool code path only).
_PARTIAL_MATCHES_CAP = 20

# learn_from_peers: clamp user-supplied limit to this range.
_LEARN_LIMIT_MIN = 1
_LEARN_LIMIT_MAX = 20


# ── Per-turn semantic query dedup ────────────────────────────────────
# Agents often call memory_recall / knowledge_lookup 4-5 times in one turn
# with tiny query variations ("海外共享中心 业务规划" vs "海外云共享服务中心
# 业务规划 共享中心") — the same-signature detector requires EXACT match so
# these slip through. Each call hits ChromaDB + embedding model (slow),
# so the UI spins at ⏳ while the agent fires yet another variant.
#
# Solution: short-circuit at the tool entry. Track normalized queries in
# a per-agent-per-turn bucket; if a new query has char-3gram Jaccard ≥
# 0.75 with any prior query in the same turn, return the previous result
# verbatim with a note.

def _norm_query(q: str) -> str:
    """Lowercase + strip spaces/punct; keep CJK + alphanum."""
    import re as _re
    return _re.sub(r"[^\w\u4e00-\u9fa5]+", "", (q or "").lower())


def _query_similarity(a: str, b: str) -> float:
    """Char-3gram Jaccard similarity of normalized queries (0-1)."""
    na, nb = _norm_query(a), _norm_query(b)
    if len(na) < 3 or len(nb) < 3:
        return 1.0 if na == nb else 0.0
    ga = {na[i:i+3] for i in range(len(na) - 2)}
    gb = {nb[i:i+3] for i in range(len(nb) - 2)}
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


_MAX_SAME_TOOL_PER_TURN = 5  # hard cap, beyond this we refuse

# Per-tool strict caps (Nov 2026): memory_recall / knowledge_lookup should
# be called EXACTLY ONCE per turn with the widest possible query.
# Calling them repeatedly with variant keywords burns tokens without new info.
_STRICT_ONE_SHOT = frozenset({"memory_recall", "knowledge_lookup"})

# ── Exploration-group budget ──
# memory_recall / knowledge_lookup / web_search / glob_files are all
# "look around" tools. The LLM treats them as independent budgets, but
# from the USER's perspective they're the same "I'm still exploring" bucket.
# So we count them TOGETHER and show the LLM a unified view via
# [TOOL_CALL_STATE]. At 5 total cross-tool calls, all start returning
# a "stop exploring, start delivering" banner.
_EXPLORATION_TOOLS = frozenset({
    "memory_recall", "knowledge_lookup", "web_search", "glob_files",
})
_EXPLORATION_HARD_CAP = 5


def _build_tool_call_state(agent, tool_name: str, current_query: str) -> str:
    """Produce a [TOOL_CALL_STATE] banner to prepend to the tool_result.

    Makes the LLM see:
      - How many times it has called exploration tools this turn
      - What queries it has already tried (any tool in the exploration group)
      - Whether it's over budget

    Returns "" if no state to show (first call, or agent unavailable).
    """
    if agent is None:
        return ""
    cache = getattr(agent, "_turn_query_cache", None)
    if cache is None:
        return ""

    # Count exploration calls across all tools in the group
    total = 0
    queries: list[str] = []
    for tn in _EXPLORATION_TOOLS:
        bucket = cache.get(tn) or []
        total += len(bucket)
        for q, _ in bucket:
            if q:
                queries.append(q)
    if total == 0 and not current_query:
        return ""

    # Deduplicate queries by normalized form for display
    seen: set = set()
    uniq_queries: list[str] = []
    for q in queries:
        nq = _norm_query(q)
        if nq in seen:
            continue
        seen.add(nq)
        uniq_queries.append(q[:40])

    remaining = max(0, _EXPLORATION_HARD_CAP - total)
    lines = [
        "[TOOL_CALL_STATE]",
        f"- exploration_calls_this_turn: {total}  (memory_recall + knowledge_lookup + web_search + glob_files)",
    ]
    if uniq_queries:
        # Show up to 8 unique queries — enough for LLM to see the pattern
        shown = uniq_queries[-8:]
        lines.append(f"- queries_so_far: {shown}")
    lines.append(f"- budget_remaining: {remaining} (hard cap {_EXPLORATION_HARD_CAP})")
    if total >= _EXPLORATION_HARD_CAP:
        lines.append("")
        lines.append("[DECISION]")
        lines.append("你已到达探查预算硬上限。立即停止信息搜集,基于已有结果开始产出:")
        lines.append("  (1) 如果信息足够 → 直接写交付物 (write_file)")
        lines.append("  (2) 如果不够    → 用一句话告诉用户缺什么,不要再调工具")
    elif total >= 3:
        lines.append("")
        lines.append("[DECISION]")
        lines.append(f"你已调用 {total} 次探查工具。再看一眼 queries_so_far:")
        lines.append("  - 如果关键词重叠度高 → 换明显不同的角度,或直接停止搜集开始产出")
        lines.append("  - 如果每次都是近义词换排列组合 → 这是死循环, 必须停下")
    lines.append("")
    return "\n".join(lines)


def _check_turn_dedup(agent, tool_name: str, query: str,
                       threshold: float = 0.45,
                       mode: str = "") -> str | None:
    """If ``query`` is >threshold similar to a prior query of the same
    tool in the current turn, return the cached result string (with a
    note prepended). Also enforces _MAX_SAME_TOOL_PER_TURN hard cap —
    beyond 5 calls the tool refuses to run in the same turn.

    Bucket is cleared at turn start by agent.chat() via `_turn_query_cache = {}`.
    """
    if agent is None or not query:
        return None
    cache = getattr(agent, "_turn_query_cache", None)
    if cache is None:
        cache = {}
        try:
            setattr(agent, "_turn_query_cache", cache)
        except Exception:
            return None
    bucket = cache.setdefault(tool_name, [])

    # Strict one-shot tools: memory_recall / knowledge_lookup must be
    # called EXACTLY ONCE per turn — UNLESS the next call uses a
    # genuinely different ``mode`` (search / count / list / outline).
    # Different modes return structurally different data (top-k chunks
    # vs whole-KB stats vs structural outline), so re-calling them is
    # information-positive, not token-burning.
    #
    # We track which modes have been used this turn under a parallel
    # cache key. If the new call's mode is one we haven't seen yet, it
    # passes the one-shot gate (still subject to MAX_SAME_TOOL_PER_TURN
    # hard cap below). Same-mode re-calls remain blocked.
    if tool_name in _STRICT_ONE_SHOT and len(bucket) >= 1:
        modes_seen = cache.setdefault("_modes_" + tool_name, set())
        if mode and mode not in modes_seen:
            # Different mode → allow. The mode gets registered AFTER the
            # call succeeds so concurrent invokes don't slip through;
            # _cache_turn_result will add it. For now we just don't reject.
            pass
        else:
            prev_q, prev_result = bucket[0]
            mode_hint = ""
            if tool_name == "knowledge_lookup":
                modes_left = ", ".join(sorted({"search","count","list","outline"} - modes_seen))
                if modes_left:
                    mode_hint = (
                        f"\n💡 想再调一次?换不同的 ``mode`` "
                        f"(已用 {sorted(modes_seen) or ['search']},还可以用 {modes_left})。"
                        f"不同 mode 返回不同数据,不算重复。"
                    )
            return (
                f"[ONE_SHOT_VIOLATION — {tool_name} 本轮已用 mode={mode!r} 调过]\n"
                f"**规则**: 同一 mode 一轮只能调一次。同 mode 重复调用"
                f"不会带来新数据,只会烧 token。\n"
                f"你第一次的查询是: {prev_q!r}\n"
                f"如果当时关键词没想全,只能基于已有结果继续;不要换近义词重试。"
                f"{mode_hint}\n\n"
                f"--- 首次查询结果(必须复用) ---\n{prev_result}"
            )

    # Hard cap: other read-only tools called too many times this turn.
    if len(bucket) >= _MAX_SAME_TOOL_PER_TURN:
        return (
            f"[RATE_LIMIT — {tool_name} already ran {len(bucket)} times "
            f"this turn, hard cap reached]\n"
            f"你这一轮调用 `{tool_name}` 已达 {_MAX_SAME_TOOL_PER_TURN} 次,"
            f"继续调用**不会有新信息**。请立即:\n"
            f"  (a) 基于已掌握的信息直接交付(写文档 / 给结论),\n"
            f"  (b) 或明确告知用户信息不足,需要 TA 补充。\n"
            f"已查过的关键词: {[q[:20] for q, _ in bucket[-5:]]}"
        )

    for prev_q, prev_result in bucket:
        sim = _query_similarity(prev_q, query)
        if sim >= threshold:
            return (
                f"[DUPLICATE_QUERY within same turn — similarity={sim:.2f} "
                f"to earlier query {prev_q!r}]\n"
                f"Reusing previous result to save tokens. 如果需要不同角度,"
                f"请换**明显不同的关键词**(不要只加/减词;相似度 <0.45),或"
                f"直接基于已有结果往下做;不要再用近义词重试同一查询。\n\n"
                f"--- previous result ---\n{prev_result}"
            )
    return None


def _cache_turn_result(agent, tool_name: str, query: str, result: str,
                        mode: str = ""):
    """Record (query, result) in this turn's bucket."""
    if agent is None or not query:
        return
    cache = getattr(agent, "_turn_query_cache", None)
    if cache is None:
        cache = {}
        try:
            setattr(agent, "_turn_query_cache", cache)
        except Exception:
            return
    bucket = cache.setdefault(tool_name, [])
    bucket.append((query, result))
    # Cap per-tool bucket to avoid unbounded growth
    if len(bucket) > 10:
        del bucket[:-10]
    # Track which modes have been used this turn — enables "same tool,
    # different mode" to bypass the one-shot lock.
    if mode:
        modes_seen = cache.setdefault("_modes_" + tool_name, set())
        modes_seen.add(mode)


def _wrap_with_state(agent, tool_name: str, query: str, result: str) -> str:
    """Prepend a [TOOL_CALL_STATE] banner to the tool result so the LLM
    sees cross-tool exploration count and budget. Idempotent — if result
    already starts with [TOOL_CALL_STATE], return unchanged.
    """
    if not isinstance(result, str):
        return result
    if result.startswith("[TOOL_CALL_STATE]") or result.startswith("[DUPLICATE_QUERY") \
            or result.startswith("[RATE_LIMIT"):
        return result
    banner = _build_tool_call_state(agent, tool_name, query)
    if not banner:
        return result
    return banner + result


# ── memory_recall (新 A.3) ──────────────────────────────────────────
# Agent-PRIVATE L3 semantic memory lookup. Deliberately separate from
# knowledge_lookup (which hits the ROLE-wide shared knowledge base):
# memory_recall is "did I personally encounter this before, what did I
# conclude?" — use it BEFORE web_search / re-analysis to avoid
# re-spending tokens on a topic this agent has already figured out.

def _tool_memory_recall(query: str = "",
                        category: str = "",
                        top_k: int = 5,
                        **_: Any) -> str:
    """Query the calling agent's private L3 semantic memory.

    Returns a compact, ranked list of prior facts/conclusions/rules
    this agent recorded. Use it as the FIRST step on any topic: if a
    recent high-confidence memory covers it, you can skip web_search
    and go straight to action.
    """
    caller_id = _.get("_caller_agent_id", "") if isinstance(_, dict) else ""
    if not caller_id:
        return "Error: memory_recall requires a calling agent context."
    q = (query or "").strip()
    if not q:
        return "Error: query is required (what are you trying to remember?)."

    # Per-turn dedup: if user already ran a near-identical query this turn,
    # reuse. Stops the "海外共享中心 业务规划" ↔ "海外云共享服务中心 业务规划
    # 共享中心" loop.
    hub = _get_hub()
    _agent = hub.get_agent(caller_id) if hub else None
    logger.info(
        "memory_recall dedup check: hub=%s agent=%s caller_id=%s q=%r "
        "bucket_size=%s",
        bool(hub), bool(_agent), caller_id[:8],
        q[:40],
        (len(getattr(_agent, "_turn_query_cache", {}).get("memory_recall", []))
         if _agent is not None else "N/A"),
    )
    _cached = _check_turn_dedup(_agent, "memory_recall", q)
    if _cached is not None:
        logger.info("memory_recall dedup HIT → returning cached")
        return _cached
    logger.info("memory_recall dedup MISS → proceeding to real query")

    try:
        from ..core.memory import get_memory_manager as _get_mm
        mm = _get_mm()
    except Exception as e:
        return f"Error: memory manager unavailable ({e})"
    if mm is None:
        return "Error: memory manager not initialized."

    try:
        k = max(1, min(int(top_k or 5), 20))
    except Exception:
        k = 5
    # Phase 3 P3-5 (2026-05-06): pass current scenario's scope so
    # cross-project memory doesn't leak through this read path.
    _cur_pid = ""
    _cur_tid = ""
    try:
        if _agent is not None:
            _sc = getattr(_agent, "current_scenario", None)
            if _sc is not None:
                _cur_pid = getattr(_sc, "project_id", "") or ""
                _cur_tid = getattr(_sc, "task_id", "") or ""
            if not _cur_pid:
                _cur_pid = getattr(_agent, "project_id", "") or ""
    except Exception:
        pass
    try:
        hits = mm.recall(caller_id, q, top_k=k,
                         category=(category or "").strip(),
                         current_project_id=_cur_pid,
                         current_task_id=_cur_tid)
    except Exception as e:
        return f"Error recalling memory: {e}"

    # ── 新 A.7: 把本轮用到的记忆 id 记在 agent 上，供响应合成时
    # 挂到 assistant 消息，UI 能渲染 🧠 badge + 删除按钮。
    # (hub/_agent already resolved above for the dedup check, reuse.)
    try:
        if _agent is not None:
            bucket = getattr(_agent, "_turn_memory_refs", None)
            if bucket is None:
                bucket = []
                setattr(_agent, "_turn_memory_refs", bucket)
            for h in hits:
                fid = h.get("id") or ""
                if not fid:
                    continue
                if any(r.get("id") == fid for r in bucket):
                    continue
                bucket.append({
                    "id": fid,
                    "category": h.get("category", ""),
                    "content_preview": (h.get("content") or "").strip()[:200],
                    "confidence": h.get("confidence", 1.0),
                    "age_days": h.get("age_days", 0),
                    "source": h.get("source", "") or "",
                })
    except Exception as _trk_err:
        logger.debug("memory_recall bucket tracking skipped: %s", _trk_err)

    if not hits:
        _no_hit = (
            f"No prior memory found for query '{q[:60]}'. "
            "You'll need to explore fresh — consider web_search or "
            "knowledge_lookup, and save_experience afterward so the "
            "next turn can skip it."
        )
        _cache_turn_result(_agent, "memory_recall", q, _no_hit)
        return _wrap_with_state(_agent, "memory_recall", q, _no_hit)

    lines = [f"Memory recall for '{q[:80]}' — {len(hits)} hit(s):"]
    for i, h in enumerate(hits, 1):
        cat = h.get("category", "general")
        age = h.get("age_days", 0)
        conf = h.get("confidence", 1.0)
        content = (h.get("content") or "").strip().replace("\n", " ")
        if len(content) > 260:
            content = content[:260] + "…"
        src = h.get("source") or ""
        if src and len(src) > 40:
            src = src[:40] + "…"
        lines.append(
            f"  [{i}] ({cat}) conf={conf:.2f}  age={age}d  id={h.get('id','')[:8]}"
        )
        lines.append(f"       {content}")
        if src:
            lines.append(f"       (source: {src})")
    lines.append("")
    lines.append(
        "💡 Use these prior conclusions if relevant. If a memory looks "
        "out-of-date or wrong, run your own verification and then "
        "save_experience with the corrected finding — it'll refresh the old one."
    )
    _result = "\n".join(lines)
    _cache_turn_result(_agent, "memory_recall", q, _result)
    return _wrap_with_state(_agent, "memory_recall", q, _result)


# ── save_experience ──────────────────────────────────────────────────

def _tool_save_experience(
    scene: str,
    core_knowledge: str,
    action_rules: list[str] | None = None,
    taboo_rules: list[str] | None = None,
    priority: str = "medium",
    tags: list[str] | None = None,
    exp_type: str = "retrospective",
    source: str = "",
    role: str = "",
    evidence: list[str] | None = None,
    **ctx: Any,
) -> str:
    """[DEPRECATED] Persist via legacy experience library.

    New code should call ``wiki_ingest(kind='experience', title, body)``
    which writes a markdown page to the wiki layer. This handler now
    auto-redirects to wiki_ingest so legacy callers transparently land
    in the new store. Set env ``TUDOU_DISABLE_LEGACY_EXPERIENCE=0`` to
    keep the old JSON-write behaviour.
    """
    # ── Auto-redirect to wiki_ingest (default) ─────────────────────
    import os as _os
    if _os.environ.get("TUDOU_DISABLE_LEGACY_EXPERIENCE", "1") != "0":
        try:
            from .knowledge import _tool_wiki_ingest  # type: ignore
        except Exception:
            try:
                # Fallback: same module
                _tool_wiki_ingest = globals().get("_tool_wiki_ingest")
            except Exception:
                _tool_wiki_ingest = None
        if _tool_wiki_ingest is not None:
            # Compose body from the legacy experience fields
            body_parts = [str(core_knowledge or "")]
            if action_rules:
                body_parts.append("\n## 行动规则")
                for r in action_rules:
                    body_parts.append(f"- {r}")
            if taboo_rules:
                body_parts.append("\n## 禁忌规则")
                for r in taboo_rules:
                    body_parts.append(f"- {r}")
            if evidence:
                body_parts.append("\n## 证据 / 来源")
                for e in evidence:
                    body_parts.append(f"- {e}")
            return _tool_wiki_ingest(
                kind="experience",
                title=str(scene or "")[:80],
                body="\n".join(body_parts),
                tags=tags or [],
                scope="" if not role or role == "default" else f"role:{role}",
                **ctx,
            ) + " (auto-redirected from save_experience)"

    # ── Legacy path (only if explicitly opted-in) ───────────────────
    try:
        if not scene or not core_knowledge:
            return "Error: 'scene' and 'core_knowledge' are required"

        # Resolve role: explicit arg > calling agent's role > 'default'.
        resolved_role = (role or "").strip()
        if not resolved_role:
            try:
                caller_id = ctx.get("_caller_agent_id", "") if isinstance(ctx, dict) else ""
                if caller_id:
                    hub = _get_hub()
                    agent = hub.get_agent(caller_id) if hub else None
                    if agent is not None:
                        resolved_role = (getattr(agent, "role", "") or "").strip()
            except Exception:
                pass
        if not resolved_role:
            resolved_role = "default"

        # Validate priority.
        pri = (priority or "medium").strip().lower()
        if pri not in ("high", "medium", "low"):
            pri = "medium"

        # Validate exp_type.
        etype = (exp_type or "retrospective").strip().lower()
        if etype not in ("retrospective", "active_learning"):
            etype = "retrospective"

        from ..experience_library import get_experience_library, Experience

        # Normalize evidence: strip whitespace, drop empties, dedup while
        # preserving insertion order.
        evidence_clean: list[str] = []
        seen_refs: set[str] = set()
        for ref in (evidence or []):
            s = str(ref).strip()
            if s and s not in seen_refs:
                seen_refs.add(s)
                evidence_clean.append(s)

        lib = get_experience_library()
        exp = Experience(
            exp_type=etype,
            source=source or "agent.save_experience",
            scene=scene.strip(),
            core_knowledge=core_knowledge.strip(),
            action_rules=[str(r).strip() for r in (action_rules or []) if str(r).strip()],
            taboo_rules=[str(r).strip() for r in (taboo_rules or []) if str(r).strip()],
            priority=pri,
            tags=[str(t).strip() for t in (tags or []) if str(t).strip()],
            evidence=evidence_clean,
        )
        saved = lib.add_experience(resolved_role, exp)

        # ── 新 A.4: mirror to calling agent's L3 memory via upsert ──
        # The role-wide Experience Library is shared across agents of
        # this role, but the calling agent also benefits from its OWN
        # private L3 memory: memory_recall() stays fast & personal, and
        # the similarity-refresh layer means repeated saves of the same
        # lesson refresh rather than spam.
        mirror_note = ""
        try:
            caller_id = ctx.get("_caller_agent_id", "") if isinstance(ctx, dict) else ""
            if caller_id:
                from ..core.memory import (
                    get_memory_manager as _gmm, SemanticFact as _SF,
                )
                mm = _gmm()
                if mm is not None:
                    # Categorize by exp_type → L3 taxonomy.
                    # retrospective → rule (lesson for future)
                    # active_learning → reasoning (how I decided something)
                    l3_cat = "rule" if etype == "retrospective" else "reasoning"
                    # Compact content: scene + core_knowledge + rules.
                    parts = [
                        f"[{exp.scene.strip()}]",
                        exp.core_knowledge.strip(),
                    ]
                    if exp.action_rules:
                        parts.append(
                            "DO: " + "; ".join(exp.action_rules[:5])
                        )
                    if exp.taboo_rules:
                        parts.append(
                            "DON'T: " + "; ".join(exp.taboo_rules[:5])
                        )
                    compact = "\n".join(parts)
                    # Confidence from priority.
                    conf_map = {"high": 0.95, "medium": 0.8, "low": 0.6}
                    fact = _SF(
                        agent_id=caller_id,
                        category=l3_cat,
                        content=compact,
                        source=f"save_experience({resolved_role}/{saved.id[:8]})",
                        confidence=conf_map.get(pri, 0.8),
                    )
                    res = mm.upsert_fact(fact, threshold=0.75)
                    action = res.get("action")
                    if action == "updated":
                        mirror_note = (
                            f" │ L3 refreshed (sim={res.get('similarity', 0):.2f})"
                        )
                    elif action == "inserted":
                        mirror_note = " │ L3 inserted"
                    else:
                        mirror_note = " │ L3 unchanged"
        except Exception as _mirror_err:
            logger.debug("save_experience L3 mirror skipped: %s", _mirror_err)

        return (
            f"✓ Experience saved: id={saved.id} role={resolved_role} "
            f"priority={saved.priority} scene={saved.scene[:60]}"
            f"{mirror_note}"
        )
    except Exception as e:
        return f"Error saving experience: {e}"


# ── knowledge_lookup ─────────────────────────────────────────────────

# Pack v3: default cap on kb_list rows sent to the LLM. Was 200 — too
# tight for real KBs (HCS acceptance test guides hit 8000+ chunks and
# the agent ran out of pagination options). Bumped to 5000 so any
# realistic KB returns in one call. Each row is ~160 chars metadata so
# 5000 ≈ 800KB JSON, comfortable in a 200K context window.
#
# Tool callers can still override via the ``limit`` and ``offset`` kwargs
# — useful when you want a tighter window for cost or a paged read.
_KB_LIST_LIMIT_DEFAULT = 5000
_KB_LIST_LIMIT_HARD_MAX = 50000


def _kb_aggregate(mode: str, query: str, rag_mode: str,
                  agent_profile: Any, agent_id: str,
                  source_file: str = "",
                  heading_pattern: str = "") -> str:
    """Pack v3 — run count / list across every domain KB bound to this
    agent + (for shared/both) the global 'knowledge' collection.

    Returns a JSON string ready to hand back to the LLM. Never raises.
    """
    from ..rag_provider import get_rag_registry, get_domain_kb_store
    reg = get_rag_registry()

    # Enumerate the (provider_id, collection, label) tuples to scan.
    targets: list[tuple[str, str, str]] = []
    if rag_mode in ("private", "both") and agent_profile is not None:
        provider_id = getattr(agent_profile, "rag_provider_id", "") or ""
        coll_ids = getattr(agent_profile, "rag_collection_ids", []) or []
        dkb_store = get_domain_kb_store()
        for kb_id in coll_ids:
            kb = dkb_store.get(kb_id)
            if kb:
                targets.append((kb.provider_id or provider_id,
                                kb.collection,
                                kb.name or kb_id))
            else:
                targets.append((provider_id, kb_id, kb_id))
        # Legacy advisor_{id} collection
        if agent_id:
            targets.append((provider_id, f"advisor_{agent_id}",
                            f"advisor_{agent_id}"))
    if rag_mode in ("shared", "both"):
        targets.append(("", "knowledge", "shared_knowledge"))

    if not targets:
        return json.dumps({
            "status": "not_found",
            "mode": mode,
            "message": "No knowledge base bound to this agent.",
        }, ensure_ascii=False)

    if mode == "count":
        per_kb = []
        grand_total_chunks = 0
        grand_total_files = 0
        grand_filter_matched = 0
        for pid, coll, label in targets:
            try:
                stat = reg.kb_statistics(pid, coll, query=query)
            except Exception as e:
                logger.warning("kb_statistics failed for %s: %s", label, e)
                continue
            stat["kb"] = label
            stat["collection"] = coll
            per_kb.append(stat)
            grand_total_chunks += stat.get("total_chunks", 0)
            grand_total_files += stat.get("unique_source_files", 0)
            grand_filter_matched += stat.get("filter_matched", 0)
        # Detect "test case / scenario / section" intent in the query —
        # for those, chunk_count is NOT the right unit. Surface a strong
        # hint redirecting to mode=outline so the LLM doesn't blindly
        # report chunk_count as if it were the answer.
        _q_low = (query or "").lower()
        _wants_units = any(
            kw in _q_low for kw in (
                "case", "scenario", "section", "用例", "场景",
                "步骤", "step", "测试", "用 例",
            )
        )
        return json.dumps({
            "status": "success",
            "mode": "count",
            "filter": query or "",
            "grand_total_chunks": grand_total_chunks,
            "grand_total_source_files": grand_total_files,
            "grand_filter_matched": grand_filter_matched,
            "per_kb": per_kb,
            "usage_guidance": (
                "⚠️ CRITICAL: `chunk_count` / `total_chunks` is PHYSICAL "
                "splits (one logical test case can span 5-15 chunks). "
                "These numbers are NOT user-meaningful units like 'test "
                "cases', 'scenarios', 'sections', 'steps', '用例', '场景'. "
                "If the user asked for those, you MUST recall this tool "
                "with `mode='outline'` — it counts UNIQUE heading_paths "
                "(each leaf heading = one user-meaningful unit). Only "
                "report chunk_count when the user EXPLICITLY asked for "
                "'chunks', '分块', or '块数'.\n\n"
                "Each per_kb row has TWO breakdowns:\n"
                "  • by_source_file       — per-file counts WITH filter\n"
                "                            applied (substring match)\n"
                "  • by_source_file_full  — per-file counts of the WHOLE\n"
                "                            KB (always present even if\n"
                "                            the filter zero-matched —\n"
                "                            common with cross-language\n"
                "                            queries like 中文 → English\n"
                "                            content).\n"
                "If filter_matched=0 but by_source_file_full has files, "
                "the keyword you tried doesn't substring-match — try a "
                "different / English form, OR use mode='search' (vector "
                "embedding handles cross-language)."
            ),
            "next_action_hint": (
                "User asked for test cases / scenarios / 用例 / 场景 — "
                "the numbers above are CHUNKS, not cases. Re-call with "
                "mode='outline' to get the real per-document case count "
                "via heading_path enumeration. Do NOT report chunk_count "
                "as the answer."
            ) if _wants_units else None,
        }, ensure_ascii=False, indent=2)

    if mode == "outline":
        # Per-file structural outline — for "how many sections / test
        # cases / chapters per document" questions. Uses heading_paths
        # captured at chunk time (no model call, no embedding).
        per_kb_outlines = []
        grand_total_files = 0
        for pid, coll, label in targets:
            try:
                outline = reg.kb_outline(
                    pid, coll,
                    source_file=source_file,
                    heading_pattern=heading_pattern,
                )
            except Exception as e:
                logger.warning("kb_outline failed for %s: %s", label, e)
                continue
            outline["kb"] = label
            outline["collection"] = coll
            per_kb_outlines.append(outline)
            grand_total_files += outline.get("total_files", 0)
        return json.dumps({
            "status": "success",
            "mode": "outline",
            "source_file_filter": source_file or "",
            "heading_pattern": heading_pattern or "",
            "grand_total_files": grand_total_files,
            "per_kb": per_kb_outlines,
            "usage_guidance": (
                "Each `headings` row is a UNIQUE heading_path with a "
                "`chunks` count of how many chunks fall under it. "
                "Counting test cases / sections per document: \n"
                " (a) Look at heading_path patterns ('3.1.1 Objective', "
                "'4.2 Procedure' etc.).\n"
                " (b) Use `heading_pattern` regex to count only those "
                "matching your structural marker. For example: \n"
                "       heading_pattern='Objective' → counts all "
                "'Objective' subsections per file.\n"
                "       heading_pattern=r'^\\d+\\.\\d+\\.\\d+' → counts "
                "third-level numbered sections (typical 'one test case "
                "= one numbered subsection') format.\n"
                " (c) `matched_headings` per file is the answer to "
                "\"how many test cases in this doc?\" given that "
                "regex.\n"
                "Drill into one file: pass source_file=\"DataArts...\""
                " (substring match)."
            ),
        }, ensure_ascii=False, indent=2)

    # mode == "list"
    # Caller can override the default cap via kw["limit"] / kw["offset"];
    # we bound at _KB_LIST_LIMIT_HARD_MAX so a runaway loop can't blow
    # the LLM context with millions of rows.
    _list_limit_raw = locals().get("limit")  # placeholder; pulled from outer kw
    per_kb_lists = []
    for pid, coll, label in targets:
        try:
            lst = reg.kb_list(pid, coll, query=query,
                              limit=_KB_LIST_LIMIT_DEFAULT)
        except Exception as e:
            logger.warning("kb_list failed for %s: %s", label, e)
            continue
        lst["kb"] = label
        lst["collection"] = coll
        per_kb_lists.append(lst)
    total_shown = sum(len(k.get("items", [])) for k in per_kb_lists)
    any_truncated = any(k.get("truncated") for k in per_kb_lists)
    return json.dumps({
        "status": "success",
        "mode": "list",
        "filter": query or "",
        "total_shown": total_shown,
        "any_truncated": any_truncated,
        "per_kb": per_kb_lists,
        "usage_guidance": (
            "Each item is a chunk's metadata only (no content body). "
            "Use this to render a table of contents or inventory of "
            "what's in the KB. If `any_truncated` is True some KBs "
            "have more items than were returned — suggest a narrower "
            "filter."
        ),
    }, ensure_ascii=False, indent=2)


def _tool_knowledge_lookup(query: str = "", entry_id: str = "",
                           agent_id: str = "", mode: str = "search",
                           **kw: Any) -> str:
    """Look up entries in the knowledge base.

    Routing is determined by the agent's rag_mode:
      - "shared"  → query global shared knowledge (default)
      - "private" → query agent's private collection
      - "both"    → query private first, then shared
      - "none"    → return empty

    Modes (Pack v3):
      - mode="search" (default): top-k vector+BM25 retrieval with content.
        Use for normal questions that can be answered from a few chunks.
        Response now includes a ``coverage`` sidecar showing per-file
        chunk distribution + KB-wide totals — use it to detect "answer
        spans many docs, need to widen".
      - mode="count": programmatic scan of ALL chunks in the collection,
        grouped by source_file. Use for aggregate queries ("how many?",
        "总数", "有多少个"). Optional ``query`` filters by substring
        match. Response carries TWO breakdowns: ``by_source_file``
        (filter applied) and ``by_source_file_full`` (whole KB) — when
        filter zero-matches with cross-language input, the _full one
        still reveals KB structure.
      - mode="list": flat list of chunks' metadata (no content). Use
        when user wants a table of contents / inventory listing.
      - mode="outline": per-file STRUCTURAL outline — unique heading_paths
        with chunk counts, optionally filtered by ``source_file``
        (substring) and / or ``heading_pattern`` (regex). Use when user
        wants "how many sections / test cases / chapters per document".
        Example: ``mode="outline", heading_pattern="Objective"`` returns
        per-file count of "Objective" sections — typical proxy for "test
        cases per doc". Bypasses retrieval; cheap (metadata-only).

    If entry_id is provided, returns that entry's full content from
    shared KB. Otherwise searches / counts / lists by query using the
    agent's configured RAG routing.
    """
    # Per-turn dedup (same as memory_recall): avoid near-identical
    # query variants hitting the RAG pipeline in a single turn.
    # Mode-aware: re-calling with a DIFFERENT mode (search→count→outline)
    # is allowed since each mode returns structurally different data.
    _caller_id = kw.get("_caller_agent_id") or agent_id or ""
    _hub = _get_hub()
    _kl_agent = _hub.get_agent(_caller_id) if _hub and _caller_id else None
    _q_str = (query or "").strip()
    _mode_for_dedup = (mode or "search").lower().strip()
    if _q_str and not entry_id:
        _dup = _check_turn_dedup(
            _kl_agent, "knowledge_lookup", _q_str,
            mode=_mode_for_dedup,
        )
        if _dup is not None:
            return _dup

    # ── Auto-route by intent (HCS-inspired, 2026-05-05) ───────────────
    # When caller passed mode="search" (the default) but the query
    # clearly asks for a count / overview, flip to mode="outline"
    # automatically. Saves ~5 wrong vector-search calls before the
    # agent figures it out itself.
    _intent_banner = ""
    if _q_str and not entry_id and _mode_for_dedup == "search":
        _intent = _classify_rag_intent(_q_str)
        if _intent == "stat":
            mode = "outline"
            _mode_for_dedup = "outline"
            _intent_banner = (
                "[auto-routed: search → outline]\n"
                "Detected statistical/aggregate intent in query "
                f"({_q_str[:60]}...). Returning per-file unique heading_paths\n"
                "instead of top-k chunks. If you actually wanted full content, "
                "re-call with mode=\"search\" explicitly.\n\n"
            )
            logger.info("knowledge_lookup auto-routed to outline: %r", _q_str[:80])

    # ── Wiki layer search (PRIMARY for agent-written knowledge) ──
    # The wiki holds everything wiki_ingest writes (experience /
    # methodology / template / pattern / reference). Search it BEFORE
    # the legacy KB so wiki hits — agent-authored, curated — surface
    # at the top. Best-effort: any error keeps falling through to KB.
    #
    # Phase-1 evolution (2026-05-08): every hit returned to the LLM
    # also lands in the calling agent's ``lookup_trace``. When the
    # agent later closes a step via finalize_step, that trace is
    # flushed and each hit gets a success_count++ on its wiki page —
    # the cheap learning signal that doesn't write any new tokens.
    wiki_lines: list[str] = []
    if mode == "search" and _q_str and not entry_id:
        try:
            from ..knowledge.wiki_store import get_wiki_store
            # Phase 3 / Domain Step 4: infer domains from the query
            # and pass them to search() for relevance boost. Keyword-
            # based, zero-LLM, returns [] for unclassifiable queries
            # (in which case search behaves identically to before).
            _inferred_domains = _infer_domains_from_query(_q_str)
            _hits = get_wiki_store().search(
                _q_str, limit=5, match_domains=_inferred_domains,
            )
            if _hits:
                wiki_lines.append("[wiki layer hits]")
                for p in _hits:
                    body_preview = (p.body or "")[:400].replace("\n", " ")
                    wiki_lines.append(
                        f"- {p.kind}/{p.slug} · {p.title} "
                        f"(scope={p.scope}, ✓{p.success_count}/✗{p.fail_count})\n"
                        f"  {body_preview}"
                        + ("..." if len(p.body or "") > 400 else "")
                    )
                    # Record into the calling agent's outcome-trace.
                    # Best-effort: silent skip if no caller agent
                    # (e.g. invoked from a hub admin path).
                    if _kl_agent is not None:
                        try:
                            _kl_agent.record_lookup_hit(
                                scope=p.scope, kind=p.kind, slug=p.slug,
                                query=_q_str,
                            )
                        except Exception as _rh_err:
                            logger.debug(
                                "record_lookup_hit skipped: %s", _rh_err,
                            )
        except Exception as _we:
            logger.debug("wiki search in knowledge_lookup skipped: %s", _we)

    # Existing KB impl runs unchanged.
    _result = _knowledge_lookup_impl(query=query, entry_id=entry_id,
                                      agent_id=agent_id, mode=mode, **kw)

    # Prepend wiki block if non-empty.
    if wiki_lines:
        _result = "\n".join(wiki_lines) + "\n\n[knowledge base]\n" + _result

    # Anti-laziness prelude (HCS-inspired): inject hard rules so the
    # agent never falls back to "请查阅文档" or summarizes tables away.
    # Skipped for empty results — adds noise to "no hits" replies.
    if _result and "no_hits" not in _result.lower() and len(_result) > 50:
        _result = _RAG_RESULT_PRELUDE + _intent_banner + _result
    elif _intent_banner:
        _result = _intent_banner + _result

    if _q_str and not entry_id:
        try:
            _cache_turn_result(
                _kl_agent, "knowledge_lookup", _q_str, _result,
                mode=_mode_for_dedup,
            )
            _result = _wrap_with_state(_kl_agent, "knowledge_lookup", _q_str, _result)
        except Exception:
            pass
    return _result


def _knowledge_lookup_impl(query: str = "", entry_id: str = "",
                            agent_id: str = "", mode: str = "search",
                            **kw: Any) -> str:
    """Inner impl of knowledge_lookup (no dedup). Kept as a separate
    function so the dedup wrapper can always cache the final result
    regardless of which early-return path fires."""
    try:
        # If entry_id is provided, fetch that entry directly from shared KB.
        if entry_id:
            entry = _knowledge.get_entry(entry_id)
            if entry:
                return json.dumps({"status": "success", "entry": entry},
                                  ensure_ascii=False, indent=2)
            return json.dumps({
                "status": "error",
                "message": f"Entry '{entry_id}' not found"
            })

        mode = (mode or "search").lower().strip()
        if mode not in ("search", "count", "list", "outline"):
            return json.dumps({
                "status": "error",
                "message": f"Unknown mode '{mode}'. "
                           f"Use search | count | list | outline.",
            })

        # search mode requires query; count / list allow empty query
        # (whole-collection overview).
        if mode == "search" and not query:
            return json.dumps({
                "status": "error",
                "message": "Either 'query' or 'entry_id' must be provided "
                           "for mode=search",
            })

        # --- RAG-routed search ---
        agent_profile = kw.get("_agent_profile")
        rag_mode = "shared"  # default
        if agent_profile:
            rag_mode = getattr(agent_profile, "rag_mode", "shared") or "shared"

        if rag_mode == "none":
            return json.dumps({
                "status": "not_found",
                "message": "RAG is disabled for this agent (rag_mode=none)"
            })

        # --- Pack v3: count / list / outline modes bypass top-k entirely. ---
        if mode in ("count", "list", "outline"):
            # Outline mode passes its own kwargs (source_file, heading_pattern)
            # via the **kw bag that the top-level wrapper hands us — pull
            # them out so _kb_aggregate doesn't have to dig.
            return _kb_aggregate(
                mode, query, rag_mode, agent_profile, agent_id,
                source_file=str(kw.get("source_file") or ""),
                heading_pattern=str(kw.get("heading_pattern") or ""),
            )

        results_combined = []

        # Private / Both: search via RAG provider registry.
        # v2 (Pack): RAG chunks are already content — keep their bodies,
        # surface citation metadata (source_file, heading_path, chunk_index),
        # and cap each chunk to _MAX_CHUNK_CHARS_PER_HIT.
        rag_hits = []
        if rag_mode in ("private", "both") and agent_id:
            try:
                from ..rag_provider import search_for_agent
                rag_results = search_for_agent(agent_profile, query,
                                               agent_id=agent_id,
                                               top_k=_RAG_TOP_K)
                for r in rag_results:
                    meta = r.get("metadata") or {}
                    raw_content = r.get("content", "") or ""
                    trimmed = raw_content[:_MAX_CHUNK_CHARS_PER_HIT]
                    tags_raw = meta.get("tags", "")
                    tags_list = tags_raw.split(",") if isinstance(tags_raw, str) and tags_raw else []

                    # Image-source chunks: prepend a markdown image
                    # reference into ``content`` so the LLM sees the
                    # image URL and tends to cite it back; the UI
                    # renders ![](...) inline. Keep the structured
                    # fields too in case downstream wants to format
                    # differently.
                    image_url = meta.get("image_url", "")
                    is_image = bool(meta.get("is_image"))
                    display_content = trimmed
                    if image_url and is_image:
                        display_content = (
                            f"![{meta.get('source_file', 'image')}]({image_url})\n\n"
                            + trimmed
                        )

                    rag_hits.append({
                        "id": r.get("id", ""),
                        "title": r.get("title", ""),
                        "content": display_content,
                        "content_truncated": len(raw_content) > _MAX_CHUNK_CHARS_PER_HIT,
                        # ── citation fields (v1-C metadata persisted these) ──
                        "source_file": meta.get("source_file", ""),
                        "heading_path": meta.get("heading_path", ""),
                        "chunk_index": meta.get("chunk_index"),
                        "tags": tags_list,
                        "source": "private_rag",
                        # ── image attachment (when ingested from .jpg/.png/...) ──
                        "image_url": image_url,
                        "is_image": is_image,
                        "mime": meta.get("mime_hint", ""),
                    })
            except Exception as e:
                logger.warning("RAG provider search failed: %s", e)

        # Shared / Both: also search the classic shared knowledge base.
        shared_hits = []
        if rag_mode in ("shared", "both"):
            shared_results = _knowledge.search(query)
            for e in shared_results:
                # Avoid duplicates (same ID already from RAG).
                if not any(r["id"] == e["id"] for r in rag_hits):
                    shared_hits.append(e)

        if not rag_hits and not shared_hits:
            return json.dumps({
                "status": "not_found",
                "message": f"知识库无匹配内容 for '{query}'. "
                           "Do NOT fabricate — tell the user no relevant knowledge found.",
            }, ensure_ascii=False)

        # v2 (Pack): RAG hits ARE content chunks (not titled entries),
        # so we return them directly as `entries` with full content + citation.
        # The Shared-pool legacy "partial/match-by-title" flow is kept for
        # shared entries only.
        if rag_hits:
            # ── Coverage sidecar (added 2026-05-04 — root-cause fix for
            # "RAG 没有之前效果好" complaint) ──────────────────────────
            # Aggregate the retrieved chunks by source_file so the LLM
            # can answer "how many / list all / per-document" questions
            # WITHOUT issuing a second knowledge_lookup call. Many real
            # queries are aggregate-style ("each cloud service has how
            # many test cases?") and the previous response forced the
            # agent to either (a) infer aggregates from a top-k sample
            # — wrong — or (b) suggest the user re-issue with mode=count
            # — bad UX. Now top-k AND a coverage breakdown ride together.
            # Plus we always include a KB-wide chunk total so "this top-k
            # is N out of M total" is explicit.
            coverage_groups: dict[str, dict] = {}
            for h in rag_hits:
                sf = h.get("source_file") or "(unknown)"
                g = coverage_groups.setdefault(sf, {
                    "source_file": sf, "chunks_in_topk": 0,
                    "headings_seen": [],
                })
                g["chunks_in_topk"] += 1
                hp = h.get("heading_path") or ""
                if hp and hp not in g["headings_seen"] \
                        and len(g["headings_seen"]) < 3:
                    g["headings_seen"].append(hp[:120])
            coverage = sorted(coverage_groups.values(),
                              key=lambda x: -x["chunks_in_topk"])

            # Optional: cheap KB-wide totals so the LLM knows top-k size
            # vs total. Falls back gracefully when stats not available
            # (different provider type, etc.).
            kb_total_chunks = None
            kb_total_files = None
            try:
                from ..rag_provider import get_rag_registry, get_domain_kb_store
                _reg = get_rag_registry()
                _store = get_domain_kb_store()
                _coll_ids = getattr(agent_profile, "rag_collection_ids", []) or []
                _t_chunks = 0
                _t_files = 0
                for _kb_id in _coll_ids:
                    _kb = _store.get(_kb_id)
                    if not _kb:
                        continue
                    _stat = _reg.kb_statistics(_kb.provider_id, _kb.collection)
                    _t_chunks += _stat.get("total_chunks", 0)
                    _t_files += _stat.get("unique_source_files", 0)
                kb_total_chunks = _t_chunks
                kb_total_files = _t_files
            except Exception as _ce:
                logger.debug("coverage stats failed: %s", _ce)

            return json.dumps({
                "status": "success",
                "entries": rag_hits,
                "coverage": {
                    "topk_size": len(rag_hits),
                    "topk_distinct_files": len(coverage),
                    "by_source_file": coverage,
                    "kb_total_chunks": kb_total_chunks,
                    "kb_total_files": kb_total_files,
                },
                "usage_guidance": (
                    "RAG content for answering. "
                    "(1) cite as [source_file §heading_path] or [title #chunk_index]; "
                    "(2) reason only from `entries` content — never extrapolate; "
                    "(3) `coverage` shows how the top-k was distributed across "
                    "files AND the KB-wide totals; for aggregate questions "
                    "(\"how many / list all / per-document\") prefer the coverage "
                    "numbers OVER counting entries by hand. "
                    "(4) If `topk_size < kb_total_chunks` and the user asked "
                    "for a roll-up, recall this tool with `mode=\"count\"` for "
                    "an exact KB-wide breakdown — it's free and one extra call."
                ),
            }, ensure_ascii=False, indent=2)

        # Fallback: only shared pool hits. Keep legacy title-match refinement.
        query_lower = query.lower().strip()
        for entry in shared_hits:
            if entry.get("title", "").lower().strip() == query_lower:
                return json.dumps({"status": "success", "entry": entry},
                                  ensure_ascii=False, indent=2)

        matches = [
            {"id": e.get("id", ""), "title": e.get("title", ""),
             "tags": e.get("tags", []),
             "source": e.get("source", "shared")}
            for e in shared_hits[:_PARTIAL_MATCHES_CAP]
        ]
        return json.dumps({
            "status": "partial",
            "message": (f"Found {len(matches)} shared entries matching title. "
                        "Use entry_id to read full content."),
            "matches": matches,
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        return json.dumps({
            "status": "error",
            "message": f"Error querying knowledge base: {str(e)}"
        })


# ── share_knowledge ──────────────────────────────────────────────────

def _tool_share_knowledge(title: str, content: str,
                          tags: list[str] | None = None,
                          **ctx: Any) -> str:
    """Share knowledge with all agents via the shared Knowledge Base."""
    try:
        if not title or not title.strip():
            return "Error: 'title' is required"
        if not content or not content.strip():
            return "Error: 'content' is required"

        # Resolve caller agent info for attribution.
        caller_name = ""
        caller_role = ""
        try:
            caller_id = ctx.get("_caller_agent_id", "") if isinstance(ctx, dict) else ""
            if caller_id:
                hub = _get_hub()
                agent = hub.get_agent(caller_id) if hub else None
                if agent is not None:
                    caller_name = getattr(agent, "name", "") or ""
                    caller_role = getattr(agent, "role", "") or ""
        except Exception:
            pass

        # Add source attribution.
        source_info = ""
        if caller_name or caller_role:
            source_info = f"\n\n---\nShared by: {caller_name} (role: {caller_role})"

        resolved_tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
        if caller_role:
            resolved_tags += ["shared-by-agent", caller_role]

        entry = _knowledge.add_entry(
            title=title.strip(),
            content=content.strip() + source_info,
            tags=resolved_tags,
        )
        return (
            f"Knowledge shared successfully: '{title.strip()}' "
            f"(id: {entry['id']}). "
            f"All agents can now access this via knowledge_lookup."
        )
    except Exception as e:
        return f"Failed to share knowledge: {e}"


# ── learn_from_peers ─────────────────────────────────────────────────

def _tool_learn_from_peers(source_role: str, topic: str = "",
                           limit: int = 5, **ctx: Any) -> str:
    """Learn from other agents' experiences by importing from another role."""
    try:
        if not source_role or not source_role.strip():
            return "Error: 'source_role' is required"

        source_role = source_role.strip()
        limit = max(_LEARN_LIMIT_MIN, min(int(limit), _LEARN_LIMIT_MAX))

        # Resolve caller's role.
        caller_role = ""
        try:
            caller_id = ctx.get("_caller_agent_id", "") if isinstance(ctx, dict) else ""
            if caller_id:
                hub = _get_hub()
                agent = hub.get_agent(caller_id) if hub else None
                if agent is not None:
                    caller_role = getattr(agent, "role", "") or ""
        except Exception:
            pass

        from ..experience_library import _get_global_library
        library = _get_global_library()
        experiences = library.import_cross_role(
            source_role=source_role,
            target_role=caller_role or "default",
            topic=topic.strip() if topic else "",
            limit=limit,
        )
        if not experiences:
            msg = f"No experiences found for role '{source_role}'"
            if topic:
                msg += f" on topic '{topic}'"
            return msg

        lines = [f"Imported {len(experiences)} experiences from role "
                 f"'{source_role}':"]
        for i, exp in enumerate(experiences, 1):
            lines.append(f"\n{i}. [{exp.priority}] {exp.scene}")
            lines.append(f"   Knowledge: {exp.core_knowledge[:200]}")
            if exp.action_rules:
                lines.append(f"   Rules: {'; '.join(exp.action_rules[:3])}")
            lines.append(f"   Success rate: {exp.success_rate:.0%}")
        return "\n".join(lines)
    except Exception as e:
        return f"Failed to learn from peers: {e}"


# ── wiki_ingest (V2 Karpathy-pattern wiki write) ──────────────────────

def _tool_wiki_ingest(kind: str = "",
                      title: str = "",
                      body: str = "",
                      tags: Any = None,
                      scope: str = "",
                      slug: str = "",
                      sources: Any = None,
                      related: Any = None,
                      domains: Any = None,
                      **ctx: Any) -> str:
    """Write or update a markdown page in the wiki layer.

    kind    ∈ experience | methodology | template | pattern | reference
    scope   ∈ "global" | "" (auto = role:<caller_role>)
    domains ∈ controlled vocabulary (security / payments-compliance /
              project-management / frontend / backend / devops /
              testing / data-analysis / writing-content / customer-
              support / legal-compliance / general-ops, etc.). Used by
              knowledge_lookup to boost domain-relevant hits AND by
              expertise_scores accumulation. List of strings.
              See app/knowledge/wiki_store.py for the rationale.
    """
    try:
        from ..knowledge.wiki_store import (
            get_wiki_store, WikiPage, slugify, VALID_KINDS,
        )
    except Exception as e:
        return f"Error: wiki layer unavailable ({e})"

    kind = (kind or "").strip().lower()
    if kind not in VALID_KINDS:
        return (f"Error: kind must be one of {list(VALID_KINDS)}, got {kind!r}")
    title = (title or "").strip()
    if not title:
        return "Error: 'title' is required"
    body = (body or "").strip()
    if not body:
        return "Error: 'body' is required (markdown content)"

    # ── Pre-publish leak check (via guardrail protocol) ─────
    # wiki pages get auto-injected into other agents' system
    # prompts as titles + on-demand reads of full body. A page
    # carrying a hardcoded API key / .env path / internal IP
    # would leak to every same-role agent + their LLM provider.
    try:
        from ..v2.core.guardrails import wiki_leak_guardrail
        out = wiki_leak_guardrail.run(title + "\n\n" + body)
        if out.tripwire_triggered:
            leak_report = out.output_info or {}
            leaks = leak_report.get("leaks", [])
            samples = ", ".join(
                f"{l['type']}={l['value'][:30]}" for l in leaks[:3]
            )
            return (
                "Error: wiki_ingest rejected — body contains "
                f"{len(leaks)} potential leak(s) ({samples}). "
                "Replace hardcoded secrets/paths/ips with env-var "
                "references and retry. Use the `scan_for_leaks` "
                "debug helper if unsure."
            )
    except Exception as _le:
        # Fail-open: leak detection MUST NOT break the legitimate
        # ingest path. Log + continue.
        import logging as _logging
        _logging.getLogger("tudou.security").debug(
            "leak_check on wiki_ingest skipped: %s", _le,
        )

    # Resolve scope: explicit "global" OR auto-derive role from caller.
    if scope == "global":
        resolved_scope = "global"
    elif scope and scope.startswith("role:"):
        resolved_scope = scope
    else:
        # Auto-resolve to caller's role
        caller_role = ""
        try:
            caller_id = ctx.get("_caller_agent_id", "") if isinstance(ctx, dict) else ""
            if caller_id:
                hub = _get_hub()
                agent = hub.get_agent(caller_id) if hub else None
                if agent is not None:
                    caller_role = (getattr(agent, "role", "") or "").strip()
        except Exception:
            pass
        resolved_scope = f"role:{caller_role}" if caller_role else "global"

    # Normalize list args
    if isinstance(tags, str):
        tags_list = [t.strip() for t in tags.split(",") if t.strip()]
    elif isinstance(tags, list):
        tags_list = [str(t).strip() for t in tags if str(t).strip()]
    else:
        tags_list = []
    sources_list = sources if isinstance(sources, list) else (
        [sources] if sources else []
    )
    related_list = related if isinstance(related, list) else (
        [related] if related else []
    )
    if isinstance(domains, str):
        domains_list = [d.strip() for d in domains.split(",") if d.strip()]
    elif isinstance(domains, list):
        domains_list = [str(d).strip() for d in domains if str(d).strip()]
    else:
        domains_list = []

    final_slug = slug.strip() if slug else slugify(title)

    page = WikiPage(
        scope=resolved_scope,
        kind=kind,
        slug=final_slug,
        title=title,
        body=body,
        tags=tags_list,
        sources=[str(s) for s in sources_list if s],
        related=[str(r) for r in related_list if r],
        domains=domains_list,
    )

    store = get_wiki_store()
    saved = store.write_page(page, log_action="ingest")
    return (
        f"Wiki page saved: scope={saved.scope} kind={saved.kind} "
        f"slug={saved.slug} (use knowledge_lookup or wiki path "
        f"{saved.kind}/{saved.slug} to retrieve)"
    )
