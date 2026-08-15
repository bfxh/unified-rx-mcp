#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""realtime_watch —— IDE 实时触发（2026-08-15，阶段1）。

文件改动 → 即时增量扫描（bug_scan+std_check 双维度）→ scan-log 打点。
复用 shadow_core 的文件指纹机制（不引入 watchdog 依赖）。

- WatchLoop：常驻线程（每 UNIFIED_RX_WATCH_INTERVAL 秒扫指纹）
- watch_once()：单轮扫描（测试/手动触发入口）
- 环境变量：UNIFIED_RX_WATCH_INTERVAL（默认 2s）、
  UNIFIED_RX_WATCH_ROOTS（分号分隔根目录——默认 cwd）、
  UNIFIED_RX_WATCH_ENABLED（默认 1——daemon 循环启用）
"""
import json
import os
import threading
import time

import shadow_core


def _watch_roots() -> list[str]:
    raw = os.environ.get("UNIFIED_RX_WATCH_ROOTS", "") or os.getcwd()
    return [r.strip() for r in raw.split(";") if r.strip()]


def _interval() -> float:
    try:
        return max(0.5, float(os.environ.get("UNIFIED_RX_WATCH_INTERVAL", "2")))
    except ValueError:
        return 2.0


def _iter_roots() -> list[str]:
    """收集根目录下可扫描文件（深度 ≤3 防大仓库爆炸——白名单扩展名复用）。"""
    out = []
    for root in _watch_roots():
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in ("node_modules", "target", "dist",
                                        ".git", "__pycache__")]
            depth = dirpath[len(root):].count(os.sep)
            if depth > 3:
                dirnames[:] = []
                continue
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                if shadow_core._is_scanable(p):
                    out.append(p)
                    if len(out) >= 500:
                        return out
    return out


class WatchLoop:
    """实时监听循环（daemon 线程——与 _spawn_self_scan 同模式）。"""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_sigs: dict[str, str] = {}
        self._last_error: str | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="rx-watch")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                watch_once(scan=True)
            except Exception as e:  # 尽力而为
                self._last_error = f"{type(e).__name__}: {e}"
            self._stop.wait(_interval())

    def tick(self) -> dict:
        """单轮检查（测试/手动）——不自动扫描，返回变更列表。"""
        return watch_once(scan=False)


def watch_once(scan: bool = True) -> dict:
    """单轮：收集文件指纹变化 → （可选）增量扫描 + scan-log 打点。

    返回 {changed: [...], scanned: n, elapsed_s}。幂等只读。
    """
    t0 = time.perf_counter()
    changed: list[str] = []
    files = _iter_roots()
    sigs: dict[str, str] = {}
    for p in files:
        sig = shadow_core._file_sig(p)
        if sig:
            sigs[p] = sig
            if p not in _WATCHER._last_sigs or _WATCHER._last_sigs[p] != sig:
                changed.append(p)
    # 消失的文件也计变更（删除）
    for p in list(_WATCHER._last_sigs):
        if p not in sigs:
            changed.append(p)
    _WATCHER._last_sigs = sigs
    scanned = 0
    if scan and changed:
        scanned = _scan_changed(changed)
    return {"changed": changed[:50], "changed_total": len(changed),
            "scanned": scanned, "elapsed_s": round(time.perf_counter() - t0, 3)}


def _scan_changed(changed: list[str]) -> int:
    """增量扫描变更文件（bug_scan + std_check 双维度——单文件，快）。"""
    from server import _call
    import scan_log_core
    n = 0
    for p in changed[:50]:
        try:
            # bug_scan 维度
            d = json.loads(_call("bug_scan", {"path": p})[0].text)
            n_issues = len(d.get("issues", [])) if isinstance(d, dict) else -1
            scan_log_core.append_scan({
                "tool": "watch_bug", "root": p, "ok": n_issues >= 0,
                "summary": f"watch 改动扫描 {os.path.basename(p)}: issues={n_issues}"})
            # std_check 维度
            s = json.loads(_call("std_check", {"path": p})[0].text)
            s_n = len(s.get("issues", [])) if isinstance(s, dict) else -1
            scan_log_core.append_scan({
                "tool": "watch_std", "root": p, "ok": s_n >= 0,
                "summary": f"watch 标准扫描 {os.path.basename(p)}: issues={s_n}"})
            n += 1
        except Exception:  # 尽力而为（单文件失败不影响其他）
            continue
    return n


_WATCHER = WatchLoop()


def start_watcher() -> None:
    """启动监听线程（幂等——已启动不重复）。"""
    if os.environ.get("UNIFIED_RX_WATCH_ENABLED", "1") != "0":
        _WATCHER.start()


def watcher_status() -> dict:
    return {"running": bool(_WATCHER._thread and _WATCHER._thread.is_alive()),
            "interval_s": _interval(),
            "roots": _watch_roots(),
            "last_error": _WATCHER._last_error,
            "tracked_files": len(_WATCHER._last_sigs)}
