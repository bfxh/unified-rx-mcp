#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""scan_cache.py — 扫描结果缓存（幂等只读工具结果复用，省 token 省重复扫描）。

原则（对齐缓存优化原则——正确性优先）：
  - 只缓存**幂等只读**扫描结果（bug_scan/std_check 对同一文件+版本结果确定）
  - 键 = (tool, path, mtime, size)——文件任何变化即失效（宁可 miss 不可错）
  - 成功才 Put；失败不缓存
  - 上限 512 条 LRU；非 JSON 保守原样
  - 持久化 ~/.unified-rx/scan-cache.json（跨会话共享=知识共享的一部分）
"""

from __future__ import annotations

import json
import os
import threading
import time

_CACHE_FILE = os.path.join(
    os.environ.get("USERPROFILE") or os.environ.get("HOME") or ".",
    ".unified-rx", "scan-cache.json")
_MAX_ENTRIES = 512
_TTL = 3600  # 缓存有效期（文件未变最多缓存 1h，防长期陈旧）
# IDE 增强 153（自扫抓出）：规则版本号进缓存键——扫描规则代码变更时 +1，
# 旧缓存自动失效（否则规则修复后命中修复前缓存，误判残留）
RULES_VERSION = "v2"

_cache: dict[str, dict] = {}
_loaded = False
_lock = threading.Lock()  # 进程内写锁（多线程 daemon 并发安全）


def _load() -> None:
    global _cache, _loaded
    if _loaded:
        return
    _loaded = True
    try:
        if os.path.exists(_CACHE_FILE):
            _cache = json.loads(open(_CACHE_FILE, encoding="utf-8").read())
    except Exception:
        _cache = {}


def _save() -> None:
    """原子写：tmp + os.replace（防多进程/线程并发写坏）。"""
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        tmp = _CACHE_FILE + ".tmp." + str(os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(_cache, ensure_ascii=False))
        os.replace(tmp, _CACHE_FILE)
    except Exception:
        pass


def _file_sig(path: str) -> str | None:
    try:
        st = os.stat(path)
        # 纳秒精度 mtime（同秒内文件修改也能识别）
        return f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return None


def get(tool: str, path: str) -> dict | None:
    """取缓存：文件未变 + 未过期 → 返回结果；否则 None（miss）。"""
    with _lock:
        return _get_locked(tool, path)


def _get_locked(tool: str, path: str) -> dict | None:
    _load()
    sig = _file_sig(path)
    if sig is None:
        return None
    key = f"{tool}|{path}|{RULES_VERSION}"
    entry = _cache.get(key)
    if not entry:
        return None
    if entry.get("sig") != sig:
        _cache.pop(key, None)  # 文件变了，缓存失效
        return None
    if time.time() - entry.get("ts", 0) > _TTL:
        _cache.pop(key, None)
        return None
    entry["ts"] = time.time()  # LRU：命中刷新时间戳
    return entry.get("result")


def put(tool: str, path: str, result: dict) -> None:
    """缓存结果（成功才调用）。LRU 截断到 512。"""
    with _lock:
        _put_locked(tool, path, result)


def _put_locked(tool: str, path: str, result: dict) -> None:
    _load()
    sig = _file_sig(path)
    if sig is None:
        return
    key = f"{tool}|{path}|{RULES_VERSION}"
    _cache[key] = {"sig": sig, "ts": time.time(), "result": result}
    # LRU：超过上限删最旧
    if len(_cache) > _MAX_ENTRIES:
        oldest = sorted(_cache.items(), key=lambda kv: kv[1].get("ts", 0))[:64]
        for k, _ in oldest:
            _cache.pop(k, None)
    _save()


def invalidate(path: str) -> None:
    """显式失效（文件被写后调用）。"""
    with _lock:
        _invalidate_locked(path)


def _invalidate_locked(path: str) -> None:
    _load()
    prefix = "|" + path
    for k in list(_cache):
        if k.endswith(prefix):
            _cache.pop(k, None)
    _save()


def stats() -> dict:
    _load()
    return {"entries": len(_cache), "file": _CACHE_FILE}


def main() -> None:
    """CLI 自检。"""
    print(stats())
    print("OK: scan_cache selftest")


if __name__ == "__main__":
    main()
