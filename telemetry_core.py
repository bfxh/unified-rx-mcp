#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""telemetry_core — unified-rx 遥测桥接层（Python → rx-telemetry Rust 常驻子进程）。

对齐 rx-core 接线模式（server.py R1）：Popen 常驻子进程 + stdin 行协议。
采集（阶段1）：
  - tick_tool(name, args, wall_ms, ok, err)：工具调用耗时/状态/错误
  - tick_hb(loop_name, cycle_ms)：daemon 循环心跳（卡死检测依据）
查询（阶段2 工具用）：
  - status() / agg(since_ts) / tail(n) / snapshot()
失败静默：exe 未编译 / 子进程崩溃 / 写失败均不影响工具调用（监控不能拖垮被监控者）。
环境变量 RX_TELEMETRY=0 整体禁用。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

# ── exe 查找（对齐 server.py rx-core 的 4 候选约定） ──────────────
_TELEMETRY_EXE = None
for _cand in (
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rx-telemetry", "target", "release", "rx-telemetry.exe"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rx-telemetry", "target", "debug", "rx-telemetry.exe"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rx-telemetry", "target", "release", "rx-telemetry"),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "rx-telemetry", "target", "debug", "rx-telemetry"),
):
    if os.path.exists(_cand):
        _TELEMETRY_EXE = _cand
        break

_proc = None
_lock = threading.Lock()
_ENABLED = None  # 惰性判定

# ── 本地队列 + 后台批量发送（2026-08-16 perf 优化）──────────────
# 原实现每次 tick 都做 Popen 管道往返（~0.5-1ms）——1000 次工具调用
# 拖慢 ~1s（test_perf_fast_dispatch <500ms 挂）。改为：tick 只入内存
# 队列（微秒级），后台线程每 0.5s 或满 50 条批量发 Rust 落盘。
_queue: list[dict] = []
_queue_lock = threading.Lock()
_sender_started = False


def _drain() -> None:
    """取出队列全部并批量发送（Rust 端缓冲满 100 条自动落盘）。
    供查询/flush/退出前同步调用；发送线程也用它。"""
    global _queue
    with _queue_lock:
        items, _queue = _queue, []
    for rec in items:
        _send({"cmd": "record", "rec": rec})


def _sender_loop() -> None:
    """后台发送线程：0.5s 轮询批量 drain（调用线程零阻塞）。
    注意：不用 Event 即时唤醒——若 tick 后立即 flush/查询，主线程
    _drain 能拿到全部记录并同步发送；_lock 串行保证 flush 排在记录后。"""
    while True:
        time.sleep(0.5)
        try:
            with _queue_lock:
                if not _queue:
                    continue
            _drain()
        except Exception:  # noqa: BLE001
            pass


def _enqueue(rec: dict) -> None:
    """入队（线程安全，微秒级不阻塞）；后台线程 0.5s 内批量发送。"""
    global _sender_started
    with _queue_lock:
        _queue.append(rec)
    if not _sender_started:
        _sender_started = True
        threading.Thread(target=_sender_loop, daemon=True,
                         name="rx-telemetry-sender").start()


def enabled() -> bool:
    """是否可用：环境开关 + exe 存在（惰性判定一次）。"""
    global _ENABLED
    if _ENABLED is None:
        _ENABLED = (os.environ.get("RX_TELEMETRY", "1") != "0"
                    and _TELEMETRY_EXE is not None)
    return _ENABLED


def _proc_get():
    """常驻子进程（懒启动 + 崩溃自动重启，对齐 _rxcore_proc_get）。
    锁内创建——发送线程与查询线程并发时不得创建两个 Popen（2026-08-16
    竞态：并发 _proc_get 双进程导致记录写进泄漏管道、查询永远看不到）。"""
    global _proc
    if not enabled():
        return None
    with _lock:
        if _proc is None or _proc.poll() is not None:
            _proc = subprocess.Popen(
                [_TELEMETRY_EXE, "serve"], stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, encoding="utf-8", bufsize=1)
        return _proc


def _send(cmd: dict, timeout: float = 5.0):
    """发一行命令，读一行响应。失败静默返回 None（不拖垮调用方）。"""
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
    except Exception:  # noqa: BLE001 —— 监控失败静默
        return None


def _rss_kb() -> int | None:
    """当前进程 RSS（Windows ctypes，尽力而为）。"""
    try:
        import ctypes
        from ctypes import wintypes
        class PROCESS_MEMORY_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]
        pmc = PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(PROCESS_MEMORY_COUNTERS)
        if ctypes.windll.psapi.GetProcessMemoryInfo(
                ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(pmc),
                pmc.cb):
            return pmc.WorkingSetSize // 1024
    except Exception:  # noqa: BLE001
        pass
    return None


def tick_tool(name: str, args: dict | None, wall_ms: float,
              ok: bool = True, err: str = "") -> None:
    """记录一次工具调用（server.py `_call` finally 调用）。入队即返（微秒级）。"""
    if not enabled():
        return
    try:
        rec = {
            "kind": "tool", "ts": time.time(), "tool": name,
            "wall_ms": round(wall_ms, 3), "status": "ok" if ok else "error",
        }
        if ok:
            if args:
                rec["args"] = json.dumps(args, ensure_ascii=False)[:200]
        elif err:
            rec["err"] = err[:200]
        _enqueue(rec)
    except Exception:  # noqa: BLE001
        pass


def tick_hb(loop_name: str, cycle_ms: float) -> None:
    """记录一次 daemon 循环心跳（含进程 RSS——卡死/内存监控依据）。"""
    if not enabled():
        return
    try:
        rec = {
            "kind": "hb", "ts": time.time(), "loop": loop_name,
            "cycle_ms": round(cycle_ms, 3), "pid": os.getpid(),
            "rss_kb": _rss_kb(),
        }
        _enqueue(rec)
    except Exception:  # noqa: BLE001
        pass


def flush() -> None:
    """强制落盘（先 drain 队列再 flush Rust 缓冲）。"""
    if enabled():
        _drain()
        _send({"cmd": "flush"})


def status() -> dict | None:
    """存储状态（路径/大小/缓冲/已落盘）。查询前先 drain 队列。"""
    _drain()
    return _send({"cmd": "status"})


def agg(since_ts: float | None = None) -> dict | None:
    """聚合报告（耗时 TOP/P95/错误率/heartbeats）。查询前先 drain 队列。"""
    _drain()
    cmd: dict = {"cmd": "agg"}
    if since_ts is not None:
        cmd["since_ts"] = since_ts
    return _send(cmd)


def tail(n: int = 20) -> list | None:
    """最近 n 条记录（流式读）。查询前先 drain 队列。"""
    _drain()
    return _send({"cmd": "tail", "n": n})


def shutdown() -> None:
    """优雅退出（drain 队列 + flush + quit）。"""
    global _proc
    if enabled():
        _drain()
    if _proc is not None and _proc.poll() is None:
        try:
            _send({"cmd": "quit"}, timeout=2.0)
        except Exception:  # noqa: BLE001
            pass
        try:
            _proc.wait(timeout=2.0)
        except Exception:  # noqa: BLE001
            pass
        _proc = None
