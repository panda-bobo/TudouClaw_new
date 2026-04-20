"""
memory.py — 三层记忆架构 (Three-Layer Memory Architecture)

L1: Working Memory  — 当前对话最近 N 轮原文，直接拼入 messages
L2: Episodic Memory — 历史对话的 LLM 摘要，SQLite FTS5 全文检索
L3: Semantic Memory  — 长期事实（用户偏好、项目约定、规则）+ 经验库

Prompt assembly:
    system = persona + 工具描述 + L3 相关片段 + L2 相关片段
    messages = L1 最近 N 轮

Write-back:
    L1 超阈值 → LLM 生成摘要 → 存入 L2
    从 LLM 回答中提取事实 → 存入 L3
"""

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from ..defaults import DEFAULT_EMBEDDING_MODEL, SQLITE_CONNECT_TIMEOUT

logger = logging.getLogger("tudou.memory")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EpisodicEntry:
    """L2 — 一段对话的摘要。"""
    id: str = ""
    agent_id: str = ""
    summary: str = ""
    keywords: str = ""          # 逗号分隔关键词
    turn_start: int = 0         # 被摘要的消息范围 (turn index)
    turn_end: int = 0
    message_count: int = 0      # 被摘要的原始消息条数
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "agent_id": self.agent_id,
            "summary": self.summary, "keywords": self.keywords,
            "turn_start": self.turn_start, "turn_end": self.turn_end,
            "message_count": self.message_count,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "EpisodicEntry":
        return cls(
            id=d.get("id", ""), agent_id=d.get("agent_id", ""),
            summary=d.get("summary", ""), keywords=d.get("keywords", ""),
            turn_start=d.get("turn_start", 0), turn_end=d.get("turn_end", 0),
            message_count=d.get("message_count", 0),
            created_at=d.get("created_at", 0.0),
        )


@dataclass
class SemanticFact:
    """L3 — 长期结构化记忆。

    category 分类体系 (五层记忆模型):
      - "intent"        任务意图: 用户真实目标、约束条件、成功/失败标准
      - "reasoning"     决策逻辑: 为什么选这个方案、排除了什么、当时的假设
      - "outcome"       执行结果: 最终成功/失败、关键输出、状态变化、失败原因
      - "rule"          经验规则: 场景→方案、错误→修复、前置条件→必须先做什么
      - "reflection"    反思改进: 哪里低效、下次优先检查什么、可合并的步骤

    记忆原则: 只记对未来有用的信息，不记过程细节。
    Agent 记忆 = 经验 + 规则 + 结论，不是日志。

    兼容旧分类映射:
      decision → reasoning, goal → intent, action_done → outcome,
      action_plan → intent, context → reasoning, issue → rule,
      learned → rule, user_pref → rule, general → outcome
    """
    id: str = ""
    agent_id: str = ""
    category: str = "general"
    content: str = ""
    source: str = ""             # 来源描述
    confidence: float = 1.0
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "agent_id": self.agent_id,
            "category": self.category, "content": self.content,
            "source": self.source, "confidence": self.confidence,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "SemanticFact":
        return cls(
            id=d.get("id", ""), agent_id=d.get("agent_id", ""),
            category=d.get("category", "general"),
            content=d.get("content", ""),
            source=d.get("source", ""),
            confidence=d.get("confidence", 1.0),
            created_at=d.get("created_at", 0.0),
            updated_at=d.get("updated_at", 0.0),
        )


# ---------------------------------------------------------------------------
# Memory configuration
# ---------------------------------------------------------------------------

