#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""search_core — 本地语义代码检索桥接（Python → rx-search Rust 常驻子进程）。

对齐 rx-core/telemetry_core 模式：Popen 常驻 + stdin 行协议。
- index(root)：构建/重建索引（root 变化才重建——内存索引驻留）
- search(q, k, root)：语义检索（BM25 + 符号加权）
- status()：索引状态
失败静默（未编译 → 调用方降级）。
环境变量 RX_SEARCH=0 禁用。
"""
from __future__ import annotations

import json
import os
import subprocess
import threading

_SEARCH_EXE = None
for _cand in (
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rx-search", "target", "release", "rx-search.exe"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rx-search", "target", "debug", "rx-search.exe"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rx-search", "target", "release", "rx-search"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rx-search", "target", "debug", "rx-search"),
):
    if os.path.exists(_cand):
        _SEARCH_EXE = _cand
        break

_proc = None
_lock = threading.Lock()
_indexed_root = None
_ENABLED = None


def enabled() -> bool:
    global _ENABLED
    if _ENABLED is None:
        _ENABLED = (os.environ.get("RX_SEARCH", "1") != "0"
                    and _SEARCH_EXE is not None)
    return _ENABLED


def _proc_get():
    global _proc
    if not enabled():
        return None
    with _lock:
        if _proc is None or _proc.poll() is not None:
            _proc = subprocess.Popen(
                [_SEARCH_EXE, "serve"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", bufsize=1)
        return _proc


def _send(cmd: dict, timeout: float = 60.0):
    p = _proc_get()
    if p is None:
        return None
    try:
        with _lock:
            p.stdin.write(json.dumps(cmd, ensure_ascii=False) + "\n")
            p.stdin.flush()
            line = p.stdout.readline()
        if not line:
            return None
        resp = json.loads(line)
        return resp.get("data") if resp.get("ok") else None
    except Exception:  # noqa: BLE001 —— 失败静默（调用方降级）
        return None


def index(root: str, limit: int = 50000) -> dict | None:
    """构建/重建索引（root 变化才重建——常驻内存索引复用）。"""
    global _indexed_root
    if not enabled():
        return None
    if _indexed_root == root:
        st = status()
        if st:
            return st
    d = _send({"cmd": "index", "root": root, "limit": limit}, timeout=120)
    if d is not None:
        _indexed_root = root
    return d


def search(q: str, root: str = "", k: int = 20,
           limit: int = 50000) -> dict | None:
    """语义检索：未索引/root 变化自动先建索引。"""
    if not enabled():
        return None
    if root:
        index(root, limit)
    hits = _send({"cmd": "search", "q": q, "k": k})
    if hits is None:
        return None
    return {"ok": True, "query": q, "hits": hits, "count": len(hits)}


def status() -> dict | None:
    return _send({"cmd": "status"})


def shutdown() -> None:
    global _proc, _indexed_root
    if _proc is not None and _proc.poll() is None:
        try:
            _send({"cmd": "quit"}, timeout=3.0)
        except Exception:  # noqa: BLE001
            pass
        try:
            _proc.wait(timeout=3.0)
        except Exception:  # noqa: BLE001
            pass
    _proc = None
    _indexed_root = None
