"""Intent extractor — 中文短指令意图提取 + 缺槽检测。

为什么要这一层
==============
中文用户尤其爱写"做个 PPT" / "看一下" / "搞一下" — 这种 input
信息量极少,主 LLM 拿到只能反问或乱猜。如果在 turn 开始前用一次
**便宜的小模型**抽取意图 + 检测是否缺关键字段,就能:

  1. 缺关键字段时 agent 直接反问,**不调主 LLM**(省一轮)
  2. 字段齐全时,把抽取结果作为 ``[USER_INTENT]`` 块注入 system prompt,
     主 LLM 同时看到原始用户输入 + 已结构化的意图,理解更准

用法
====

::

    from app.intent_extractor import extract_intent, IntentResult

    # 生产: 把 llm caller 注入,通常是 DeepSeek-Chat
    def my_caller(prompt: str) -> str:
        from app.llm import chat
        resp = chat(
            messages=[{"role": "user", "content": prompt}],
            model="deepseek-chat",
            temperature=0.0,  # 抽取要稳定
            tools=None,
        )
        return resp["message"]["content"]

    result = extract_intent("做个 PPT", llm_caller=my_caller)
    if result.should_clarify:
        # missing_required 非空 → agent 直接反问,不进主 LLM
        questions = result.clarifying_questions()
        # ...回复用户...
    else:
        # 完整意图 → 注入 system prompt
        system_block = result.as_system_block()
        # ...拼到 system prompt 里...

设计约束
========
* 故意不 wire 到 ``agent.py`` 调用链 — 入口怎么接,等 Phase 2
  (system_prompt 块化条件装入) 一起做,以免现在接了一处又改一处
* ``llm_caller`` 是 ``Callable[[str], str]``,允许传入任何返回 string
  的可调用对象(测试 mock / 替换 provider 都方便)
* ``temperature=0.0`` 在 caller 内部由调用方决定 — 模块本身不强制

Cost / latency
==============
按 DeepSeek-Chat 估算,提取一次:
  input  ~150 tokens (system + user prompt)
  output ~80 tokens  (JSON)
  延迟   ~300-500ms 首 token
  成本   ~¥0.0003 / call
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger = logging.getLogger("tudou.intent_extractor")


# ──────────────────────────────────────────────────────────────────────
# Result type
# ──────────────────────────────────────────────────────────────────────


@dataclass
class IntentResult:
    """抽取结果。

    Fields
    ------
    intent              — 一级意图标签,固定枚举
    deliverable_type    — 期望产出类型(pptx / docx / xlsx / code / answer / ...)
    topic               — 主题(若可识别)
    audience            — 受众(高管 / 客户 / 同事 / ...)
    page_count / length — 篇幅(PPT 页数,文档字数等)
    missing_required    — 缺失的关键字段名列表(由 caller 配置必需性)
    raw                 — extractor 原始 JSON(便于调试 / 后续 ML 训练)
    extractor_failed    — extractor 调用 / 解析失败时为 True
    """

    intent: str = "unknown"
    deliverable_type: Optional[str] = None
    topic: Optional[str] = None
    audience: Optional[str] = None
    page_count: Optional[int] = None
    length: Optional[str] = None
    missing_required: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    extractor_failed: bool = False

    @property
    def should_clarify(self) -> bool:
        """非空 missing_required + extractor 没失败 → 推荐反问。"""
        return bool(self.missing_required) and not self.extractor_failed

    def clarifying_questions(self) -> str:
        """生成给用户的反问文本(中文)。

        基于 ``missing_required`` 字段名映射到自然语言提问。一句话覆盖
        所有缺失项,避免来回多轮。
        """
        if not self.missing_required:
            return ""
        prompts = {
            "topic": "主题",
            "audience": "受众",
            "page_count": "页数",
            "length": "篇幅",
            "deliverable_type": "想要什么形式(PPT / Word / 直接答复)",
            "deadline": "什么时候要",
        }
        items = [prompts.get(k, k) for k in self.missing_required]
        joined = " / ".join(items)
        return f"为了更准确地处理,请补充下面信息:{joined}(可一并发我)"

    def as_system_block(self) -> str:
        """生成注入 system prompt 的结构化块。

        只有 should_clarify == False 时才用 — 字段齐全才有意义。
        """
        if self.extractor_failed:
            return ""
        lines = ["[USER_INTENT]"]
        if self.intent and self.intent != "unknown":
            lines.append(f"- intent: {self.intent}")
        if self.deliverable_type:
            lines.append(f"- deliverable_type: {self.deliverable_type}")
        if self.topic:
            lines.append(f"- topic: {self.topic}")
        if self.audience:
            lines.append(f"- audience: {self.audience}")
        if self.page_count:
            lines.append(f"- page_count: {self.page_count}")
        if self.length:
            lines.append(f"- length: {self.length}")
        if len(lines) == 1:
            return ""
        return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────
# Heuristic gate — should we even call the extractor?
# ──────────────────────────────────────────────────────────────────────


# 长输入大概率信息齐全,extractor 反而失真,直接 bypass 节省 ¥0.0003
_LONG_INPUT_THRESHOLD = 60

# 已经包含明显结构化片段(@提及、JSON、URL、文件路径)的输入也跳过
_STRUCTURED_HINT_RE = re.compile(
    r"(@\w|https?://|/[A-Za-z0-9_\-/]+\.\w{2,5}|\{\s*\"|```)"
)


def should_extract(user_message: str) -> bool:
    """判定是否值得调 extractor。返回 False 时 caller 直接走老路径。

    规则:
      * 输入超过 ``_LONG_INPUT_THRESHOLD`` 字符 → 不调
      * 输入包含结构化标记(URL / JSON / 文件路径 / @ 提及)→ 不调
      * 否则调
    """
    msg = (user_message or "").strip()
    if not msg:
        return False
    if len(msg) > _LONG_INPUT_THRESHOLD:
        return False
    if _STRUCTURED_HINT_RE.search(msg):
        return False
    return True


# ──────────────────────────────────────────────────────────────────────
# Prompt templates
# ──────────────────────────────────────────────────────────────────────


_SYSTEM_PROMPT_ZH = """你是一个意图提取器。从用户输入中提取以下字段,无法识别的字段填 null。