@dataclass
class MemoryConfig:
    """每个 Agent 的记忆配置。"""
    l1_max_turns: int = 10           # L1 保留最近 N 轮 (user+assistant = 1 turn)
    l2_compress_threshold: int = 15  # 首次触发压缩的轮数
    l2_retrieve_top_k: int = 3       # 检索 L2 摘要的 top-K 条
    l3_retrieve_top_k: int = 5       # 检索 L3 事实的 top-K 条
    l3_max_facts: int = 200          # 每个 agent 最多保留的 L3 事实数
    auto_extract_facts: bool = True  # 是否自动从回答中提取事实
    enabled: bool = True             # 总开关
    # Vector search (ChromaDB MCP) settings
    vector_search_enabled: bool = True   # 优先使用向量搜索（ChromaDB MCP可用时）
    vector_model: str = DEFAULT_EMBEDDING_MODEL
    vector_top_k: int = 5            # 向量搜索返回 top-K

    def to_dict(self) -> dict:
        return {
            "l1_max_turns": self.l1_max_turns,
            "l2_compress_threshold": self.l2_compress_threshold,
            "l2_retrieve_top_k": self.l2_retrieve_top_k,
            "l3_retrieve_top_k": self.l3_retrieve_top_k,
            "l3_max_facts": self.l3_max_facts,
            "auto_extract_facts": self.auto_extract_facts,
            "enabled": self.enabled,
            "vector_search_enabled": self.vector_search_enabled,
            "vector_model": self.vector_model,
            "vector_top_k": self.vector_top_k,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "MemoryConfig":
        return cls(
            l1_max_turns=d.get("l1_max_turns", 10),
            l2_compress_threshold=d.get("l2_compress_threshold", 15),
            l2_retrieve_top_k=d.get("l2_retrieve_top_k", 3),
            l3_retrieve_top_k=d.get("l3_retrieve_top_k", 5),
            l3_max_facts=d.get("l3_max_facts", 200),
            auto_extract_facts=d.get("auto_extract_facts", True),
            enabled=d.get("enabled", True),
            vector_search_enabled=d.get("vector_search_enabled", True),
            vector_model=d.get("vector_model", "all-MiniLM-L6-v2"),
            vector_top_k=d.get("vector_top_k", 5),
        )


# ---------------------------------------------------------------------------
# Progressive compression level tracking (per agent)
# ---------------------------------------------------------------------------

# Compression level determines summarization aggressiveness:
#   Level 0 (first): Detailed summary, preserve 80% of info (~15 turns)
#   Level 1: Moderate summary, preserve key decisions/outcomes (~30 turns)
#   Level 2+: Aggressive summary, only preserve rules/conclusions (~45+ turns)

_agent_compression_level: dict[str, int] = {}  # agent_id → level


# ---------------------------------------------------------------------------
# MemoryManager
# ---------------------------------------------------------------------------

class MemoryManager:
    """
    三层记忆管理器。

    每个 Agent 拥有独立的记忆空间（按 agent_id 隔离）。
    MemoryManager 是全局单例，通过 get_memory_manager() 获取。
    """

    def __init__(self, db_path: str = ""):
        if not db_path:
            import os
            data_dir = os.path.join(os.path.expanduser("~"), ".tudou_claw")
            os.makedirs(data_dir, exist_ok=True)
            db_path = os.path.join(data_dir, "tudou_claw.db")

        self._db_path = db_path
        self._rlock = threading.RLock()

        # 复用主数据库连接
        self._conn = sqlite3.connect(
            db_path, check_same_thread=False, timeout=SQLITE_CONNECT_TIMEOUT,
        )
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.row_factory = sqlite3.Row

        self._create_memory_tables()
        logger.info("MemoryManager initialized: %s", db_path)

    # ------------------------------------------------------------------
    # Schema — FTS5 tables for L2 & L3
    # ------------------------------------------------------------------

    def _create_memory_tables(self):
        """创建记忆相关表（含 FTS5 全文索引）。"""
        c = self._conn
        c.executescript("""
        -- L2: Episodic Memory — 对话摘要
        CREATE TABLE IF NOT EXISTS memory_episodic (
            id          TEXT PRIMARY KEY,
            agent_id    TEXT NOT NULL DEFAULT '',
            summary     TEXT NOT NULL DEFAULT '',
            keywords    TEXT NOT NULL DEFAULT '',
            turn_start  INTEGER NOT NULL DEFAULT 0,
            turn_end    INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0,
            created_at  REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_me_agent ON memory_episodic(agent_id);
        CREATE INDEX IF NOT EXISTS idx_me_created ON memory_episodic(created_at);

        -- L2 FTS5 全文索引
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_episodic_fts USING fts5(
            summary, keywords,
            content=memory_episodic,
            content_rowid=rowid
        );

        -- L2 FTS5 触发器 — 自动同步
        CREATE TRIGGER IF NOT EXISTS memory_episodic_ai AFTER INSERT ON memory_episodic BEGIN
            INSERT INTO memory_episodic_fts(rowid, summary, keywords)
            VALUES (new.rowid, new.summary, new.keywords);
        END;
        CREATE TRIGGER IF NOT EXISTS memory_episodic_ad AFTER DELETE ON memory_episodic BEGIN
            INSERT INTO memory_episodic_fts(memory_episodic_fts, rowid, summary, keywords)
            VALUES ('delete', old.rowid, old.summary, old.keywords);
        END;
        CREATE TRIGGER IF NOT EXISTS memory_episodic_au AFTER UPDATE ON memory_episodic BEGIN
            INSERT INTO memory_episodic_fts(memory_episodic_fts, rowid, summary, keywords)
            VALUES ('delete', old.rowid, old.summary, old.keywords);
            INSERT INTO memory_episodic_fts(rowid, summary, keywords)
            VALUES (new.rowid, new.summary, new.keywords);
        END;

        -- L3: Semantic Memory — 长期事实
        CREATE TABLE IF NOT EXISTS memory_semantic (
            id          TEXT PRIMARY KEY,
            agent_id    TEXT NOT NULL DEFAULT '',
            category    TEXT NOT NULL DEFAULT 'general',
            content     TEXT NOT NULL DEFAULT '',
            source      TEXT NOT NULL DEFAULT '',
            confidence  REAL NOT NULL DEFAULT 1.0,
            created_at  REAL NOT NULL DEFAULT 0,
            updated_at  REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS idx_ms_agent ON memory_semantic(agent_id);
        CREATE INDEX IF NOT EXISTS idx_ms_cat ON memory_semantic(category);

        -- L3 FTS5 全文索引
        CREATE VIRTUAL TABLE IF NOT EXISTS memory_semantic_fts USING fts5(
            content, category, source,
            content=memory_semantic,
            content_rowid=rowid
        );

        -- L3 FTS5 触发器
        CREATE TRIGGER IF NOT EXISTS memory_semantic_ai AFTER INSERT ON memory_semantic BEGIN
            INSERT INTO memory_semantic_fts(rowid, content, category, source)
            VALUES (new.rowid, new.content, new.category, new.source);
        END;
        CREATE TRIGGER IF NOT EXISTS memory_semantic_ad AFTER DELETE ON memory_semantic BEGIN
            INSERT INTO memory_semantic_fts(memory_semantic_fts, rowid, content, category, source)
            VALUES ('delete', old.rowid, old.content, old.category, old.source);
        END;
        CREATE TRIGGER IF NOT EXISTS memory_semantic_au AFTER UPDATE ON memory_semantic BEGIN
            INSERT INTO memory_semantic_fts(memory_semantic_fts, rowid, content, category, source)
            VALUES ('delete', old.rowid, old.content, old.category, old.source);
            INSERT INTO memory_semantic_fts(rowid, content, category, source)
            VALUES (new.rowid, new.content, new.category, new.source);
        END;

        -- 记忆配置表
        CREATE TABLE IF NOT EXISTS memory_config (
            agent_id    TEXT PRIMARY KEY,
            data        TEXT NOT NULL DEFAULT '{}'
        );
        """)
        c.commit()

    # ------------------------------------------------------------------
    # Config per agent
    # ------------------------------------------------------------------

    def get_config(self, agent_id: str) -> MemoryConfig:
        """获取 agent 的记忆配置。"""
        row = self._conn.execute(
            "SELECT data FROM memory_config WHERE agent_id=?",
            (agent_id,),
        ).fetchone()
        if row:
            try:
                return MemoryConfig.from_dict(json.loads(row["data"]))
            except (json.JSONDecodeError, TypeError):
                pass
        return MemoryConfig()

    def save_config(self, agent_id: str, config: MemoryConfig):
        """保存 agent 的记忆配置。"""
        with self._rlock:
            self._conn.execute(
                "INSERT OR REPLACE INTO memory_config(agent_id, data) VALUES(?,?)",
                (agent_id, json.dumps(config.to_dict(), ensure_ascii=False)),
            )
            self._conn.commit()

    # ==================================================================
    # L1: Working Memory
    # ==================================================================
    # L1 lives in Agent.messages — no persistence needed here.
    # MemoryManager provides helpers to split messages into L1 window.

    def get_l1_messages(self, messages: list[dict],
                        max_turns: int = 10) -> list[dict]:
        """
        从完整 messages 中提取 L1 窗口（最近 N 轮）。

        一轮 = 一对 user + assistant 消息。
        system 消息总是保留。tool/tool_calls 消息跟随所属轮次。
        """
        if not messages:
            return []

        # 分离 system 消息
        system_msgs = []
        non_system = []
        for m in messages:
            if m.get("role") == "system":
                system_msgs.append(m)
            else:
                non_system.append(m)

        if len(non_system) == 0:
            return system_msgs

        # 计算轮次：每个 user 消息开始一轮
        turns: list[list[dict]] = []
        current_turn: list[dict] = []
        for m in non_system:
            if m.get("role") == "user" and current_turn:
                turns.append(current_turn)
                current_turn = [m]
            else:
                current_turn.append(m)
        if current_turn:
            turns.append(current_turn)

        # 只保留最近 max_turns 轮
        recent_turns = turns[-max_turns:] if len(turns) > max_turns else turns
        l1_messages = []
        for turn in recent_turns:
            l1_messages.extend(turn)

        # system 只保留第一条（最新的 system prompt）
        result = []
        if system_msgs:
            result.append(system_msgs[0])
        result.extend(l1_messages)
        return result

    def get_overflow_messages(self, messages: list[dict],
                              max_turns: int = 10) -> list[dict]:
        """
        获取超出 L1 窗口的旧消息（用于压缩到 L2）。
        """
        if not messages:
            return []

        non_system = [m for m in messages if m.get("role") != "system"]
        turns: list[list[dict]] = []
        current_turn: list[dict] = []
        for m in non_system:
            if m.get("role") == "user" and current_turn:
                turns.append(current_turn)
                current_turn = [m]
            else:
                current_turn.append(m)
        if current_turn:
            turns.append(current_turn)

        if len(turns) <= max_turns:
            return []

        overflow_turns = turns[:-max_turns]
        overflow = []
        for turn in overflow_turns:
            overflow.extend(turn)
        return overflow

    # ==================================================================
    # L2: Episodic Memory — 对话摘要
    # ==================================================================

    def save_episodic(self, entry: EpisodicEntry):
        """保存一条 L2 摘要。"""
        if not entry.id:
            entry.id = str(uuid.uuid4())
        if not entry.created_at:
            entry.created_at = time.time()

        with self._rlock:
            self._conn.execute("""
                INSERT OR REPLACE INTO memory_episodic
                (id, agent_id, summary, keywords, turn_start, turn_end,
                 message_count, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                entry.id, entry.agent_id, entry.summary, entry.keywords,
                entry.turn_start, entry.turn_end, entry.message_count,
                entry.created_at,
            ))
            self._conn.commit()

        # Also store in ChromaDB for vector search
        self.vector_store_episodic(entry)

    def search_episodic(self, agent_id: str, query: str,
                        top_k: int = 3) -> list[EpisodicEntry]:
        """
        FTS5 搜索 L2 摘要。返回按相关性排序的结果。
        """
        if not query.strip():
            # 没有查询词时，返回最近的摘要
            return self.get_recent_episodic(agent_id, limit=top_k)

        # 构建 FTS5 查询：对每个词进行 OR 匹配
        tokens = self._tokenize_query(query)
        if not tokens:
            return self.get_recent_episodic(agent_id, limit=top_k)

        fts_query = " OR ".join(tokens)
        try:
            rows = self._conn.execute("""
                SELECT e.*, rank
                FROM memory_episodic e
                JOIN memory_episodic_fts fts ON e.rowid = fts.rowid
                WHERE memory_episodic_fts MATCH ?
                  AND e.agent_id = ?
                ORDER BY rank
                LIMIT ?
            """, (fts_query, agent_id, top_k)).fetchall()
        except sqlite3.OperationalError:
            # FTS 查询语法错误时降级为最近记录
            logger.warning("FTS query failed for episodic: %s", fts_query)
            return self.get_recent_episodic(agent_id, limit=top_k)

        return [EpisodicEntry.from_dict(dict(r)) for r in rows]

    def get_recent_episodic(self, agent_id: str,
                            limit: int = 5) -> list[EpisodicEntry]:
        """获取最近的 L2 摘要。"""
        rows = self._conn.execute("""
            SELECT * FROM memory_episodic
            WHERE agent_id = ?
            ORDER BY created_at DESC
            LIMIT ?
        """, (agent_id, limit)).fetchall()
        return [EpisodicEntry.from_dict(dict(r)) for r in rows]

    def count_episodic(self, agent_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM memory_episodic WHERE agent_id=?",
            (agent_id,),
        ).fetchone()
        return row["cnt"] if row else 0

    def delete_episodic(self, entry_id: str):
        with self._rlock:
            self._conn.execute(
                "DELETE FROM memory_episodic WHERE id=?", (entry_id,),
            )
            self._conn.commit()

    def clear_episodic(self, agent_id: str):
        """清空某个 agent 的所有 L2 记忆。"""
        with self._rlock:
            self._conn.execute(
                "DELETE FROM memory_episodic WHERE agent_id=?", (agent_id,),
            )
            self._conn.commit()

    # ==================================================================
    # L3: Semantic Memory — 长期事实
    # ==================================================================

    def save_fact(self, fact: SemanticFact, preserve_timestamps: bool = False):
        """保存一条 L3 事实。

        Args:
            preserve_timestamps: 为 True 时保留 fact 上的 created_at/updated_at，
                                 用于持久化恢复或 consolidation 等场景。
        """
        if not fact.id:
            fact.id = str(uuid.uuid4())
        now = time.time()
        if not preserve_timestamps:
            if not fact.created_at:
                fact.created_at = now
            fact.updated_at = now
        else:
            if not fact.created_at:
                fact.created_at = now
            if not fact.updated_at:
                fact.updated_at = now

        with self._rlock:
            self._conn.execute("""
                INSERT OR REPLACE INTO memory_semantic
                (id, agent_id, category, content, source, confidence,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                fact.id, fact.agent_id, fact.category, fact.content,
                fact.source, fact.confidence, fact.created_at, fact.updated_at,
            ))
            self._conn.commit()

        # Also store in ChromaDB for vector search
        self.vector_store_fact(fact)

    def search_facts(self, agent_id: str, query: str,
                     top_k: int = 5, category: str = "") -> list[SemanticFact]:
        """FTS5 搜索 L3 事实。"""
        if not query.strip():
            return self.get_recent_facts(agent_id, limit=top_k, category=category)

        tokens = self._tokenize_query(query)
        if not tokens:
            return self.get_recent_facts(agent_id, limit=top_k, category=category)

        fts_query = " OR ".join(tokens)
        try:
            if category:
                rows = self._conn.execute("""
                    SELECT s.*, rank
                    FROM memory_semantic s
                    JOIN memory_semantic_fts fts ON s.rowid = fts.rowid
                    WHERE memory_semantic_fts MATCH ?
                      AND s.agent_id = ?
                      AND s.category = ?
                    ORDER BY rank
                    LIMIT ?
                """, (fts_query, agent_id, category, top_k)).fetchall()
            else:
                rows = self._conn.execute("""
                    SELECT s.*, rank
                    FROM memory_semantic s
                    JOIN memory_semantic_fts fts ON s.rowid = fts.rowid
                    WHERE memory_semantic_fts MATCH ?
                      AND s.agent_id = ?
                    ORDER BY rank
                    LIMIT ?
                """, (fts_query, agent_id, top_k)).fetchall()
        except sqlite3.OperationalError:
            logger.warning("FTS query failed for semantic: %s", fts_query)
            return self.get_recent_facts(agent_id, limit=top_k, category=category)

        return [SemanticFact.from_dict(dict(r)) for r in rows]

    def get_recent_facts(self, agent_id: str, limit: int = 10,
                         category: str = "") -> list[SemanticFact]:
        """获取最近的 L3 事实。"""
        if category:
            rows = self._conn.execute("""
                SELECT * FROM memory_semantic
                WHERE agent_id = ? AND category = ?
                ORDER BY updated_at DESC LIMIT ?
            """, (agent_id, category, limit)).fetchall()
        else:
            rows = self._conn.execute("""
                SELECT * FROM memory_semantic
                WHERE agent_id = ?
                ORDER BY updated_at DESC LIMIT ?
            """, (agent_id, limit)).fetchall()
        return [SemanticFact.from_dict(dict(r)) for r in rows]

    def count_facts(self, agent_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM memory_semantic WHERE agent_id=?",
            (agent_id,),
        ).fetchone()
        return row["cnt"] if row else 0

    def delete_fact(self, fact_id: str):
        with self._rlock:
            self._conn.execute(
                "DELETE FROM memory_semantic WHERE id=?", (fact_id,),
            )
            self._conn.commit()
        # 同步删除 ChromaDB 中的向量
        if self._check_chromadb_available():
            try:
                coll = self._get_chroma_collection("memory_facts")
                coll.delete(ids=[fact_id])
            except Exception:
                pass

    def clear_facts(self, agent_id: str):
        """清空某个 agent 的所有 L3 记忆。"""
        with self._rlock:
            self._conn.execute(
                "DELETE FROM memory_semantic WHERE agent_id=?", (agent_id,),
            )
            self._conn.commit()

    # ==================================================================
    # ChromaDB Vector Search with auto-fallback to FTS5
    # ==================================================================

    def _check_chromadb_available(self) -> bool:
        """Check if ChromaDB + sentence-transformers are installed."""
        if not hasattr(self, '_chromadb_available'):
            try:
                import chromadb  # noqa: F401
                from chromadb.utils import embedding_functions  # noqa: F401
                self._chromadb_available = True
                logger.info("ChromaDB detected — vector search enabled")
            except ImportError:
                self._chromadb_available = False
                logger.debug("ChromaDB not installed — using FTS5 only")
        return self._chromadb_available

    def _get_chromadb_client(self):
        """Lazily initialize ChromaDB client for in-process use."""
        if not hasattr(self, '_chroma_client') or self._chroma_client is None:
            import chromadb
            from chromadb.utils import embedding_functions

            persist_dir = str(Path(self._db_path).parent / "chromadb")
            os.makedirs(persist_dir, exist_ok=True)

            self._chroma_client = chromadb.PersistentClient(path=persist_dir)
            model_name = DEFAULT_EMBEDDING_MODEL
            self._chroma_embed_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=model_name
            )
            logger.info(f"ChromaDB client initialized: {persist_dir}")
        return self._chroma_client

    def _get_chroma_collection(self, name: str):
        """Get or create a ChromaDB collection."""
        client = self._get_chromadb_client()
        return client.get_or_create_collection(
            name=f"tudou_{name}",
            embedding_function=self._chroma_embed_fn,
            metadata={"hnsw:space": "cosine"},
        )

    # ------------------------------------------------------------------
    # Vector write quality controls
    # ------------------------------------------------------------------
    # 向量化写入不是"来什么存什么"，而是经过质量门控:
    #   1. 最小内容长度 — 太短的文本 embedding 质量差，不如不存
    #   2. 置信度门槛 — 低置信度的 fact 用 FTS5 就够了
    #   3. 语义去重 — 用 cosine distance 检测已有近似文档，做 upsert 而非 insert
    #   4. 元数据丰富 — 写入时带上 agent_id / category / source / timestamp
    #      这样查询时可以用 where 过滤，避免跨 agent 污染
    # ------------------------------------------------------------------

    _VEC_MIN_CONTENT_LEN = 10     # 少于 10 字符不值得向量化
    _VEC_MIN_FACT_CONFIDENCE = 0.3  # 置信度 < 0.3 不写向量库
    _VEC_DEDUP_DISTANCE = 0.08    # cosine distance < 0.08 认为重复

    def _vector_quality_check_fact(self, fact: SemanticFact) -> bool:
        """门控: 该 fact 是否值得写入向量库。"""
        # 1. 内容长度
        content = (fact.content or "").strip()
        if len(content) < self._VEC_MIN_CONTENT_LEN:
            logger.debug("Vector skip fact %s: too short (%d chars)", fact.id, len(content))
            return False
        # 2. 置信度
        if fact.confidence < self._VEC_MIN_FACT_CONFIDENCE:
            logger.debug("Vector skip fact %s: low confidence %.2f", fact.id, fact.confidence)
            return False
        return True

    def _vector_quality_check_episodic(self, entry: EpisodicEntry) -> bool:
        """门控: 该摘要是否值得写入向量库。"""
        summary = (entry.summary or "").strip()
        if len(summary) < self._VEC_MIN_CONTENT_LEN:
            logger.debug("Vector skip episodic %s: too short", entry.id)
            return False
        return True

    def vector_store_fact(self, fact: SemanticFact):
        """Store a fact in ChromaDB for vector search.

        写入流程:
          1. 检查 ChromaDB 是否可用
          2. 质量门控 (长度 + 置信度)
          3. 语义去重 (cosine distance < 0.08 → 更新而非新增)
          4. upsert 到向量库，带丰富元数据
        """
        if not self._check_chromadb_available():
            return
        if not self._vector_quality_check_fact(fact):
            return
        try:
            coll = self._get_chroma_collection("memory_facts")
            metadata = {
                "agent_id": fact.agent_id,
                "category": fact.category or "general",
                "source": fact.source or "",
                "confidence": fact.confidence,
                "created_at": fact.created_at or time.time(),
                "content_len": len(fact.content),
            }
            # 语义去重: 查询最近似的一条，distance < 阈值则视为重复 → 用同一 id upsert
            if coll.count() > 0:
                try:
                    dup_check = coll.query(
                        query_texts=[fact.content],
                        n_results=1,
                        where={"agent_id": fact.agent_id},
                    )
                    if (dup_check and dup_check.get("distances")
                            and dup_check["distances"][0]
                            and dup_check["distances"][0][0] < self._VEC_DEDUP_DISTANCE
                            and dup_check["ids"][0][0] != fact.id):
                        # 找到语义重复的旧文档 → 更新旧文档内容
                        old_id = dup_check["ids"][0][0]
                        logger.debug("Vector dedup fact: %s ≈ %s (dist=%.4f), updating old",
                                     fact.id, old_id, dup_check["distances"][0][0])
                        coll.update(ids=[old_id], documents=[fact.content], metadatas=[metadata])
                        return
                except Exception:
                    pass  # 去重失败不影响正常存储

            coll.upsert(
                ids=[fact.id],
                documents=[fact.content],
                metadatas=[metadata],
            )
            logger.debug("Vector stored fact %s (category=%s, confidence=%.2f)",
                         fact.id, fact.category, fact.confidence)
        except Exception as e:
            logger.warning(f"ChromaDB vector_store_fact failed: {e}")

    def vector_store_episodic(self, entry: EpisodicEntry):
        """Store an episodic entry in ChromaDB for vector search.

        向量化的内容是: summary + keywords (拼接)
        元数据: agent_id, turn_start, turn_end, created_at
        """
        if not self._check_chromadb_available():
            return
        if not self._vector_quality_check_episodic(entry):
            return
        try:
            coll = self._get_chroma_collection("memory_episodes")
            # 向量化文本 = 摘要 + 关键词
            text = entry.summary
            if entry.keywords:
                text += " | keywords: " + entry.keywords
            metadata = {
                "agent_id": entry.agent_id,
                "turn_start": entry.turn_start,
                "turn_end": entry.turn_end,
                "message_count": entry.message_count,
                "created_at": entry.created_at or time.time(),
            }
            coll.upsert(
                ids=[entry.id],
                documents=[text],
                metadatas=[metadata],
            )
            logger.debug("Vector stored episodic %s (turns %d-%d)",
                         entry.id, entry.turn_start, entry.turn_end)
        except Exception as e:
            logger.warning(f"ChromaDB vector_store_episodic failed: {e}")

    def vector_store_knowledge(self, entry_id: str, title: str, content: str,
                               tags: list[str] | None = None):
        """Store a Knowledge Wiki entry in ChromaDB for vector search.

        向量化的内容是: title + content (拼接, title 权重高)
        元数据: source="knowledge_wiki", tags
        这是第3个写入入口 — Knowledge Wiki 手动添加的知识条目。
        """
        if not self._check_chromadb_available():
            return
        text = (title or "").strip()
        body = (content or "").strip()
        if len(text) + len(body) < self._VEC_MIN_CONTENT_LEN:
            return
        # 标题重复两遍提升权重 (title 是高价值摘要)
        doc_text = f"{text}. {text}. {body}"
        try:
            coll = self._get_chroma_collection("knowledge")
            metadata = {
                "agent_id": "__shared__",  # Knowledge Wiki 是全局共享的
                "source": "knowledge_wiki",
                "title": title or "",
                "tags": ",".join(tags or []),
                "created_at": time.time(),
            }
            coll.upsert(ids=[entry_id], documents=[doc_text], metadatas=[metadata])
            logger.debug("Vector stored knowledge %s: %s", entry_id, title)
        except Exception as e:
            logger.warning(f"ChromaDB vector_store_knowledge failed: {e}")

    def vector_delete_knowledge(self, entry_id: str):
        """Delete a Knowledge Wiki entry from ChromaDB."""
        if not self._check_chromadb_available():
            return
        try:
            coll = self._get_chroma_collection("knowledge")
            coll.delete(ids=[entry_id])
        except Exception as e:
            logger.warning(f"ChromaDB vector_delete_knowledge failed: {e}")

    def search_knowledge_vector(self, query: str, top_k: int = 5) -> list[dict]:
        """Vector search for Knowledge Wiki entries. Returns [{id, title, content, distance}]."""
        if not self._check_chromadb_available():
            return []
        try:
            coll = self._get_chroma_collection("knowledge")
            if coll.count() == 0:
                return []
            results = coll.query(
                query_texts=[query],
                n_results=min(top_k, 20),
            )
            items = []
            if results and results.get("ids") and results["ids"][0]:
                for i, doc_id in enumerate(results["ids"][0]):
                    meta = results["metadatas"][0][i] if results.get("metadatas") else {}
                    items.append({
                        "id": doc_id,
                        "title": meta.get("title", ""),
                        "tags": meta.get("tags", "").split(",") if meta.get("tags") else [],
                        "distance": results["distances"][0][i] if results.get("distances") else 0,
                    })
            return items
        except Exception as e:
            logger.warning(f"ChromaDB search_knowledge failed: {e}")
            return []

    # ------------------------------------------------------------------
    # Migrate existing data: FTS5 → ChromaDB (one-time sync)
    # ------------------------------------------------------------------

    def migrate_to_vector(self, agent_id: str | None = None) -> dict:
        """将已有的 FTS5 数据一次性同步到 ChromaDB。

        这是第4个写入入口 — 存量数据迁移。
        可以指定某个 agent_id，也可以不指定（迁移所有）。

        Returns: {facts_synced: int, episodes_synced: int, knowledge_synced: int, skipped: int}
        """
        if not self._check_chromadb_available():
            return {"error": "ChromaDB not available"}

        stats = {"facts_synced": 0, "episodes_synced": 0, "knowledge_synced": 0, "skipped": 0}

        # 1. 迁移 L3 facts
        try:
            if agent_id:
                where_clause = "WHERE agent_id = ?"
                params = (agent_id,)
            else:
                where_clause = ""
                params = ()
            rows = self._conn.execute(
                f"SELECT * FROM memory_semantic {where_clause} ORDER BY created_at DESC",
                params
            ).fetchall()
            for r in rows:
                fact = SemanticFact.from_dict(dict(r))
                if self._vector_quality_check_fact(fact):
                    self.vector_store_fact(fact)
                    stats["facts_synced"] += 1
                else:
                    stats["skipped"] += 1
        except Exception as e:
            logger.warning(f"Fact migration failed: {e}")

        # 2. 迁移 L2 episodes
        try:
            rows = self._conn.execute(
                f"SELECT * FROM memory_episodic {where_clause} ORDER BY created_at DESC",
                params
            ).fetchall()
            for r in rows:
                entry = EpisodicEntry.from_dict(dict(r))
                if self._vector_quality_check_episodic(entry):
                    self.vector_store_episodic(entry)
                    stats["episodes_synced"] += 1
                else:
                    stats["skipped"] += 1
        except Exception as e:
            logger.warning(f"Episode migration failed: {e}")

        # 3. 迁移 Knowledge Wiki
        try:
            from ..knowledge import list_entries as kb_list_entries
        except ImportError:
            try:
                from app.knowledge import list_entries as kb_list_entries
            except ImportError:
                kb_list_entries = None

        if kb_list_entries:
            try:
                for entry in kb_list_entries():
                    self.vector_store_knowledge(
                        entry_id=entry["id"],
                        title=entry.get("title", ""),
                        content=entry.get("content", ""),
                        tags=entry.get("tags"),
                    )
                    stats["knowledge_synced"] += 1
            except Exception as e:
                logger.warning(f"Knowledge migration failed: {e}")

        logger.info("Vector migration complete: %s", stats)
        return stats

    def get_vector_stats(self) -> dict:
        """获取向量库统计信息。"""
        if not self._check_chromadb_available():
            return {"available": False}
        try:
            client = self._get_chromadb_client()
            prefix = "tudou_"
            collections = client.list_collections()
            stats = {"available": True, "collections": {}}
            for c in collections:
                name = c.name
                display = name[len(prefix):] if name.startswith(prefix) else name
                stats["collections"][display] = {
                    "count": c.count(),
                    "full_name": name,
                }
            return stats
        except Exception as e:
            return {"available": False, "error": str(e)}

    def search_facts_vector(self, agent_id: str, query: str,
                            top_k: int = 5, category: str = "") -> list[SemanticFact]:
        """Vector search for L3 facts using ChromaDB. Fallback to FTS5."""
        if not self._check_chromadb_available():
            return self.search_facts(agent_id, query, top_k, category)

        try:
            coll = self._get_chroma_collection("memory_facts")
            if coll.count() == 0:
                return self.search_facts(agent_id, query, top_k, category)

            where_filter = {"agent_id": agent_id}
            if category:
                where_filter = {"$and": [
                    {"agent_id": agent_id},
                    {"category": category},
                ]}

            results = coll.query(
                query_texts=[query],
                n_results=min(top_k, 50),
                where=where_filter,
            )

            if not results or not results.get("ids") or not results["ids"][0]:
                return self.search_facts(agent_id, query, top_k, category)

            # Convert ChromaDB results back to SemanticFact objects
            facts = []
            for i, doc_id in enumerate(results["ids"][0]):
                meta = results["metadatas"][0][i] if results.get("metadatas") else {}
                content = results["documents"][0][i] if results.get("documents") else ""
                facts.append(SemanticFact(
                    id=doc_id,
                    agent_id=agent_id,
                    category=meta.get("category", "general"),
                    content=content,
                    source=meta.get("source", ""),
                    confidence=meta.get("confidence", 0.5),
                    created_at=meta.get("created_at", 0),
                ))
            return facts

        except Exception as e:
            logger.warning(f"ChromaDB search_facts failed, falling back to FTS5: {e}")
            return self.search_facts(agent_id, query, top_k, category)

    def search_episodic_vector(self, agent_id: str, query: str,
                               top_k: int = 3) -> list[EpisodicEntry]:
        """Vector search for L2 episodes using ChromaDB. Fallback to FTS5."""
        if not self._check_chromadb_available():
            return self.search_episodic(agent_id, query, top_k)

        try:
            coll = self._get_chroma_collection("memory_episodes")
            if coll.count() == 0:
                return self.search_episodic(agent_id, query, top_k)

            results = coll.query(
                query_texts=[query],
                n_results=min(top_k, 50),
                where={"agent_id": agent_id},
            )

            if not results or not results.get("ids") or not results["ids"][0]:
                return self.search_episodic(agent_id, query, top_k)

            entries = []
            for i, doc_id in enumerate(results["ids"][0]):
                meta = results["metadatas"][0][i] if results.get("metadatas") else {}
                content = results["documents"][0][i] if results.get("documents") else ""
                entries.append(EpisodicEntry(
                    id=doc_id,
                    agent_id=agent_id,
                    summary=content,
                    keywords="",
                    turn_start=meta.get("turn_start", 0),
                    turn_end=meta.get("turn_end", 0),
                    message_count=0,
                    created_at=meta.get("created_at", 0),
                ))
            return entries

        except Exception as e:
            logger.warning(f"ChromaDB search_episodic failed, falling back to FTS5: {e}")
            return self.search_episodic(agent_id, query, top_k)

    # ==================================================================
    # Prompt Assembly — 检索 L2+L3 注入 system prompt
    # ==================================================================

    # 分类优先级: 高优先级的记忆排在前面，模型更容易关注
    _CATEGORY_PRIORITY = {
        "intent": 0,       # 任务意图 — 最重要，理解用户到底要什么
        "rule": 1,         # 经验规则 — 场景→方案，可复用知识
        "reasoning": 2,    # 决策逻辑 — 为什么这么做，避免重复讨论
        "reflection": 3,   # 反思改进 — Agent 进化，下次做得更好
        "outcome": 4,      # 执行结果 — 了解进度和状态
    }

    _CATEGORY_LABELS = {
        "intent": "🎯 意图",
        "rule": "📏 规则",
        "reasoning": "🧠 决策",
        "reflection": "💡 反思",
        "outcome": "✅ 结果",
        # ── legacy categories (backward compat for existing DB rows) ──
        "decision": "🧠 决策",
        "goal": "🎯 意图",
        "action_done": "✅ 结果",
        "action_plan": "🎯 意图",
        "context": "🧠 决策",
        "issue": "📏 规则",
        "learned": "📏 规则",
        "user_pref": "📏 规则",
        "general": "✅ 结果",
        "project_rule": "📏 规则",
    }

    # Map legacy categories to new 5-layer system for new writes
    _LEGACY_CATEGORY_MAP = {
        "decision": "reasoning",
        "goal": "intent",
        "action_done": "outcome",
        "action_plan": "intent",
        "context": "reasoning",
        "issue": "rule",
        "learned": "rule",
        "user_pref": "rule",
        "general": "outcome",
        "project_rule": "rule",
    }

    def retrieve_for_prompt(self, agent_id: str,
                            current_query: str,
                            config: Optional[MemoryConfig] = None,
                            ) -> str:
        """
        根据当前用户输入检索相关记忆，返回注入 system prompt 的文本。

        输出按优先级分层:
          1. 待办/目标/规则 — 直接影响当前行动
          2. 决策/问题/上下文 — 需要知道的背景
          3. 已完成/经验/偏好 — 参考信息
          4. 历史对话摘要 — 上下文补充
        """
        if config is None:
            config = self.get_config(agent_id)

        if not config.enabled:
            return ""

        parts = []

        # Choose search strategy: vector (ChromaDB) or FTS5
        use_vector = config.vector_search_enabled and self._check_chromadb_available()

        # L3 事实检索
        if use_vector:
            facts = self.search_facts_vector(
                agent_id, current_query,
                top_k=config.l3_retrieve_top_k,
            )
        else:
            facts = self.search_facts(
                agent_id, current_query,
                top_k=config.l3_retrieve_top_k,
            )
        if facts:
            # 按分类优先级排序
            facts.sort(key=lambda f: self._CATEGORY_PRIORITY.get(f.category, 9))
            fact_lines = []
            for f in facts:
                label = self._CATEGORY_LABELS.get(f.category, f"[{f.category}]")
                fact_lines.append(f"- {label} {f.content}")
            parts.append("## Key Facts\n" + "\n".join(fact_lines))

        # L2 摘要检索
        if use_vector:
            episodes = self.search_episodic_vector(
                agent_id, current_query,
                top_k=config.l2_retrieve_top_k,
            )
        else:
            episodes = self.search_episodic(
                agent_id, current_query,
                top_k=config.l2_retrieve_top_k,
            )
        if episodes:
            ep_lines = []
            for ep in episodes:
                ep_lines.append(f"- {ep.summary}")
            parts.append("## Work History\n" + "\n".join(ep_lines))

        if not parts:
            return ""

        return "[Long-term Memory]\n" + "\n\n".join(parts)

    # ==================================================================
    # Write-back: L1→L2 压缩 (需要 LLM 调用)
    # ==================================================================

    def compress_to_episodic(self, agent_id: str,
                              messages: list[dict],
                              llm_call: Any = None,
                              turn_start: int = 0,
                              ) -> Optional[EpisodicEntry]:
        """
        将一批消息压缩为 L2 摘要（渐进式压缩）。

        压缩力度随 compression_level 递增:
          Level 0: 详细摘要，保留 80% 信息（五段式模板，段落完整）
          Level 1: 中等摘要，仅保留决策+结果+规则（省略过程）
          Level 2+: 高度压缩，仅保留规则和结论（3-5 行要点）

        参数:
            messages: 需要压缩的消息列表（通常是 L1 溢出部分）
            llm_call: 调用 LLM 的函数，签名: llm_call(prompt: str) -> str
            turn_start: 这批消息在全局对话中的起始轮次号

        返回:
            保存后的 EpisodicEntry
        """
        if not messages:
            return None

        conversation_text = self._format_messages_for_summary(messages)
        if not conversation_text.strip():
            return None

        # Track and increment compression level for this agent
        level = _agent_compression_level.get(agent_id, 0)

        if llm_call:
            summary, keywords = self._llm_summarize(
                conversation_text, llm_call, compression_level=level,
            )
        else:
            summary, keywords = self._simple_summarize(messages)

        if not summary:
            return None

        # Increment compression level for next time
        _agent_compression_level[agent_id] = level + 1

        entry = EpisodicEntry(
            agent_id=agent_id,
            summary=summary,
            keywords=keywords,
            turn_start=turn_start,
            turn_end=turn_start + self._count_turns(messages),
            message_count=len(messages),
        )
        self.save_episodic(entry)
        logger.info("Compressed %d messages to L2 (level=%d) for agent %s",
                     len(messages), level, agent_id)
        return entry

    def _llm_summarize(self, conversation_text: str,
                       llm_call: Any,
                       compression_level: int = 0) -> tuple[str, str]:
        """调用 LLM 生成对话摘要 + 关键词（渐进式压缩）。

        compression_level:
          0 — 详细五段式摘要 (保留 ~80% 信息)
          1 — 中等压缩: 仅保留决策+结果+规则 (省略过程描述)
          2+ — 高度压缩: 仅保留规则和关键结论 (3-5 行要点)
        """
        current_time = time.strftime("%Y-%m-%d %H:%M")

        if compression_level >= 2:
            # Level 2+: Aggressive — rules & conclusions only
            prompt = (
                "你是 Agent 的记忆压缩器。将对话**高度压缩**为可复用的规则和结论。\n\n"
                "只保留: 经验规则（if-then 模式）、关键结论、用户偏好。\n"
                "不保留: 过程、意图描述、中间步骤、失败重试。\n\n"
                f"当前时间: {current_time}\n\n"
                "对话内容:\n"
                f"{conversation_text}\n\n"
                "请按 JSON 格式回复:\n"
                '{"summary": "3-5 行要点，每行一条规则/结论", '
                '"keywords": "关键词1,关键词2"}\n\n'
                "示例:\n"
                '{"summary": "[规则] Ollama tool schema超15KB必须先裁剪。'
                '[规则] LLM 400错误优先检查payload大小。'
                '[结论] retry-without-tools机制已上线。", '
                '"keywords": "Ollama,payload,400"}'
            )
        elif compression_level == 1:
            # Level 1: Moderate — decisions, outcomes, rules
            prompt = (
                "你是 Agent 的记忆压缩器。将对话压缩为**决策+结果+规则**。\n\n"
                "保留: 做了什么决策(为什么)、最终结果、可复用的规则。\n"
                "省略: 意图描述、详细执行过程、中间状态。\n\n"
                f"当前时间: {current_time}\n\n"
                "对话内容:\n"
                f"{conversation_text}\n\n"
                "请按 JSON 格式回复:\n"
                "{\n"
                '  "summary": "按下面模板填写",\n'
                '  "keywords": "关键词1,关键词2,关键词3"\n'
                "}\n\n"
                "summary 模板（三段式）:\n"
                "【决策】选择……因为……\n"
                "【结果】成功/失败 + 关键输出\n"
                "【规则】场景→方案 / 错误→修复\n"
            )
        else:
            # Level 0: Detailed five-section summary
            prompt = (
                "你是 Agent 的记忆压缩器。将以下对话压缩为一条**结构化记忆条目**。\n\n"
                "核心原则: 记忆 = 经验 + 规则 + 结论，不是日志。\n"
                "不记录: 每条命令、终端输出、重试过程、系统噪音。\n\n"
                f"当前时间: {current_time}\n\n"
                "对话内容:\n"
                f"{conversation_text}\n\n"
                "请按以下 JSON 格式回复（不要包含其他内容）:\n"
                "{\n"
                '  "summary": "按下面模板填写",\n'
                '  "keywords": "关键词1,关键词2,关键词3"\n'
                "}\n\n"
                "summary 模板（五段式，每段可选，没有就省略）:\n"
                "【任务意图】用户想要……\n"
                "【决策逻辑】选择……因为……；排除……因为……\n"
                "【执行结果】成功/失败，原因是……；状态变化：……\n"
                "【经验规则】场景：…… → 应执行：……；错误：…… → 解决方案：……\n"
                "【改进策略】下次遇到同类任务，优先……\n\n"
                "示例:\n"
                '{"summary": "[2025-01-15] '
                '【任务意图】用户需要修复Ollama工具调用400错误。'
                '【决策逻辑】选择裁剪tool schema而非换模型，因为错误根因是schema超15KB限制。'
                '【执行结果】添加_validate_tools()校验+retry-without-tools机制，400错误修复。'
                '【经验规则】Ollama tool schema超15KB → 必须先裁剪再发送，否则400。'
                '【改进策略】下次遇到LLM 400错误，优先检查payload大小而非重试。", '
                '"keywords": "Ollama,tool_schema,400错误,payload限制"}'
            )
        try:
            result = llm_call(prompt)
            # 尝试解析 JSON
            result = result.strip()
            # 处理 markdown 代码块包裹的情况
            if result.startswith("```"):
                lines = result.split("\n")
                result = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
                result = result.strip()

            parsed = json.loads(result)
            summary = parsed.get("summary", result)
            keywords = parsed.get("keywords", "")
            return summary, keywords
        except (json.JSONDecodeError, Exception) as e:
            # JSON 解析失败，直接用返回文本作为摘要
            logger.warning("LLM summarize JSON parse failed: %s", e)
            if isinstance(result, str) and result:
                return result[:1000], ""
            return "", ""

    def _simple_summarize(self, messages: list[dict]) -> tuple[str, str]:
        """无 LLM 时的简单摘要提取。"""
        parts = []
        keywords_set = set()

        for m in messages:
            role = m.get("role", "")
            content = m.get("content", "")
            if not isinstance(content, str) or not content.strip():
                continue
            if role == "user":
                # 提取用户问题的前100字
                text = content.strip()[:100]
                parts.append(f"用户问: {text}")
            elif role == "assistant":
                text = content.strip()[:150]
                parts.append(f"助手答: {text}")

        summary = "; ".join(parts)
        if len(summary) > 500:
            summary = summary[:500] + "..."

        return summary, ",".join(list(keywords_set)[:10])

    # ==================================================================
    # Write-back: 从回答中提取 L3 事实
    # ==================================================================

    def extract_facts(self, agent_id: str,
                      user_message: str,
                      assistant_response: str,
                      llm_call: Any = None,
                      config: Optional[MemoryConfig] = None,
                      ) -> list[SemanticFact]:
        """
        从一轮对话中提取长期事实，存入 L3。

        仅在用户表达偏好、规则、项目约定等情况下提取。
        """
        if config is None:
            config = self.get_config(agent_id)

        if not config.auto_extract_facts or not config.enabled:
            return []

        if not llm_call:
            # 无 LLM 时不做事实提取
            return []

        # 上游已被 auto-compact 截断 → 提取出来的都是"无法分析"式垃圾，直接跳过
        if self._looks_like_compressed_placeholder(user_message) or \
                self._looks_like_compressed_placeholder(assistant_response):
            logger.debug(
                "extract_facts skipped for agent %s: upstream context is a "
                "compression placeholder", agent_id,
            )
            return []

        # 先检查是否值得提取（简单启发式）
        if not self._worth_extracting(user_message, assistant_response):
            return []

        current_time = time.strftime("%Y-%m-%d %H:%M")
        prompt = (
            "你是 Agent 的记忆提取器。从对话中提取**对未来有用的关键记忆**。\n\n"
            "核心原则: Agent 记忆 = 经验 + 规则 + 结论，不是日志。\n"
            "不记录: 每条命令本身、终端输出全文、无意义重试、状态轮询、系统噪音。\n\n"
            "五层记忆分类:\n\n"
            "| category | 记什么 | 举例 |\n"
            "|----------|--------|------|\n"
            "| intent | 任务的真实目标、用户意图、约束条件、成功标准 | "
            "\"用户需要将TudouClaw的记忆体系从流水账改为结构化五层模型，约束: 兼容旧数据\" |\n"
            "| reasoning | 为什么选这个方案、排除了什么、当时的假设和风险判断 | "
            "\"选择重构L3分类而非新建表，因为SQLite schema变更成本高；排除新增layer是因为会破坏现有检索逻辑\" |\n"
            "| outcome | 最终成功/失败/部分成功、关键输出、状态变化、失败原因 | "
            "\"记忆体系重构完成，5层分类上线；旧数据通过映射表兼容，无需迁移\" |\n"
            "| rule | 场景→用什么方案、错误→修复方案、前置条件→必须先做什么 | "
            "\"新分支首次推送必须加 -u 关联上游，否则推送失败\" |\n"
            "| reflection | 哪里低效、下次应优先检查什么、哪些步骤可以合并 | "
            "\"下次修改DB schema前应先检查是否有未迁移的旧数据，避免运行时报错\" |\n\n"
            f"当前时间: {current_time}\n\n"
            f"用户: {user_message[:1000]}\n"
            f"助手: {assistant_response[:1000]}\n\n"
            "提取规则:\n"
            "- 每条 content 必须是**自包含的完整句子**，脱离上下文也能理解\n"
            "- 只提取对未来有价值的信息，不提取过程细节和临时状态\n"
            "- rule 类型要抽象成「场景→方案」的 if-then 模式，不是叙述句\n"
            "- reasoning 要记录「为什么」而非「做了什么」\n"
            "- reflection 要有具体的改进动作，不是空泛的感想\n"
            "- confidence: 0.9=用户明确说的, 0.7=从对话推断的, 0.5=不太确定\n"
            "- 宁缺毋滥: 没有高价值信息就返回空数组 []\n\n"
            "返回 JSON 数组（不要包含其他内容）:\n"
            '[{"content": "自包含描述", "category": "intent|reasoning|outcome|rule|reflection", "confidence": 0.9}]'
        )
        try:
            result = llm_call(prompt)
            result = result.strip()
            if result.startswith("```"):
                lines = result.split("\n")
                result = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
                result = result.strip()

            facts_data = json.loads(result)
            if not isinstance(facts_data, list):
                return []

            saved_facts = []
            for fd in facts_data:
                if not isinstance(fd, dict) or not fd.get("content"):
                    continue
                # 去重：检查是否已有相似事实
                # 优先使用向量搜索去重 (语义相似度更准)，否则用 FTS5 + 词汇重叠
                if self._check_chromadb_available():
                    existing = self.search_facts_vector(
                        agent_id, fd["content"], top_k=1,
                    )
                else:
                    existing = self.search_facts(
                        agent_id, fd["content"], top_k=1,
                    )
                if existing and self._is_similar(existing[0].content, fd["content"]):
                    # 更新已有事实
                    existing[0].content = fd["content"]
                    existing[0].updated_at = time.time()
                    self.save_fact(existing[0])
                    saved_facts.append(existing[0])
                    continue

                fact = SemanticFact(
                    agent_id=agent_id,
                    category=fd.get("category", "general"),
                    content=fd["content"],
                    source=f"conversation:{time.strftime('%Y-%m-%d %H:%M')}",
                    confidence=fd.get("confidence", 0.7),
                )
                self.save_fact(fact)
                saved_facts.append(fact)

            # 清理过多的事实
            self._prune_facts(agent_id, config.l3_max_facts)

            if saved_facts:
                logger.info("Extracted %d facts for agent %s",
                            len(saved_facts), agent_id)
            return saved_facts

        except (json.JSONDecodeError, Exception) as e:
            logger.debug("Fact extraction failed: %s", e)
            return []

    # ── Session-level action buffer (replaces per-tool logging) ──
    # Actions are buffered during a chat session and aggregated into a single
    # structured "outcome" memory at session end, instead of writing one L3
    # entry per tool call.
    _action_buffer: dict[str, list[dict]] = {}  # agent_id → [{tool, summary, ts}]

    def buffer_agent_action(self, agent_id: str, tool_name: str,
                             summary: str, details: str = "") -> None:
        """Buffer a tool action for later session-level aggregation.

        Unlike the old record_agent_action, this does NOT write to L3 immediately.
        Call flush_action_buffer() at session end to generate a single structured
        outcome memory from all buffered actions.
        """
        _SKIP_TOOLS = {
            "list_agents", "list_tools", "help", "get_status",
            "knowledge_lookup", "get_time", "search_web",
            "read_file", "search_files", "glob_files", "web_search",
            "web_fetch", "get_skill_guide",
        }
        if tool_name in _SKIP_TOOLS:
            return
        # Skip error results
        if summary and summary.strip().startswith(("DENIED:", "Error:", "error:", "Failed")):
            return

        buf = self._action_buffer.setdefault(agent_id, [])
        buf.append({
            "tool": tool_name,
            "summary": summary[:200],
            "details": details[:150] if details else "",
            "ts": time.strftime("%H:%M"),
        })
        # Cap buffer to prevent unbounded growth
        if len(buf) > 50:
            buf[:] = buf[-30:]

    def flush_action_buffer(self, agent_id: str,
                             llm_call: Any = None) -> Optional[SemanticFact]:
        """Aggregate buffered actions into a single 'outcome' memory.

        Called at session end (after the final assistant response).
        If there are fewer than 2 actions, skip (not enough substance).
        """
        buf = self._action_buffer.pop(agent_id, [])
        if len(buf) < 2:
            return None

        # Group by tool for concise summary
        tool_groups: dict[str, list[str]] = {}
        for a in buf:
            tool_groups.setdefault(a["tool"], []).append(a["summary"])

        actions_text = ""
        for tool, summaries in tool_groups.items():
            actions_text += f"- {tool}: {len(summaries)}次 — {summaries[0]}"
            if len(summaries) > 1:
                actions_text += f" ... {summaries[-1]}"
            actions_text += "\n"

        if llm_call:
            try:
                agg_prompt = (
                    "以下是 Agent 在一次会话中的操作列表。"
                    "请用一句话总结最终结果和状态变化（不要列举每个操作）:\n\n"
                    f"{actions_text}\n"
                    "格式: [日期] 最终结果，状态变化，失败原因（如有）"
                )
                result_text = llm_call(agg_prompt).strip()
            except Exception:
                result_text = None
        else:
            result_text = None

        if not result_text:
            # Simple aggregation without LLM
            tools_used = list(tool_groups.keys())
            result_text = (
                f"[{time.strftime('%Y-%m-%d')}] "
                f"本次会话执行了 {len(buf)} 个操作 "
                f"(工具: {', '.join(tools_used[:5])})"
            )

        fact = SemanticFact(
            agent_id=agent_id,
            category="outcome",
            content=result_text,
            source=f"session_aggregate:{time.strftime('%Y-%m-%d %H:%M')}",
            confidence=0.7,
        )
        self.save_fact(fact)
        logger.info("Flushed action buffer for %s: %d actions → 1 outcome",
                     agent_id, len(buf))
        return fact

    def record_agent_action(self, agent_id: str, action_type: str,
                            tool_name: str, summary: str,
                            details: str = "", confidence: float = 0.9):
        """Buffer agent action for session-level aggregation.

        CHANGED in v2: No longer writes directly to L3. Instead buffers
        the action and aggregates at session end via flush_action_buffer().

        Kept for backward compatibility — callers don't need to change.
          - MCP 调用后
          - 重要的状态变更

        Args:
            action_type: "tool_exec" | "file_change" | "mcp_call" | "config_change"
            tool_name: 工具名称
            summary: 一句话描述做了什么
            details: 可选的详细信息 (文件名、参数等)
        """
        # Delegate to buffer instead of direct L3 write
        self.buffer_agent_action(agent_id, tool_name, summary, details)

    def _worth_extracting(self, user_msg: str, assistant_resp: str) -> bool:
        """启发式判断这轮对话是否值得提取记忆。

        核心原则: 只在对话包含「对未来有用的信息」时才提取。
        不提取: 简单问答、闲聊、纯执行过程、错误重试。

        触发条件 (五层记忆模型):
          - 意图: 用户明确表达了目标、需求、约束
          - 决策: 做出了选型、方案选择、权衡取舍
          - 结果: 一个阶段性任务完成或失败（不是每个命令）
          - 规则: 总结出了可复用的经验/规则/约定
          - 反思: 发现了低效的地方、可改进的流程
        """
        # 用户消息太短（闲聊/确认），通常不值得
        if len(user_msg.strip()) < 8:
            return False

        # 助手回复太短（简单回答），通常不值得
        if len(assistant_resp.strip()) < 100:
            return False

        combined = (user_msg + " " + assistant_resp).lower()

        # ── 意图类: 用户在表达目标/需求 ──
        intent_kw = [
            "目标", "需求", "希望", "想要", "计划", "里程碑", "阶段",
            "要求", "约束", "截止", "优先级",
            "goal", "want", "need", "plan", "milestone", "requirement",
            "deadline", "priority", "objective",
        ]
        # ── 决策类: 做出了选择/权衡 ──
        reasoning_kw = [
            "决定", "选择", "因为", "所以", "权衡", "对比", "排除",
            "方案", "选型", "采用", "不用", "而不是", "原因是",
            "之所以", "考虑到", "假设",
            "decide", "choose", "because", "trade-off", "instead of",
            "reason", "assume", "approach", "versus",
        ]
        # ── 结果类: 阶段性成果（不是每条命令的结果）──
        outcome_kw = [
            "完成了", "搞定了", "上线", "部署成功", "发布",
            "失败", "失败原因", "无法", "不支持",
            "最终", "结论", "结果是",
            "completed", "finished", "deployed", "released",
            "failed because", "conclusion", "result is",
        ]
        # ── 规则类: 可复用的经验 ──
        rule_kw = [
            "记住", "以后", "总是", "永远", "必须", "不要",
            "规则", "约定", "规范", "如果.*就", "每次.*都",
            "踩坑", "注意", "关键是",
            "remember", "always", "never", "must", "convention",
            "rule", "gotcha", "caveat", "trick is",
        ]
        # ── 反思类: 改进/优化/低效 ──
        reflection_kw = [
            "下次", "改进", "优化", "低效", "应该先",
            "可以合并", "多余", "浪费", "更好的方式",
            "next time", "improve", "optimize", "inefficient",
            "should have", "better way", "unnecessary",
        ]

        all_groups = [intent_kw, reasoning_kw, outcome_kw, rule_kw, reflection_kw]
        hits = sum(1 for group in all_groups
                   if any(kw in combined for kw in group))

        # 需要至少命中 1 组关键词才提取
        # 但如果助手回复很长（>1500字，说明是实质性工作），降低阈值
        if len(assistant_resp.strip()) > 1500:
            return hits >= 1
        return hits >= 1

    @staticmethod
    def _looks_like_compressed_placeholder(text: str) -> bool:
        """识别 L2 auto-compact 产物（如"对话内容已被压缩"）。

        这类占位符的特征是**自我描述**："对话已被压缩 / 摘要标记 / 无法分析"，
        以及明显短于一条真实对话。L3 不该在上面做事实提取。
        """
        if not text:
            return False
        markers = (
            "对话内容已被压缩", "上下文压缩", "仅显示摘要标记",
            "未包含实际讨论内容", "无法提取具体讨论",
            "内容不完整,无法分析", "context compressed",
            "context truncated",
        )
        low = text[:500]
        return any(m in low for m in markers)

    def _is_similar(self, a: str, b: str) -> bool:
        """语义去重判断 — 中英文混合。

        之前版本用 str.split() 按空格分词，对**没有空格的中文**完全失效：
        整句会被当成一个 token，两句 99% 相同但因一个标点/数字差异就判不相似，
        导致 L3 被同类事实反复灌入（#ISSUE_L3_DEDUP_CN）。

        新版：
          • 英文: word token（长度≥3 过滤噪音）
          • 中文: 按 bigram 切（和 _tokenize_query 一致）
          • 合并两个 token 集合后算 Jaccard
          • 最少 4 个 bigram 时才比较，避免短句误判
        """
        tokens_a = self._content_tokens(a)
        tokens_b = self._content_tokens(b)
        if len(tokens_a) < 4 or len(tokens_b) < 4:
            # 太短时退化到字符串宽松匹配
            return (a.strip() == b.strip()) or (a in b) or (b in a)
        overlap = len(tokens_a & tokens_b)
        union = len(tokens_a | tokens_b)
        # Jaccard 相似度 — 比旧的"min 集合占比"更严格、更对称
        return overlap / union >= 0.5

    @staticmethod
    def _content_tokens(s: str) -> set[str]:
        """内容 → token 集合（英文词 + 中文 bigram）。"""
        import re
        tokens: set[str] = set()
        if not s:
            return tokens
        s_low = s.lower()
        # 英文词（去掉 1-2 字母噪音）
        for w in re.findall(r"[a-z][a-z0-9_]{2,}", s_low):
            tokens.add(w)
        # 中文 bigram
        for seg in re.findall(r"[\u4e00-\u9fff]+", s):
            if len(seg) == 1:
                tokens.add(seg)
            else:
                for i in range(len(seg) - 1):
                    tokens.add(seg[i:i+2])
        # 数字连续段（时间、端口等，对 rule/intent 去重有用）
        for n in re.findall(r"\d{2,}", s):
            tokens.add(n)
        return tokens

    # ==================================================================
    # User Feedback → L3 Fact Learning
    # ==================================================================

    # Patterns that indicate user is correcting / expressing preference
    _FEEDBACK_CORRECTION_KW = [
        "不对", "不是这样", "错了", "我要的是", "我说的是", "不是",
        "你搞错了", "应该是", "请改成", "换成", "不要用", "别用",
        "wrong", "not what i", "i meant", "i want", "don't use",
        "should be", "instead use", "please change", "fix this",
    ]
    _FEEDBACK_PREFERENCE_KW = [
        "用中文", "用英文", "说中文", "说英文", "简洁", "详细",
        "代码风格", "偏好", "习惯", "我喜欢", "我不喜欢",
        "in chinese", "in english", "be concise", "more detail",
        "i prefer", "i like", "i don't like", "my style",
    ]

    def detect_and_learn_feedback(
        self,
        agent_id: str,
        user_message: str,
        assistant_response: str,
        prev_assistant: str = "",
        llm_call: Any = None,
    ) -> list[SemanticFact]:
        """Detect implicit user feedback signals and extract preference facts.

        Feedback signals:
        - Explicit correction: "不对", "我要的是...", "wrong", "should be..."
        - Preference expression: "用中文回复", "be more concise"
        - Repeated question: user asks same topic again (implies previous answer was poor)

        Args:
            user_message: Current user message
            assistant_response: Current assistant response
            prev_assistant: Previous assistant response (for context)
            llm_call: LLM function for extraction

        Returns:
            List of saved SemanticFact (category=rule, high confidence)
        """
        user_lower = user_message.lower()

        # Detect correction signal
        is_correction = any(kw in user_lower for kw in self._FEEDBACK_CORRECTION_KW)
        # Detect preference signal
        is_preference = any(kw in user_lower for kw in self._FEEDBACK_PREFERENCE_KW)

        if not is_correction and not is_preference:
            return []

        if not llm_call:
            # Without LLM, do simple rule-based extraction
            return self._extract_feedback_simple(
                agent_id, user_message, is_correction, is_preference)

        # LLM-based extraction for richer feedback
        signal_type = "correction" if is_correction else "preference"
        prompt = (
            "你是 Agent 的用户反馈学习器。从用户的反馈中提取**可复用的偏好规则**。\n\n"
            "核心原则: 提取「以后遇到类似情况应该怎么做」的规则，不是记录事件本身。\n\n"
            f"反馈类型: {signal_type}\n"
            f"用户消息: {user_message[:500]}\n"
            f"助手回复: {assistant_response[:500]}\n"
            + (f"之前的回复: {prev_assistant[:300]}\n" if prev_assistant else "")
            + "\n"
            "提取 1-2 条规则，返回 JSON 数组:\n"
            '[{"content": "自包含的偏好规则", "category": "rule", "confidence": 0.85}]\n\n'
            "示例:\n"
            '用户: "用中文回复我" → [{"content": "用户偏好中文回复，所有输出应使用中文", '
            '"category": "rule", "confidence": 0.9}]\n'
            '用户: "不对，应该用FastAPI不是Flask" → [{"content": "该项目使用FastAPI框架，不使用Flask", '
            '"category": "rule", "confidence": 0.9}]\n\n'
            "没有有用信息就返回 []"
        )

        try:
            result = llm_call(prompt).strip()
            if result.startswith("```"):
                lines = result.split("\n")
                result = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:]).strip()

            facts_data = json.loads(result)
            if not isinstance(facts_data, list):
                return []

            saved = []
            for fd in facts_data:
                if not isinstance(fd, dict) or not fd.get("content"):
                    continue
                # Dedup against existing facts
                if self._check_chromadb_available():
                    existing = self.search_facts_vector(agent_id, fd["content"], top_k=1)
                else:
                    existing = self.search_facts(agent_id, fd["content"], top_k=1)
                if existing and self._is_similar(existing[0].content, fd["content"]):
                    existing[0].content = fd["content"]
                    existing[0].confidence = max(existing[0].confidence, fd.get("confidence", 0.85))
                    existing[0].updated_at = time.time()
                    self.save_fact(existing[0])
                    saved.append(existing[0])
                    continue

                fact = SemanticFact(
                    agent_id=agent_id,
                    category="rule",
                    content=fd["content"],
                    source=f"user_feedback:{time.strftime('%Y-%m-%d %H:%M')}",
                    confidence=fd.get("confidence", 0.85),
                )
                self.save_fact(fact)
                saved.append(fact)

            if saved:
                logger.info("Learned %d feedback facts for agent %s", len(saved), agent_id)
            return saved
        except Exception as e:
            logger.debug("Feedback learning LLM failed: %s, using simple extraction", e)
            return self._extract_feedback_simple(
                agent_id, user_message, is_correction, is_preference)

    def _extract_feedback_simple(
        self, agent_id: str, user_message: str,
        is_correction: bool, is_preference: bool,
    ) -> list[SemanticFact]:
        """Simple rule-based feedback extraction (no LLM needed)."""
        facts = []
        msg = user_message.strip()

        # Only extract if the message is specific enough
        if len(msg) < 6:
            return []

        if is_preference:
            # Direct preference — store as-is with high confidence
            fact = SemanticFact(
                agent_id=agent_id,
                category="rule",
                content=f"用户偏好: {msg[:200]}",
                source=f"user_feedback:{time.strftime('%Y-%m-%d %H:%M')}",
                confidence=0.8,
            )
            self.save_fact(fact)
            facts.append(fact)

        if is_correction and len(msg) > 15:
            # Correction with enough detail to be useful
            fact = SemanticFact(
                agent_id=agent_id,
                category="rule",
                content=f"用户纠正: {msg[:200]}",
                source=f"user_feedback:{time.strftime('%Y-%m-%d %H:%M')}",
                confidence=0.85,
            )
            self.save_fact(fact)
            facts.append(fact)

        return facts

    def _prune_facts(self, agent_id: str, max_facts: int):
        """超上限时的分级淘汰 — 保护 intent/rule/reflection，优先删流水账。

        旧版按 ``(confidence ASC, updated_at ASC)`` 排序删最"低分最老"的。
        问题：流水账日志常被 LLM 标成 0.9 confidence，与真正的规则/意图
        同档甚至更高；结果该删的删不动，意图反而可能被顶掉。

        新版分两档清：
          档 A — 高价值，只在档 B 清空后才动：
              intent / rule / reflection / reasoning
          档 B — 过程记录，先清：
              outcome / general / 其它
          档 B 内优先删"流水账指纹"命中的：
              含时间戳（YYYY-MM-DD / HH:MM）
              含 URL / 文件路径
              开头是 "已完成" / "已成功" / "任务状态" / "已创建"
        """
        count = self.count_facts(agent_id)
        if count <= max_facts:
            return
        to_delete_count = count - max_facts

        HIGH_VALUE = ("intent", "rule", "reflection", "reasoning")

        # 按"可删优先级"排序：log_score DESC（流水账指纹越多越先删），
        # category tier DESC（档 B 先删），confidence ASC，updated_at ASC。
        with self._rlock:
            rows = self._conn.execute("""
                SELECT id, category, content, confidence, updated_at
                FROM memory_semantic
                WHERE agent_id = ?
            """, (agent_id,)).fetchall()

        scored = []
        for r in rows:
            cat = (r["category"] or "").lower()
            content = r["content"] or ""
            tier_b = 0 if cat in HIGH_VALUE else 1      # 1 = 先删档 B
            log_score = self._log_fingerprint_score(content)
            scored.append((
                r["id"],
                tier_b,              # 1 先 / 0 后
                log_score,           # 高分先
                -float(r["confidence"] or 0),  # 负号让低 confidence 先
                r["updated_at"] or 0,          # 小（旧）先
            ))
        # 删除顺序：tier_b DESC, log_score DESC, -confidence DESC, updated_at ASC
        scored.sort(key=lambda t: (-t[1], -t[2], -t[3], t[4]))
        to_delete_ids = [row[0] for row in scored[:to_delete_count]]

        if not to_delete_ids:
            return
        placeholders = ",".join("?" for _ in to_delete_ids)
        with self._rlock:
            self._conn.execute(
                f"DELETE FROM memory_semantic WHERE id IN ({placeholders})",
                to_delete_ids,
            )
            self._conn.commit()
        logger.info(
            "L3 prune: agent=%s deleted=%d (kept=%d, cap=%d)",
            agent_id, len(to_delete_ids), count - len(to_delete_ids), max_facts,
        )

    @staticmethod
    def _log_fingerprint_score(content: str) -> int:
        """流水账指纹打分 — 分数越高越像"一次性过程记录"（更该删）。"""
        import re
        if not content:
            return 0
        score = 0
        # 时间戳 (2026-04-17 / 09:00)
        if re.search(r"\d{4}-\d{2}-\d{2}", content):
            score += 2
        if re.search(r"\b\d{1,2}:\d{2}\b", content):
            score += 1
        # URL
        if re.search(r"https?://", content):
            score += 2
        # 文件路径（含后缀）
        if re.search(r"[\w/.-]+\.(py|js|md|yaml|yml|json|sh|txt|csv)\b", content):
            score += 1
        # "已完成/已成功/已创建/任务状态"类流水开头
        if re.match(r"^\s*(已完成|已成功|已创建|已部署|已设置|任务状态|首次执行)",
                    content):
            score += 2
        return score

    # ==================================================================
    # Utility
    # ==================================================================

    def _tokenize_query(self, query: str) -> list[str]:
        """
        将查询分词用于 FTS5。
        对中文按字分割（bigram），对英文按空格分词。
        """
        import re
        tokens = []
        # 提取英文单词
        eng_words = re.findall(r'[a-zA-Z_]\w{2,}', query)
        tokens.extend(eng_words)
        # 提取中文（bigram）
        cjk_chars = re.findall(r'[\u4e00-\u9fff]+', query)
        for segment in cjk_chars:
            if len(segment) >= 2:
                for i in range(len(segment) - 1):
                    tokens.append(segment[i:i+2])
            else:
                tokens.append(segment)
        # 去重、过滤太短的
        seen = set()
        result = []
        for t in tokens:
            tl = t.lower()
            if tl not in seen and len(tl) >= 2:
                # FTS5 需要用双引号包裹含特殊字符的 token
                result.append(f'"{tl}"')
                seen.add(tl)
        return result

    def _format_messages_for_summary(self, messages: list[dict]) -> str:
        """将消息列表格式化为文本（用于 LLM 摘要）。"""
        parts = []
        for m in messages:
            role = m.get("role", "unknown")
            content = m.get("content", "")
            if not isinstance(content, str) or not content.strip():
                continue
            if role == "system":
                continue  # 跳过 system 消息
            if role == "tool":
                text = content.strip()[:200]
                parts.append(f"[工具结果] {text}")
            elif role == "user":
                text = content.strip()[:300]
                parts.append(f"用户: {text}")
            elif role == "assistant":
                text = content.strip()[:300]
                parts.append(f"助手: {text}")
        return "\n".join(parts)

    def _count_turns(self, messages: list[dict]) -> int:
        """统计消息中的轮次数。"""
        return sum(1 for m in messages if m.get("role") == "user")

    # ------------------------------------------------------------------
    # 统计与调试
    # ------------------------------------------------------------------

    def get_stats(self, agent_id: str) -> dict:
        """获取 agent 的记忆统计。"""
        return {
            "episodic_count": self.count_episodic(agent_id),
            "fact_count": self.count_facts(agent_id),
            "config": self.get_config(agent_id).to_dict(),
        }

    def clear_all(self, agent_id: str):
        """清空 agent 的所有记忆。"""
        self.clear_episodic(agent_id)
        self.clear_facts(agent_id)
        logger.info("Cleared all memory for agent %s", agent_id)


