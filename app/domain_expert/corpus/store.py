"""sqlite-vss vector store wrapper.

Schema:
    chunks (id INTEGER PK, text TEXT, metadata_json TEXT, source TEXT, created_at REAL)
    chunks_vss (rowid INTEGER, embedding BLOB)  -- managed by sqlite-vss

API:
    store.insert(chunks: list[Chunk], embeddings: list[list[float]])
    store.query(embedding, top_k=8) -> list[(chunk, score)]
    store.delete_by_source(source: str)
    store.count() -> int
"""
from __future__ import annotations
import json
import sqlite3
import os
from .chunker import Chunk


class VectorStore:
    def __init__(self, db_path: str, embedding_dim: int = 1024):
        self.db_path = db_path
        self.embedding_dim = embedding_dim
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.enable_load_extension(True)
        try:
            import sqlite_vss
            sqlite_vss.load(self.conn)
        except ImportError as e:
            raise RuntimeError(
                "sqlite-vss not installed. pip install sqlite-vss"
            ) from e
        self._init_schema()

    def _init_schema(self):
        cur = self.conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
            )
        """)
        cur.execute(f"""
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vss USING vss0(
                embedding({self.embedding_dim})
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source)")
        self.conn.commit()

    def insert(self, chunks: list[Chunk], embeddings: list[list[float]]) -> int:
        assert len(chunks) == len(embeddings), "chunks/embeddings length mismatch"
        cur = self.conn.cursor()
        inserted = 0
        for ch, emb in zip(chunks, embeddings):
            cur.execute(
                "INSERT INTO chunks (text, metadata_json, source) VALUES (?, ?, ?)",
                (ch.text, json.dumps(ch.metadata, ensure_ascii=False),
                 ch.metadata.get("source", "unknown")),
            )
            row_id = cur.lastrowid
            cur.execute(
                "INSERT INTO chunks_vss (rowid, embedding) VALUES (?, ?)",
                (row_id, json.dumps(emb)),
            )
            inserted += 1
        self.conn.commit()
        return inserted

    def query(self, embedding: list[float], top_k: int = 8) -> list[tuple[Chunk, float]]:
        cur = self.conn.cursor()
        cur.execute(f"""
            SELECT c.id, c.text, c.metadata_json, c.source, vss.distance
            FROM chunks_vss vss
            JOIN chunks c ON c.id = vss.rowid
            WHERE vss_search(vss.embedding, vss_search_params(?, ?))
            ORDER BY vss.distance ASC
        """, (json.dumps(embedding), top_k))
        results = []
        for row in cur.fetchall():
            _id, text, meta_json, source, dist = row
            meta = json.loads(meta_json) if meta_json else {}
            chunk = Chunk(text=text, metadata=meta)
            score = 1.0 / (1.0 + dist)  # smaller distance → higher score
            results.append((chunk, score))
        return results

    def delete_by_source(self, source: str) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT id FROM chunks WHERE source = ?", (source,))
        ids = [r[0] for r in cur.fetchall()]
        if not ids:
            return 0
        placeholders = ",".join(["?"] * len(ids))
        cur.execute(f"DELETE FROM chunks_vss WHERE rowid IN ({placeholders})", ids)
        cur.execute(f"DELETE FROM chunks WHERE id IN ({placeholders})", ids)
        self.conn.commit()
        return len(ids)

    def count(self) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM chunks")
        return cur.fetchone()[0]

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
