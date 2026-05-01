"""MemoryDream — 全量记忆维护循环。

灵感来自 gbrain 的 `dream` 命令：周期性扫描所有 agent 的记忆，
跑增量 consolidator 跑不到的"全局视角"清理：

  1. 全量调 ``MemoryConsolidator.consolidate(force=True)`` —— 每个
     agent 都跑一次（增量 consolidator 5 分钟一次的间隔在这里跳过）。
  2. **孤立 fact 检测** —— 找出长时间未被检索过 + 低 confidence 的
     L3 fact，按规则衰减或删除。增量 consolidator 只衰减"长时间未
     更新"的，但**未被检索过**是更强的信号（gbrain orphans 同款）。
  3. **知识库引用审计** —— 找出 ``legacy_kb`` 里没被任何 agent fact
     提及过的 entry，列入 candidates（不直接删，先报告）。

触发方式：
  * 手动：``POST /api/portal/memory/dream``（admin 权限）
  * 定时：每天凌晨 03:00 cron（注册到现有 scheduler）

返回 ``DreamReport`` 详细列出做了什么；UI 用它生成可读报告。
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("tudou.memory_dream")


# ── 阈值 ──
# 这些跟 MemoryConsolidator 的常量保持同源思路（30 天衰减），
# 但 dream 是全量、强制执行，所以"未检索"加严：
#   * 60 天未被检索 + confidence < 0.5  → 删除
#   * 30 天未被检索 + confidence < 0.3  → 删除
#   * 90 天未被检索（无论 confidence）  → confidence -= 0.2
ORPHAN_HARD_DELETE_DAYS = 60
ORPHAN_HARD_DELETE_CONF = 0.5
ORPHAN_LOW_CONF_DAYS = 30
ORPHAN_LOW_CONF_THRESHOLD = 0.3
ORPHAN_DECAY_DAYS = 90
ORPHAN_DECAY_RATE = 0.2

# KB entry 多久没被任何 fact / wiki 引用就算孤立
KB_ORPHAN_DAYS = 60


@dataclass
class DreamReport:
    """一次 dream 的执行报告。"""
    started_at: float = 0.0
    finished_at: float = 0.0
    agents_processed: int = 0
    consolidator_actions: dict[str, int] = field(default_factory=dict)
    # ↑ 累加 plans_resolved + facts_merged + decayed + deleted（来自
    # MemoryConsolidator）
    orphans_deleted: int = 0
    orphans_decayed: int = 0
    kb_orphan_candidates: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_s": self.finished_at - self.started_at,
            "agents_processed": self.agents_processed,
            "consolidator_actions": dict(self.consolidator_actions),
            "orphans_deleted": self.orphans_deleted,
            "orphans_decayed": self.orphans_decayed,
            "kb_orphan_candidates": list(self.kb_orphan_candidates),
            "errors": list(self.errors),
        }

    def to_markdown(self) -> str:
        """生成可读的 markdown 报告，前端 UI 直接展示。"""
        d = self.finished_at - self.started_at
        lines = [
            f"# 🌙 Memory Dream Report",
            "",
            f"- 开始: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.started_at))}",
            f"- 耗时: {d:.1f}s",
            f"- 处理 agent 数: {self.agents_processed}",
            "",
            "## 整理动作 (Consolidator)",
        ]
        if self.consolidator_actions:
            for k, v in self.consolidator_actions.items():
                lines.append(f"- {k}: **{v}**")
        else:
            lines.append("- (无)")
        lines += [
            "",
            "## 孤立 Fact 处理",
            f"- 删除（未访问 + 低置信度）: **{self.orphans_deleted}** 条",
            f"- 衰减（长期未访问）: **{self.orphans_decayed}** 条",
            "",
            "## 知识库孤立候选",
        ]
        if self.kb_orphan_candidates:
            lines.append(
                f"以下 {len(self.kb_orphan_candidates)} 条 KB entry 长期"
                f"未被任何 agent fact 引用，可考虑删除："
            )
            for c in self.kb_orphan_candidates[:20]:
                lines.append(f"- `{c.get('id', '?')}` — {c.get('title', '?')}")
            if len(self.kb_orphan_candidates) > 20:
                lines.append(f"- … 另有 {len(self.kb_orphan_candidates) - 20} 条")
        else:
            lines.append("- 无")
        if self.errors:
            lines += ["", "## 错误"]
            for e in self.errors[:10]:
                lines.append(f"- {e}")
        return "\n".join(lines)


class MemoryDream:
    """全量记忆维护协调器。

    单例模式，跟 ``MemoryManager`` 共用同一个 sqlite 连接；
    通过 ``app.core.memory.get_memory_manager()`` 拿到 mm 实例。
    """

    def __init__(self, memory_manager: Any = None,
                 consolidator: Any = None,
                 hub: Any = None):
        # 延迟绑定 —— 既允许测试注入，也允许运行时从全局 hub 拿
        self._mm = memory_manager
        self._consolidator = consolidator
        self._hub = hub
        self._last_dream_at: float = 0.0
        self._last_report: DreamReport | None = None

    # ── 入口 ──

    def dream_all(self, llm_call: Any = None) -> DreamReport:
        """跑一轮全量 dream。

        Args:
            llm_call: 可选 LLM 函数，传给 consolidator 的智能合并步骤。
                None → consolidator 走简单拼接 fallback。

        Returns:
            DreamReport
        """
        report = DreamReport(started_at=time.time())
        try:
            mm = self._resolve_mm()
            if mm is None:
                report.errors.append("MemoryManager not available")
                report.finished_at = time.time()
                return report

            # 1. Consolidator 全量跑一次
            self._run_consolidator_for_all(mm, report, llm_call)

            # 2. 孤立 fact 处理
            self._sweep_orphan_facts(mm, report)

            # 3. KB 引用审计
            try:
                self._audit_kb_references(mm, report)
            except Exception as e:
                report.errors.append(f"kb audit: {e}")
                logger.warning("dream KB audit failed: %s", e)

        except Exception as e:
            report.errors.append(f"dream_all top-level: {e}")
            logger.exception("dream_all failed")

        report.finished_at = time.time()
        self._last_dream_at = report.finished_at
        self._last_report = report

        action_total = sum(report.consolidator_actions.values())
        logger.info(
            "memory dream finished: %d agents, %d consolidator actions, "
            "%d orphans deleted, %d decayed, %d KB orphans, duration=%.1fs",
            report.agents_processed, action_total,
            report.orphans_deleted, report.orphans_decayed,
            len(report.kb_orphan_candidates),
            report.finished_at - report.started_at,
        )
        return report

    def last_report(self) -> DreamReport | None:
        return self._last_report

    # ── Internals ──

    def _resolve_mm(self):
        """获取 MemoryManager（优先用注入，否则从模块拿全局单例）。"""
        if self._mm is not None:
            return self._mm
        try:
            from .memory import get_memory_manager
            self._mm = get_memory_manager()
            return self._mm
        except Exception:
            return None

    def _resolve_consolidator(self):
        if self._consolidator is not None:
            return self._consolidator
        mm = self._resolve_mm()
        if mm is None:
            return None
        try:
            from .memory import MemoryConsolidator
            self._consolidator = MemoryConsolidator(mm)
            return self._consolidator
        except Exception:
            return None

    def _all_agent_ids(self, mm) -> list[str]:
        """从 hub.agents 拿 agent id 列表；hub 不可用时退到 sqlite。"""
        try:
            if self._hub is not None and hasattr(self._hub, "agents"):
                return list(self._hub.agents.keys())
        except Exception:
            pass
        # Fallback: distinct agent_id from semantic 表（可能漏没记忆的 agent，
        # 但 dream 主要就是处理有记忆的 agent，OK）
        try:
            rows = mm._conn.execute(
                "SELECT DISTINCT agent_id FROM memory_semantic"
            ).fetchall()
            return [r["agent_id"] for r in rows if r["agent_id"]]
        except sqlite3.OperationalError:
            return []

    def _run_consolidator_for_all(self, mm, report: DreamReport,
                                   llm_call: Any) -> None:
        cons = self._resolve_consolidator()
        if cons is None:
            report.errors.append("Consolidator not available")
            return
        agent_ids = self._all_agent_ids(mm)
        for aid in agent_ids:
            try:
                r = cons.consolidate(aid, llm_call=llm_call, force=True)
                if r.get("skipped"):
                    continue
                report.agents_processed += 1
                for k in ("plans_resolved", "facts_merged",
                           "facts_decayed", "facts_deleted"):
                    if k in r:
                        report.consolidator_actions[k] = (
                            report.consolidator_actions.get(k, 0) + int(r.get(k, 0)))
            except Exception as e:
                report.errors.append(f"consolidate {aid[:8]}: {e}")
                logger.warning("dream consolidate %s failed: %s", aid, e)

    def _sweep_orphan_facts(self, mm, report: DreamReport) -> None:
        """处理"未检索 + 低 confidence" 的孤立 fact。

        三层策略（最严到最宽）：
          1. 60 天未访问 + confidence < 0.5  → DELETE
          2. 30 天未访问 + confidence < 0.3  → DELETE
          3. 90 天未访问（任何 confidence） → confidence -= 0.2
        """
        now = time.time()

        def _seconds(days: float) -> float:
            return days * 86400.0

        try:
            with mm._rlock:
                # 第一层：硬删 —— 60 天未活跃 + 低中置信度。
                # "未活跃" = (last_accessed_at < threshold) 或 (从未访问过
                # 且 created_at < threshold)。后半句捕住"创建后从未被检索
                # 过"的孤立 fact —— gbrain orphans 同款。
                hard_threshold = now - _seconds(ORPHAN_HARD_DELETE_DAYS)
                cur = mm._conn.execute(
                    "DELETE FROM memory_semantic "
                    "WHERE confidence < ? "
                    "  AND ("
                    "       (last_accessed_at > 0 AND last_accessed_at < ?)"
                    "    OR (last_accessed_at = 0 AND created_at > 0 AND created_at < ?)"
                    "  )",
                    (ORPHAN_HARD_DELETE_CONF, hard_threshold, hard_threshold),
                )
                report.orphans_deleted += cur.rowcount or 0

                # 第二层：30 天未活跃 + 更低置信度
                low_threshold = now - _seconds(ORPHAN_LOW_CONF_DAYS)
                cur = mm._conn.execute(
                    "DELETE FROM memory_semantic "
                    "WHERE confidence < ? "
                    "  AND ("
                    "       (last_accessed_at > 0 AND last_accessed_at < ?)"
                    "    OR (last_accessed_at = 0 AND created_at > 0 AND created_at < ?)"
                    "  )",
                    (ORPHAN_LOW_CONF_THRESHOLD, low_threshold, low_threshold),
                )
                report.orphans_deleted += cur.rowcount or 0

                # 第三层：长期未访问 → 衰减 confidence。
                # 注意 last_accessed_at == 0 表示从未被检索过 —— 用
                # created_at 作 fallback 阈值，避免新建 fact 立刻被衰减。
                cur = mm._conn.execute(
                    "UPDATE memory_semantic "
                    "SET confidence = MAX(0, confidence - ?) "
                    "WHERE ("
                    "    (last_accessed_at > 0 AND last_accessed_at < ?) "
                    " OR (last_accessed_at = 0 AND created_at < ?)"
                    ")",
                    (ORPHAN_DECAY_RATE,
                     now - _seconds(ORPHAN_DECAY_DAYS),
                     now - _seconds(ORPHAN_DECAY_DAYS)),
                )
                report.orphans_decayed += cur.rowcount or 0

                # 衰减后置信度低于地板值 → 顺手清掉
                cur = mm._conn.execute(
                    "DELETE FROM memory_semantic WHERE confidence < 0.05"
                )
                report.orphans_deleted += cur.rowcount or 0

                mm._conn.commit()
        except Exception as e:
            report.errors.append(f"orphan sweep: {e}")
            logger.warning("dream orphan sweep failed: %s", e)

    def _audit_kb_references(self, mm, report: DreamReport) -> None:
        """找 ``legacy_kb`` 里"很可能没人用"的 entry。

        规则（保守）：
          * KB entry 创建超过 60 天
          * 没有任何 L3 fact 的 content 提到它的 title 或 id
        命中只放进 candidates 报告，不直接删 —— 知识库 entry 删除是
        破坏性的，必须由人确认。
        """
        try:
            from .. import knowledge as _kb
            entries = _kb.list_entries() or []
        except Exception as e:
            report.errors.append(f"kb load: {e}")
            return

        now = time.time()
        threshold = now - KB_ORPHAN_DAYS * 86400.0

        # 拿所有 fact content 做一个简单包含检测（小数据量足够，
        # 几千 entry × 几千 fact 都还在毫秒级）
        try:
            fact_rows = mm._conn.execute(
                "SELECT content FROM memory_semantic"
            ).fetchall()
            corpus = "\n".join(str(r["content"] or "") for r in fact_rows).lower()
        except Exception:
            corpus = ""

        for e in entries:
            created = float(e.get("created_at") or 0)
            if created == 0 or created > threshold:
                continue  # 太新 → 跳过
            title = str(e.get("title") or "").strip()
            eid = str(e.get("id") or "").strip()
            if not title and not eid:
                continue
            referenced = False
            if title and title.lower() in corpus:
                referenced = True
            if not referenced and eid and eid.lower() in corpus:
                referenced = True
            if not referenced:
                report.kb_orphan_candidates.append({
                    "id": eid,
                    "title": title,
                    "age_days": int((now - created) / 86400.0),
                })


# ── 全局单例 ──
_dream_singleton: MemoryDream | None = None


def get_memory_dream() -> MemoryDream:
    global _dream_singleton
    if _dream_singleton is None:
        _dream_singleton = MemoryDream()
    return _dream_singleton
