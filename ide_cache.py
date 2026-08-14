#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""ide_cache.py — IDE 增量同步缓存（IDE_ENHANCE_PLAN R1）。

文件版本跟踪（mtime_ns + size + 首尾哈希）→ LSP 结果缓存：
- 版本未变 → 直接返回缓存（省 LSP spawn + token，大仓库 IDE 操作 token 省 90%+）
- 版本变了 → 重新查询 + 更新缓存
- LRU 上限防膨胀

设计（抄 AetherStudio delta 增量思想）：缓存键 = path + kind（诊断/符号/hover/references）。
"""

import hashlib
import json
import os
import sqlite3
import threading
import time

# 缓存上限（条目数——LRU 淘汰）
_MAX_ENTRIES = 512
# 版本计算采样：头/尾各 N 字节（大文件不全量哈希——版本判断够用）
_SAMPLE_BYTES = 4096

_lock = threading.Lock()
# path -> {version: str, entries: {kind: {"data": ..., "ts": float}}}
_CACHE: dict = {}

# ── 温层持久化（R3：SQLite KV——进程重启恢复，冷查询不重新跑 LSP）──
_WARM_DB: str | None = None  # None = 未启用持久化
_DB_LOCK = threading.Lock()


def enable_persistence(db_path: str) -> None:
    """启用温层持久化（SQLite）。调用一次（幂等）。"""
    global _WARM_DB
    _WARM_DB = db_path
    with _DB_LOCK, sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS ide_cache("
            "path TEXT NOT NULL, kind TEXT NOT NULL, "
            "version TEXT NOT NULL, data TEXT NOT NULL, ts REAL NOT NULL, "
            "PRIMARY KEY(path, kind))"
        )
    # 启动时恢复温层 → 热层
    try:
        with _DB_LOCK, sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT path, kind, version, data FROM ide_cache ORDER BY ts DESC LIMIT ?",
                (_MAX_ENTRIES,),
            ).fetchall()
        with _lock:
            for path, kind, version, data in rows:
                try:
                    entry = _CACHE.setdefault(path, {"version": version, "entries": {}})
                    entry["entries"][kind] = {"data": json.loads(data), "ts": time.time()}
                except (json.JSONDecodeError, TypeError):
                    continue
    except sqlite3.Error:  # 尽力而为（吞错有注释——可追溯）
        pass


def _persist(path: str, kind: str, version: str, data: dict) -> None:
    """温层落盘（后台线程不安全时由调用方持锁——这里独立连接）。"""
    if not _WARM_DB:
        return
    try:
        with _DB_LOCK, sqlite3.connect(_WARM_DB) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO ide_cache(path, kind, version, data, ts) "
                "VALUES(?, ?, ?, ?, ?)",
                (path, kind, version, json.dumps(data, ensure_ascii=False), time.time()),
            )
    except sqlite3.Error:  # 尽力而为（吞错有注释——可追溯）
        pass


def file_version(path: str) -> str | None:
    """文件版本指纹：mtime_ns + size + 首尾采样哈希。

    返回 None = 文件不存在/不可读。mtime+size 先粗判（快），哈希保精确（防同 mtime 篡改）。
    """
    try:
        st = os.stat(path)
    except OSError:
        return None
    try:
        with open(path, "rb") as f:
            head = f.read(_SAMPLE_BYTES)
            if st.st_size > _SAMPLE_BYTES * 2:
                f.seek(-_SAMPLE_BYTES, os.SEEK_END)
                tail = f.read(_SAMPLE_BYTES)
            else:
                tail = b""
    except OSError:
        return None
    h = hashlib.blake2b(head + tail, digest_size=8).hexdigest()
    return f"{st.st_mtime_ns}:{st.st_size}:{h}"


def cached(path: str, kind: str) -> dict | None:
    """版本匹配则返回缓存数据，否则 None（缓存失效）。"""
    ver = file_version(path)
    if ver is None:
        return None
    with _lock:
        entry = _CACHE.get(path)
        if entry and entry["version"] == ver and kind in entry["entries"]:
            entry["entries"][kind]["ts"] = time.time()  # LRU 刷新
            return entry["entries"][kind]["data"]
    return None


def store(path: str, kind: str, data: dict) -> None:
    """存缓存（带版本）+ 温层持久化（R3）。"""
    ver = file_version(path)
    if ver is None:
        return
    with _lock:
        entry = _CACHE.setdefault(path, {"version": ver, "entries": {}})
        entry["version"] = ver
        entry["entries"][kind] = {"data": data, "ts": time.time()}
        _evict_lru()
    _persist(path, kind, ver, data)  # 温层落盘（锁外，独立连接）


def invalidate(path: str | None = None) -> None:
    """失效：单个文件或全部（path=None）。"""
    with _lock:
        if path is None:
            _CACHE.clear()
        else:
            _CACHE.pop(path, None)


def _evict_lru() -> None:
    """LRU 淘汰（超上限时逐出最久未用条目）。"""
    while len(_CACHE) > _MAX_ENTRIES:
        oldest_path = None
        oldest_ts = float("inf")
        for p, entry in _CACHE.items():
            for kind, v in entry["entries"].items():
                if v["ts"] < oldest_ts:
                    oldest_ts = v["ts"]
                    oldest_path = p
        if oldest_path is None:
            break
        _CACHE.pop(oldest_path, None)


def stats() -> dict:
    """缓存统计（调试/诊断用）。"""
    with _lock:
        total_entries = sum(len(e["entries"]) for e in _CACHE.values())
        return {"files": len(_CACHE), "entries": total_entries, "max": _MAX_ENTRIES}


def is_cached(path: str, kind: str) -> bool:
    """版本一致且缓存存在（不返回数据——给调用方决定是否省 token 用）。"""
    return cached(path, kind) is not None