字段定义:
  intent            — 一级意图,从这个枚举里选:
                      create_pptx / create_doc / create_sheet /
                      edit_file / search_info / data_analysis /
                      coding / casual_chat / other
  deliverable_type  — 期望产出类型: pptx / docx / xlsx / code / answer / null
  topic             — 主题(如"AI 安全"、"Q3 销售复盘"),null 如不明
  audience          — 受众(如"高管"、"客户"、"同事"),null 如不明
  page_count        — PPT 页数(整数),null 如不适用 / 不明
  length            — 字数描述(如"500字以内"、"详尽"),null 如不明
  missing_required  — 该意图下,缺失的关键字段名(数组)。判断标准:
                       intent=create_pptx → topic, audience, page_count 都需要
                       intent=create_doc  → topic, length 需要
                       intent=search_info → topic 需要
                       其他意图           → 通常 [] (字段都可选)

只返回 JSON,无 markdown 围栏,无解释。"""


def _build_user_prompt(user_message: str) -> str:
    return f"用户输入: {user_message}\n\n返回 JSON:"


# ──────────────────────────────────────────────────────────────────────
# Core extraction
# ──────────────────────────────────────────────────────────────────────


_REQUIRED_BY_INTENT: dict[str, list[str]] = {
    "create_pptx":  ["topic", "audience", "page_count"],
    "create_doc":   ["topic", "length"],
    "create_sheet": ["topic"],
    "search_info":  ["topic"],
    "data_analysis": ["topic"],
}


def extract_intent(
    user_message: str,
    llm_caller: Optional[Callable[[str], str]] = None,
    *,
    system_prompt: Optional[str] = None,
) -> IntentResult:
    """主入口。

    Args:
        user_message: 用户当前 turn 的原始输入
        llm_caller: ``Callable[[str], str]``,接收完整 prompt 字符串,返回
            模型纯文本输出(应当是 JSON)。**测试时传 mock,生产时传一个
            包了 DeepSeek-Chat 之类便宜模型的闭包。** 为 None 时直接降级
            到启发式 fallback(不调任何 LLM)。
        system_prompt: 替换默认中文 system prompt(国际化场景用)。

    Returns:
        IntentResult。``extractor_failed=True`` 表示这次 extractor 调用 / 解析
        失败,caller 应当 fall back 到老路径(直接进主 LLM)而不是反问。
    """
    msg = (user_message or "").strip()
    if not msg:
        return IntentResult(extractor_failed=True)

    if llm_caller is None:
        # 不调 LLM 也能出一个保守结果 — 只填 intent="unknown",不反问
        return IntentResult(intent="unknown", extractor_failed=False)

    sys_p = system_prompt or _SYSTEM_PROMPT_ZH
    full_prompt = sys_p + "\n\n" + _build_user_prompt(msg)

    try:
        raw_text = llm_caller(full_prompt)
    except Exception as e:
        logger.warning("intent extractor LLM call failed: %s", e)
        return IntentResult(extractor_failed=True)

    # 解析 JSON(LLM 可能加 ```json 围栏 / 加解释,做一遍宽松剥离)
    parsed = _parse_json_loose(raw_text or "")
    if parsed is None:
        logger.warning("intent extractor returned unparsable JSON: %r",
                       (raw_text or "")[:200])
        return IntentResult(extractor_failed=True, raw={"_raw": raw_text})

    return _result_from_dict(parsed)


def _result_from_dict(d: dict) -> IntentResult:
    """把 LLM 返回的 dict 转成 IntentResult,补齐 missing_required 的兜底逻辑。"""
    if not isinstance(d, dict):
        return IntentResult(extractor_failed=True)

    def _opt_str(k: str) -> Optional[str]:
        v = d.get(k)
        if v is None or v == "" or v == "null":
            return None
        return str(v).strip() or None

    def _opt_int(k: str) -> Optional[int]:
        v = d.get(k)
        if v is None or v == "" or v == "null":
            return None
        try:
            n = int(v)
            return n if n > 0 else None
        except (TypeError, ValueError):
            return None

    intent = _opt_str("intent") or "unknown"
    res = IntentResult(
        intent=intent,
        deliverable_type=_opt_str("deliverable_type"),
        topic=_opt_str("topic"),
        audience=_opt_str("audience"),
        page_count=_opt_int("page_count"),
        length=_opt_str("length"),
        raw=d,
    )

    # missing_required: 优先用 LLM 返回的;若没有,基于 intent 自己推
    llm_missing = d.get("missing_required")
    if isinstance(llm_missing, list):
        res.missing_required = [str(x) for x in llm_missing if x]
    else:
        required = _REQUIRED_BY_INTENT.get(intent, [])
        res.missing_required = [
            k for k in required
            if getattr(res, k, None) in (None, "", 0)
        ]

    return res


# ──────────────────────────────────────────────────────────────────────
# JSON parsing — tolerant of LLM noise
# ──────────────────────────────────────────────────────────────────────


_JSON_FENCE_RE = re.compile(
    r"```(?:json|JSON)?\s*\n?(.*?)\n?```",
    re.DOTALL,
)


def _parse_json_loose(text: str) -> Optional[dict]:
    """宽松解析 — 容忍 ``` 围栏 / 围栏前后的解释文字。"""
    if not text:
        return None
    s = text.strip()

    # 先剥 markdown 围栏
    m = _JSON_FENCE_RE.search(s)
    if m:
        s = m.group(1).strip()

    # 直接解析
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else None
    except json.JSONDecodeError:
        pass

    # 失败:尝试找第一个 { ... } 块
    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        try:
            d = json.loads(s[start:end + 1])
            return d if isinstance(d, dict) else None
        except json.JSONDecodeError:
            return None
    return None


__all__ = [
    "IntentResult",
    "extract_intent",
    "should_extract",
]
