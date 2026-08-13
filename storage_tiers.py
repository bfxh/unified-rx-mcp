#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""storage_tiers.py — P2b 热/温/冷三层存储（抄 AetherStudio ai_hot_data 思路）。

设计：
  - 热数据（hot）：最近 N 条在内存 + 追加式 JSONL（mmap 思路的 Python 版：open append + flush）
  - 温数据（warm）：超过阈值自动归档进 SQLite（可查询、可过滤）
  - 冷数据（cold）：超长期不访问的旧数据压缩存储（gzip jsonl）
  - 查询自动合并三层（热优先，温次之，冷最后）

与 scan-log 的关系：scan_log_core 的 JSONL 落盘保持不变（兼容），
storage_tiers 作为升级版存储（daemon 日志/教训/索引元数据通用）。

用法：
  st = TieredStore(base_dir)
  st.append(record)          # 写热层（自动触发温归档）
  st.query(filter_fn)        # 三层合并查询
  st.stats()
"""
import gzip
import json
import os
import sqlite3
import threading
import time

_HOT_MAX = 500       # 热层最大条数（超过触发温归档）
_WARM_MAX = 5000     # 温层最大条数（超过触发冷压缩）
_COLD_COMPRESS_AT = 10000  # 冷层压缩阈值


class TieredStore:
    """热/温/冷三层存储（线程安全）。"""

    def __init__(self, base_dir: str, name: str = "records"):
        self._lock = threading.Lock()
        self._base = str(base_dir)
        self._name = name
        os.makedirs(self._base, exist_ok=True)
        self._hot_file = os.path.join(self._base, f"{name}.hot.jsonl")
        self._warm_db = os.path.join(self._base, f"{name}.warm.db")
        self._cold_file = os.path.join(self._base, f"{name}.cold.jsonl.gz")
        self._hot_cache: list[dict] = []
        self._load_hot()
        with self._lock, sqlite3.connect(self._warm_db) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS warm("
                         "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "ts REAL, data TEXT)")

    # ── 热层 ──────────────────────────────────────────────
    def _load_hot(self):
        """启动时加载热层（最多 _HOT_MAX 条）。"""
        if not os.path.exists(self._hot_file):
            return
        try:
            with open(self._hot_file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            self._hot_cache.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
            self._hot_cache = self._hot_cache[-_HOT_MAX:]
        except OSError:
            pass

    def append(self, record: dict) -> None:
        """写一条记录（带时间戳）。热层满自动温归档。"""
        rec = dict(record)
        rec.setdefault("ts", time.time())
        with self._lock:
            self._hot_cache.append(rec)
            try:
                with open(self._hot_file, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fh.flush()
            except OSError:
                pass
            if len(self._hot_cache) >= _HOT_MAX:
                self._warm_archive()

    # ── 温层 ──────────────────────────────────────────────
    def _warm_archive(self) -> None:
        """热层 → 温层（SQLite），清空热文件。"""
        if not self._hot_cache:
            return
        with sqlite3.connect(self._warm_db) as conn:
            conn.executemany(
                "INSERT INTO warm(ts, data) VALUES (?,?)",
                [(r.get("ts", time.time()), json.dumps(r, ensure_ascii=False))
                 for r in self._hot_cache])
            # 温层超限 → 最旧的一半转冷
            n = conn.execute("SELECT count(*) FROM warm").fetchone()[0]
            if n > _WARM_MAX:
                old = conn.execute(
                    "SELECT seq, data FROM warm ORDER BY seq LIMIT ?",
                    (n - _WARM_MAX,)).fetchall()
                self._cold_append([json.loads(d) for _, d in old])
                conn.execute("DELETE FROM warm WHERE seq IN (%s)" %
                             ",".join(str(s) for s, _ in old))
        self._hot_cache = []
        try:
            os.remove(self._hot_file)
        except OSError:
            pass

    # ── 冷层 ──────────────────────────────────────────────
    def _cold_append(self, records: list[dict]) -> None:
        """温层 → 冷层（gzip jsonl，压缩存储）。"""
        if not records:
            return
        mode = "ab" if os.path.exists(self._cold_file) else "wb"
        # gzip 二进制模式不支持 encoding 参数：文本手动编码
        with gzip.open(self._cold_file, mode) as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\n")

    # ── 查询 ──────────────────────────────────────────────
    def query(self, limit: int = 100, filter_fn=None) -> list[dict]:
        """三层合并查询（热优先 → 温 → 冷），可选 filter_fn(record)->bool。"""
        out: list[dict] = []
        with self._lock:
            # 热层（最新）
            for r in reversed(self._hot_cache):
                if filter_fn is None or filter_fn(r):
                    out.append(r)
                if len(out) >= limit:
                    return out
            # 温层（次新，倒序；全表扫描避免 LIMIT 截断漏掉旧记录——温层最多 _WARM_MAX 条，全读可接受）
            with sqlite3.connect(self._warm_db) as conn:
                rows = conn.execute(
                    "SELECT data FROM warm ORDER BY seq DESC").fetchall()
            for (data,) in rows:
                try:
                    r = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if filter_fn is None or filter_fn(r):
                    out.append(r)
                if len(out) >= limit:
                    return out
            # 冷层（最旧，gzip 读）
            if os.path.exists(self._cold_file):
                try:
                    with gzip.open(self._cold_file, "rt", encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                r = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if filter_fn is None or filter_fn(r):
                                out.append(r)
                            if len(out) >= limit:
                                return out
                except OSError:
                    pass
        return out

    def stats(self) -> dict:
        with self._lock:
            with sqlite3.connect(self._warm_db) as conn:
                n_warm = conn.execute("SELECT count(*) FROM warm").fetchone()[0]
            n_cold = 0
            if os.path.exists(self._cold_file):
                try:
                    with gzip.open(self._cold_file, "rt", encoding="utf-8") as fh:
                        n_cold = sum(1 for _ in fh)
                except OSError:
                    pass
            return {"hot": len(self._hot_cache), "warm": n_warm, "cold": n_cold,
                    "base": self._base, "name": self._name}
