"""User-message intent detectors.

Three classifiers, all pure functions, all return bool:

    user_explicitly_requests_retrieval(text)  — "查/搜/find/lookup/..."
    user_explicitly_requests_wiki_write(text) — "记下来/save to wiki/..."
    user_asked_for_verification(text)         — "validate/test/验证/..."

Used by:
  - legacy chat loop (app/agent.py): tool-set filtering + must-verify
    nudge condition
  - future SDK adapter (app/agent_runtime/): same purpose, called from
    instructions builder + RunHooks

Conservative-by-design: false-negatives (no detection) are preferred
over false-positives (mis-trigger). The user can always rephrase to
opt in explicitly. Mis-triggering would either expose a tool the
admin didn't want, or loop the agent on a non-verification task.

History: extracted from app/agent.py:151-319 + 1996-2045 on
2026-05-15 as part of the "B: shared runtime helpers" refactor
(per docs/MIGRATION_OPENAI_AGENTS_SDK.md §10 no-regret moves).
Behavior is byte-identical to the in-file versions.
"""
from __future__ import annotations

import re as _re_module


# ── Retrieval intent (knowledge_lookup / memory_recall opt-in) ──────

_RETRIEVAL_PATTERNS_ZH = (
    # 查/查找/查询/查一下/查下/查阅 + 关键词
    r"(?:^|[，。、\s])查(?:一下|一查|找|询|阅|查)?(?:[一下]?)(?:\s|[一-鿿])",
    # 搜/搜索/搜一下
    r"(?:^|[，。、\s])搜(?:一下|索|搜)?(?:\s|[一-鿿])",
    # 找/找一下/找下 + (相关/类似/之前 ...)? + 名词
    # 名词扩到 "方案/计划/资料/记录/信息/文档/笔记/知识/wiki/资源/
    #          案例/例子/样例/材料/文件/历史/数据/经验/做法/手册"
    r"找(?:一下|下|找)?[一-鿿\s]{0,8}"
    r"(?:资料|记录|信息|文档|笔记|知识|wiki|资源|方案|案例|"
    r"例子|样例|材料|文件|历史|数据|经验|做法|手册|计划|内容)",
    # 记得 / 记得吗 / 想起 / 回忆 / 之前说过 / 我说过 / 上次
    r"(记得|想起|回忆|之前(说过|提到|讲过)|我(?:说过|提过|讲过)|上次)",
    # 知识库 / wiki / 记忆 / memory + 里有 / 有没有 / 有哪些
    r"(?:知识库|wiki|记忆|memory)\s*(?:里|中|内)?\s*"
    r"(?:有|找|有没有|有哪些|有什么)",
    # 显式调用工具名 (admin / power user)
    r"\b(?:knowledge_lookup|memory_recall)\b",
    # "看看 / 看下 + 知识库/记忆/wiki"
    r"看\s*(?:一下|看|下)?\s*(?:知识库|wiki|记忆|memory)",
    # "调取 / 调用 + 记忆/知识"
    r"(?:调取|调用)\s*(?:记忆|知识|wiki)",
)

_RETRIEVAL_PATTERNS_EN = (
    r"\b(?:search|searches|searching)\b",
    r"\blook(?:\s+up|up|s\s+up|ed\s+up|ing\s+up)\b",
    r"\bfind(?:\s+(?:in|the|me|my))\b",
    r"\b(?:recall|recalls|recalling)\b",
    r"\b(?:remember|remembers|remembering)\b",
    r"\b(?:lookup|knowledge[-_\s]?lookup|memory[-_\s]?recall)\b",
    r"\b(?:knowledge\s+base|wiki|memory)\b\s*"
    r"(?:has|have|contains|stores|for|about)",
    r"\bdid\s+(?:we|i|you)\s+(?:say|mention|talk|note|record|discuss)",
    r"\bhave\s+(?:we|i|you)\s+(?:saved|noted|recorded|stored|"
    r"mentioned|discussed|talked\s+about)",
)

_RETRIEVAL_RE_ZH = _re_module.compile("|".join(_RETRIEVAL_PATTERNS_ZH))
_RETRIEVAL_RE_EN = _re_module.compile(
    "|".join(_RETRIEVAL_PATTERNS_EN), _re_module.IGNORECASE)


