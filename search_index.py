#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""search_index.py — P0b 混合检索层（BM25 全文 + 向量接口 + RRF 融合）。

抄：tantivy/meilisearch 的全文索引思路 + BGE 向量接口 + Reciprocal Rank Fusion。
设计：
  - SQLite FTS5 做 BM25 全文索引（零依赖，Python 内置）
  - 向量检索留接口（embed_fn 可注入 BGE/onnxruntime，未配置时自动降级纯 BM25）
  - RRF 融合两路结果（k=60，业界默认）
  - 供 cb_index / kb_query / lesson 检索复用

用法：
  idx = SearchIndex(path_or_dir)
  idx.add_document(id, text, meta)
  idx.search("查询词")  -> [{id, score, meta}]
  idx.search_hybrid("查询", embed_fn=...)  -> RRF 融合
"""
import json
import os
import sqlite3
import threading

_RRF_K = 60  # RRF 常数（业界默认）


class SearchIndex:
    """SQLite FTS5 全文索引 + 可选向量 + RRF 融合。

    内部：FTS5 虚拟表 + doc_map 映射表（doc_id -> rowid）。
    FTS5 的删除按内容匹配，故用官方 'delete' 命令（需完整原内容）。
    """

    def __init__(self, db_path: str, table: str = "docs"):
        self._lock = threading.Lock()
        self._db_path = str(db_path)
        self._table = table
        self._map_table = f"{table}_map"
        with self._lock, sqlite3.connect(self._db_path) as conn:
            conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5("
                "title, content, meta)"
            )
            conn.execute(
                f"CREATE TABLE IF NOT EXISTS {self._map_table}("
                "doc_id TEXT PRIMARY KEY, rowid INTEGER)"
            )

    # ── 写入 ──────────────────────────────────────────────
    def add_document(self, doc_id: str, content: str, title: str = "",
                     meta: dict | None = None) -> None:
        """新增/替换文档。content 为检索文本，meta 为任意元数据(JSON)。"""
        meta_json = json.dumps(meta or {}, ensure_ascii=False)
        with self._lock, sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                f"SELECT rowid FROM {self._map_table} WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
            if cur is not None:
                # 普通 DELETE by rowid（FTS5 官方支持；'delete' 命令内容匹配不可靠）
                conn.execute(f"DELETE FROM {self._table} WHERE rowid = ?", (cur[0],))
                conn.execute(f"DELETE FROM {self._map_table} WHERE doc_id = ?", (doc_id,))
            cur = conn.execute(
                f"INSERT INTO {self._table}(title, content, meta) VALUES (?,?,?)",
                (title, content, meta_json),
            )
            new_rowid = cur.lastrowid
            conn.execute(
                f"INSERT INTO {self._map_table}(doc_id, rowid) VALUES (?,?)",
                (doc_id, new_rowid),
            )

    def add_many(self, docs: list[dict]) -> None:
        """批量添加：docs = [{id, content, title?, meta?}, ...]"""
        for d in docs:
            self.add_document(d["id"], d["content"], d.get("title", ""), d.get("meta"))

    def delete(self, doc_id: str) -> None:
        with self._lock, sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                f"SELECT rowid FROM {self._map_table} WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
            if cur is None:
                return
            conn.execute(f"DELETE FROM {self._table} WHERE rowid = ?", (cur[0],))
            conn.execute(f"DELETE FROM {self._map_table} WHERE doc_id = ?", (doc_id,))

    # ── 检索 ──────────────────────────────────────────────
    def search(self, query: str, limit: int = 20) -> list[dict]:
        """BM25 全文检索（FTS5 默认 bm25 排序）。"""
        if not query.strip():
            return []
        with self._lock, sqlite3.connect(self._db_path) as conn:
            try:
                rows = conn.execute(
                    f"SELECT t.rowid, t.title, t.content, t.meta, "
                    f"bm25({self._table}) AS score "
                    f"FROM {self._table} t WHERE {self._table} MATCH ? "
                    f"ORDER BY score LIMIT ?",
                    (self._query_safe(query), limit),
                ).fetchall()
            except sqlite3.OperationalError:
                return []  # 语法错误（特殊字符）降级空结果
        ids = {r[0]: 1 for r in rows}
        out = []
        if rows:
            with self._lock, sqlite3.connect(self._db_path) as conn:
                for rowid, title, content, meta, score in rows:
                    m = conn.execute(
                        f"SELECT doc_id FROM {self._map_table} WHERE rowid = ?",
                        (rowid,),
                    ).fetchone()
                    out.append({
                        "id": m[0] if m else str(rowid),
                        "title": title, "content": content,
                        "meta": json.loads(meta) if meta else {},
                        "bm25_score": float(score),
                    })
        return out

    def search_hybrid(self, query: str, embed_fn=None, limit: int = 20) -> list[dict]:
        """混合检索：BM25 + 向量（若有 embed_fn），RRF 融合。

        embed_fn(text) -> list[float] 或 None（未配置时纯 BM25）。
        向量检索需子类实现 _vector_search；未实现时自动降级纯 BM25。
        """
        bm25_hits = self.search(query, limit=limit * 2)
        vec_hits = self._vector_search(query, embed_fn, limit=limit * 2) if embed_fn else []
        if not vec_hits:
            return bm25_hits[:limit]
        # RRF 融合（k=60）：排名倒数加权，两路结果取并集
        scores: dict[str, float] = {}
        for rank, hit in enumerate(bm25_hits):
            scores[hit["id"]] = scores.get(hit["id"], 0.0) + 1.0 / (_RRF_K + rank)
        for rank, hit in enumerate(vec_hits):
            scores[hit["id"]] = scores.get(hit["id"], 0.0) + 1.0 / (_RRF_K + rank)
        merged = {h["id"]: h for h in bm25_hits + vec_hits}
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return [merged[i] for i, _ in ranked[:limit]]

    # ── 内部 ──────────────────────────────────────────────
    @staticmethod
    def _query_safe(q: str) -> str:
        """FTS5 查询转义：去掉特殊语法字符，防语法错误/注入。

        FTS5 特殊字符：`-`(排除) `"`(短语) `*`(前缀) `(`/`)`(分组) `OR`/`AND`/`NOT`。
        统一替换为空格（简单可靠，牺牲少量查询语法能力换取零崩溃）。
        """
        out = []
        for t in q.split():
            t = t.replace('"', "").replace("'", "")
            # 连字符是排除语法（rx-core → rx - core 报错），拆成空格
            t = t.replace("-", " ")
            t = t.replace("(", " ").replace(")", " ")
            t = t.replace("*", " ").replace(":", " ")
            for w in t.split():
                if w.upper() in ("OR", "AND", "NOT"):
                    continue  # 逻辑操作符去掉（防注入语义）
                out.append(w)
        return " ".join(out).strip()[:200]

    def _vector_search(self, query, embed_fn, limit):
        """向量检索接口——由子类/外部注入实现（如 onnxruntime BGE）。"""
        return []

    def stats(self) -> dict:
        with self._lock, sqlite3.connect(self._db_path) as conn:
            n = conn.execute(f"SELECT count(*) FROM {self._map_table}").fetchone()[0]
        return {"table": self._table, "docs": n, "db": self._db_path}