# ---------------------------------------------------------------------------
# Global singleton
# ---------------------------------------------------------------------------
_memory_manager: Optional[MemoryManager] = None
# =====================================================================
# MemoryConsolidator — 记忆整理器
# =====================================================================

class MemoryConsolidator:
    """
    定期整理 L3 记忆，保持记忆系统精炼有效。

    三大能力:
    1. **Intent→Outcome 归并**: intent 记忆匹配到 outcome 时，标记意图为已达成
    2. **相似记忆合并**: 同 category 中语义高度重复的条目合并为一条
    3. **过期记忆衰减**: 长时间未更新且低 confidence 的记忆降权或删除

    触发时机:
    - 每次对话 write-back 后（轻量级，只处理最近变动）
    - Workflow step 完成后（归并 intent→outcome）
    - 可选的定时周期整理（全量扫描）
    """

    # 归并匹配阈值
    _PLAN_DONE_SIMILARITY = 0.55   # plan vs done 文本相似度阈值
    _MERGE_SIMILARITY = 0.65       # 同 category 合并阈值
    _DECAY_DAYS = 30               # 超过 N 天未更新的低置信度记忆开始衰减
    _DECAY_RATE = 0.1              # 每次整理衰减的 confidence 幅度
    _MIN_CONFIDENCE = 0.15         # 低于此值的记忆直接删除
    _MAX_MERGE_PER_RUN = 10        # 单次最多合并条数（防止卡顿）

    def __init__(self, memory_manager: MemoryManager):
        self._mm = memory_manager
        self._last_consolidate: dict[str, float] = {}  # agent_id → timestamp
        self._consolidate_interval = 300  # 最少间隔 5 分钟

    # ── 主入口 ──

    def consolidate(self, agent_id: str,
                    llm_call: Any = None,
                    force: bool = False) -> dict:
        """
        执行一轮记忆整理。返回整理报告。

        Args:
            agent_id: 目标 agent
            llm_call: 可选 LLM 函数，用于智能合并摘要（无则用简单拼接）
            force: 跳过间隔限制，强制执行
        """
        now = time.time()
        last = self._last_consolidate.get(agent_id, 0)
        if not force and (now - last) < self._consolidate_interval:
            return {"skipped": True, "reason": "interval_not_reached"}

        report = {
            "agent_id": agent_id,
            "timestamp": now,
            "plans_resolved": 0,
            "facts_merged": 0,
            "facts_decayed": 0,
            "facts_deleted": 0,
        }

        try:
            # 1. Intent→Outcome 归并
            report["plans_resolved"] = self._resolve_plans(agent_id)

            # 2. 相似记忆合并
            report["facts_merged"] = self._merge_similar(agent_id, llm_call)

            # 3. 过期记忆衰减
            decayed, deleted = self._decay_stale(agent_id)
            report["facts_decayed"] = decayed
            report["facts_deleted"] = deleted

        except Exception as e:
            logger.warning("MemoryConsolidator error for %s: %s", agent_id, e)
            report["error"] = str(e)

        self._last_consolidate[agent_id] = now

        total_actions = (report["plans_resolved"] + report["facts_merged"]
                         + report["facts_decayed"] + report["facts_deleted"])
        if total_actions > 0:
            logger.info(
                "Memory consolidated for %s: %d plans resolved, "
                "%d merged, %d decayed, %d deleted",
                agent_id, report["plans_resolved"],
                report["facts_merged"], report["facts_decayed"],
                report["facts_deleted"],
            )
        return report

    # ── 1. Plan→Done 归并 ──

    def _resolve_plans(self, agent_id: str) -> int:
        """
        将已达成的 intent 标记为 outcome。

        逻辑: 遍历所有 intent 记忆，如果在 outcome 中找到语义匹配的记录，
        说明该意图已达成 → 将 intent 更新为 outcome 并标注完成时间。

        也兼容旧分类: action_plan → action_done。
        """
        # New categories
        plans = self._mm.get_recent_facts(agent_id, limit=50, category="intent")
        # Also check legacy categories
        plans += self._mm.get_recent_facts(agent_id, limit=50, category="action_plan")
        if not plans:
            return 0

        dones = self._mm.get_recent_facts(agent_id, limit=100, category="outcome")
        dones += self._mm.get_recent_facts(agent_id, limit=100, category="action_done")
        done_texts = [d.content.lower() for d in dones]

        resolved = 0
        for plan in plans:
            # 幂等检查: 已标记 [已完成] 的跳过
            if "[已完成]" in plan.content:
                continue
            plan_text = plan.content.lower()
            # 检查是否有匹配的 done 记录
            matched = False
            for done_text in done_texts:
                if self._text_similarity(plan_text, done_text) > self._PLAN_DONE_SIMILARITY:
                    matched = True
                    break

            # 也检查向量相似度（如果可用）
            if not matched and self._mm._check_chromadb_available():
                try:
                    # Search in both new and legacy outcome categories
                    for _cat in ("outcome", "action_done"):
                        vec_results = self._mm.search_facts_vector(
                            agent_id, plan.content, top_k=3, category=_cat)
                        for vr in vec_results:
                            if self._text_similarity(
                                    plan_text, vr.content.lower()) > self._PLAN_DONE_SIMILARITY:
                                matched = True
                                break
                        if matched:
                            break
                except Exception:
                    pass

            if matched:
                # 将 intent 转为 outcome（使用新对象，避免原地变更风险）
                timestamp = time.strftime("%Y-%m-%d")
                resolved_fact = SemanticFact(
                    id=plan.id,
                    agent_id=plan.agent_id,
                    category="outcome",
                    content=f"[{timestamp}] [已达成] {plan.content}",
                    source=f"consolidated:{timestamp}",
                    confidence=plan.confidence,
                    created_at=plan.created_at,
                    updated_at=time.time(),
                )
                self._mm.save_fact(resolved_fact)
                resolved += 1

        return resolved

    # ── 2. 相似记忆合并 ──

    def _merge_similar(self, agent_id: str, llm_call: Any = None) -> int:
        """
        在同一 category 内，合并语义高度相似的记忆条目。

        合并策略:
        - 保留更新时间最近的条目
        - 内容合并（LLM 摘要 or 简单拼接去重）
        - 删除被合并的旧条目
        """
        merged_total = 0
        # New 5-layer categories + legacy for backward compat
        categories = [
            "intent", "reasoning", "outcome", "rule", "reflection",
            # legacy (still in DB)
            "action_done", "decision", "context",
            "learned", "issue", "user_pref", "goal",
        ]

        for cat in categories:
            if merged_total >= self._MAX_MERGE_PER_RUN:
                break
            facts = self._mm.get_recent_facts(
                agent_id, limit=50, category=cat)
            if len(facts) < 2:
                continue

            # 构建合并组: 贪心匹配
            used = set()
            merge_groups: list[list[SemanticFact]] = []

            for i, fa in enumerate(facts):
                if fa.id in used:
                    continue
                group = [fa]
                for j in range(i + 1, len(facts)):
                    fb = facts[j]
                    if fb.id in used:
                        continue
                    if self._text_similarity(
                            fa.content.lower(), fb.content.lower()
                    ) > self._MERGE_SIMILARITY:
                        group.append(fb)
                        used.add(fb.id)
                if len(group) > 1:
                    used.add(fa.id)
                    merge_groups.append(group)

            # 执行合并
            for group in merge_groups:
                if merged_total >= self._MAX_MERGE_PER_RUN:
                    break
                self._do_merge(group, llm_call)
                merged_total += len(group) - 1  # 合并掉的条目数

        return merged_total

    def _do_merge(self, group: list[SemanticFact], llm_call: Any = None):
        """合并一组相似的记忆条目。"""
        # 按 updated_at 降序，保留最新的
        group.sort(key=lambda f: f.updated_at, reverse=True)
        keeper = group[0]
        others = group[1:]
        original_content = keeper.content  # 保底用

        if llm_call:
            # 用 LLM 生成合并摘要
            try:
                contents = "\n".join(
                    f"- {f.content}" for f in group)
                prompt = (
                    "你是一个记忆整理助手。以下是几条语义相似的记忆条目，"
                    "请合并为一条精炼的记录，保留所有关键信息，去除重复。\n\n"
                    f"条目:\n{contents}\n\n"
                    "输出合并后的一条记录（纯文本，不需要JSON）:"
                )
                result = llm_call(prompt)
                if result and result.strip():
                    keeper.content = result.strip()
                else:
                    self._simple_merge_content(keeper, others)
            except Exception:
                # LLM 失败，用简单合并
                self._simple_merge_content(keeper, others)
        else:
            self._simple_merge_content(keeper, others)

        # 安全保底：如果合并后内容为空，恢复原始内容
        if not keeper.content.strip():
            keeper.content = original_content

        # 更新 keeper
        keeper.confidence = max(f.confidence for f in group)
        keeper.updated_at = time.time()
        keeper.source = f"merged:{time.strftime('%Y-%m-%d')}"
        self._mm.save_fact(keeper)

        # 删除被合并的条目（delete_fact 已统一处理 SQLite + ChromaDB）
        for other in others:
            try:
                self._mm.delete_fact(other.id)
            except Exception:
                pass

    def _simple_merge_content(self, keeper: SemanticFact,
                              others: list[SemanticFact]):
        """无 LLM 时的简单合并: 保留 keeper 内容 + 从 others 补充新信息。"""
        keeper_words = set(keeper.content.lower().split())
        additions = []
        remaining_budget = 500 - len(keeper.content)  # 可用的补充空间
        for other in others:
            other_words = set(other.content.lower().split())
            new_words = other_words - keeper_words
            # 如果有超过 30% 的新词，值得补充
            if len(new_words) > len(other_words) * 0.3:
                addition = other.content
                # 检查空间是否还够
                if remaining_budget > len(addition) + 2:
                    additions.append(addition)
                    remaining_budget -= len(addition) + 2
                    keeper_words.update(other_words)  # 避免后续重复补充
        if additions:
            keeper.content = keeper.content.rstrip('.。') + "；" + "；".join(additions)

    # ── 3. 过期记忆衰减 ──

    def _decay_stale(self, agent_id: str) -> tuple[int, int]:
        """
        对长期未更新的低置信度记忆进行衰减。

        Returns:
            (decayed_count, deleted_count)
        """
        now = time.time()
        threshold_ts = now - (self._DECAY_DAYS * 86400)

        # 获取所有事实（按 updated_at 升序，最旧的先处理）
        all_facts = self._mm.get_recent_facts(agent_id, limit=200)
        # 反转为升序（最旧的在前）
        all_facts.reverse()

        decayed = 0
        deleted = 0

        for fact in all_facts:
            if fact.updated_at > threshold_ts:
                continue  # 还没过期
            if fact.confidence >= 0.9:
                continue  # 高置信度的不衰减（用户明确说的）
            # 不衰减 rule 和 reflection（这些是长期稳定的可复用知识）
            if fact.category in ("rule", "reflection", "user_pref"):
                continue

            new_confidence = fact.confidence - self._DECAY_RATE

            if new_confidence < self._MIN_CONFIDENCE:
                # 直接删除（delete_fact 统一处理 SQLite + ChromaDB）
                try:
                    self._mm.delete_fact(fact.id)
                    deleted += 1
                except Exception:
                    pass
            else:
                # 降低 confidence（保留原始时间戳，否则衰减会刷新 updated_at 导致永不再衰减）
                fact.confidence = new_confidence
                self._mm.save_fact(fact, preserve_timestamps=True)
                decayed += 1

        return decayed, deleted

    # ── 工具方法 ──

    @staticmethod
    def _text_similarity(a: str, b: str) -> float:
        """
        计算两段文本的相似度（0~1）。
        结合词袋重叠 + 关键词匹配。
        """
        words_a = set(a.split())
        words_b = set(b.split())
        if not words_a or not words_b:
            return 0.0

        overlap = len(words_a & words_b)
        # Jaccard 和 Overlap coefficient 的混合
        jaccard = overlap / len(words_a | words_b) if words_a | words_b else 0
        overlap_coeff = overlap / min(len(words_a), len(words_b))

        # 加权: overlap coefficient 更重要（短文本 vs 长文本时更鲁棒）
        return jaccard * 0.3 + overlap_coeff * 0.7


# =====================================================================
# Singleton
# =====================================================================

_mm_lock = threading.Lock()


def get_memory_manager(db_path: str = "") -> MemoryManager:
    """获取全局 MemoryManager 单例。"""
    global _memory_manager
    if _memory_manager is None:
        with _mm_lock:
            if _memory_manager is None:
                _memory_manager = MemoryManager(db_path=db_path)
    return _memory_manager


def init_memory_manager(db_path: str = "") -> MemoryManager:
    """显式初始化 MemoryManager（可指定数据库路径）。"""
    global _memory_manager
    with _mm_lock:
        _memory_manager = MemoryManager(db_path=db_path)
    return _memory_manager