def user_explicitly_requests_retrieval(user_text: str) -> bool:
    """True iff user's message phrasing explicitly asks for memory/KB
    retrieval. False for action verbs (改/做/继续/fix/build/...) and
    for general questions that don't name retrieval explicitly.
    """
    if not user_text or not isinstance(user_text, str):
        return False
    txt = user_text.strip()
    if not txt:
        return False
    txt_lower = txt.lower()
    if any(kw in txt for kw in (
        "查", "搜", "找", "记得", "想起", "回忆", "之前", "我说过",
        "知识库", "wiki", "记忆", "memory",
    )) or any(kw in txt_lower for kw in (
        "search", "lookup", "look up", "find", "recall", "remember",
        "mention", "discuss", "noted", "recorded",
    )):
        if _RETRIEVAL_RE_ZH.search(txt):
            return True
        if _RETRIEVAL_RE_EN.search(txt):
            return True
    return False


# ── Wiki-write intent (wiki_ingest opt-in) ──────────────────────────

_WIKI_WRITE_PATTERNS_ZH = (
    r"(记下来|记一下|记录一下)",
    r"(?:存|写|保存|放|加)(?:进|入|到)\s*(?:wiki|知识库|记忆)",
    r"总结\s*(?:成|为|进)\s*(?:wiki|知识库|经验|笔记)",
    # 做/写/来个/做一下/写个/做个 + 复盘/retro/...
    r"(?:做|写|来个|整理)(?:一下|个|出)?\s*"
    r"(?:复盘|retro|总结|经验|笔记|playbook)",
    r"\bwiki_ingest\b",
    r"整理\s*(?:成|为|进)\s*(?:wiki|知识库|经验|文档)",
)

_WIKI_WRITE_PATTERNS_EN = (
    # Generic "save X to wiki" — accepts pronouns AND nouns
    r"\bsave\s+(?:\w+\s+)?(?:to|in|into)\s+(?:the\s+)?"
    r"(?:wiki|memory|knowledge\s*base|kb)\b",
    r"\bsave\s+(?:this|that|it|these|them)\b",
    # add/append/put/persist/store/log/record [pronoun] (to|in|into)
    # [the] wiki|memory|kb
    r"\b(?:add|append|put|persist|store|log|record)\s+"
    r"(?:\w+\s+)?(?:to|into|in)\s+(?:the\s+)?"
    r"(?:wiki|memory|knowledge\s*base|kb)\b",
    r"\bwrite\s+(?:a|the|me\s+a)?\s*"
    r"(?:retro|retrospective|playbook|summary|note|lesson|"
    r"wiki\s*entry)",
    r"\bwiki_ingest\b",
    r"\bremember\s+this\b",
)

_WIKI_WRITE_RE_ZH = _re_module.compile("|".join(_WIKI_WRITE_PATTERNS_ZH))
_WIKI_WRITE_RE_EN = _re_module.compile(
    "|".join(_WIKI_WRITE_PATTERNS_EN), _re_module.IGNORECASE)


def user_explicitly_requests_wiki_write(user_text: str) -> bool:
    """True iff user's message phrasing explicitly asks the agent to
    save/persist something to the wiki/knowledge base. False for
    action verbs that don't name the wiki ("修复 X" / "做 X").
    """
    if not user_text or not isinstance(user_text, str):
        return False
    txt = user_text.strip()
    if not txt:
        return False
    txt_lower = txt.lower()
    if any(kw in txt for kw in (
        "记下", "记一下", "记录", "存进", "写进", "存到", "放进", "加到",
        "总结", "复盘", "整理", "wiki", "知识库", "经验",
    )) or any(kw in txt_lower for kw in (
        "save", "wiki", "knowledge", "remember this", "memory",
        "retro", "retrospective", "playbook", "persist",
        "add this", "add it", "add to", "store", "log this",
    )):
        if _WIKI_WRITE_RE_ZH.search(txt):
            return True
        if _WIKI_WRITE_RE_EN.search(txt):
            return True
    return False


# ── Verification intent (must-verify nudge) ─────────────────────────

VERIFY_INTENT_KEYWORDS = (
    # ZH
    "验证", "确认", "检查", "测试", "跑通", "通过验证", "全部通过",
    "0 错误", "0 个错误", "无错误", "0 errors",
    # specific tools
    "terraform validate", "terraform plan", "terraform apply",
    "npm test", "npm run test", "pytest", "jest", "mypy", "lint",
    "go test", "cargo test", "gradle test", "mvn test",
    # EN
    "validate", "verify", "test", "lint", "check ",
)


def user_asked_for_verification(user_text: str) -> bool:
    """True iff user's message implies a verification step is part of
    'done'. Conservative — false-negatives are fine (no nudge), but
    false-positives would loop the agent on a non-verification task.
    """
    if not user_text or not isinstance(user_text, str):
        return False
    txt_lower = user_text.lower()
    return any(kw in user_text for kw in VERIFY_INTENT_KEYWORDS) \
        or any(kw in txt_lower for kw in VERIFY_INTENT_KEYWORDS)
