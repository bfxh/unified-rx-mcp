#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""stress_scan —— 工具集自身压力测试（阶段3，Superluminal 场景：大数据量不崩不卡）。

场景（mode 选择，默认 auto 全跑）：
  log       扫描日志高并发 append（8 线程 × N 条）→ 丢行检测
  telemetry 遥测高并发 tick（8 线程 × N 条）→ 丢记录检测 + 耗时
  index     代码索引/遍历大仓库（给定 path）计时
  file      大文件读取计时（找最大文件读一遍）
输出：每场景 {ok, elapsed_ms, items, throughput, errors, detail}。
失败不抛——单场景失败记入 results 不中断其余。
"""
from __future__ import annotations

import json
import os
import threading
import time


def _t(scene: str, ok: bool, elapsed_ms: float, items: int,
       errors: int = 0, detail: str = "") -> dict:
    return {"scene": scene, "ok": ok, "elapsed_ms": round(elapsed_ms, 1),
            "items": items,
            "throughput": round(items / max(elapsed_ms / 1000.0, 1e-6)),
            "errors": errors, "detail": detail}


def _stress_log(scale: int = 10000) -> dict:
    """8 线程并发 append scan-log → 读回计数（丢行检测）。
    用隔离日志文件（UNIFIED_RX_SCAN_LOG 指向临时文件）——共享 scan-log
    会被 daemon 并发写 + 2000 行截断，干扰丢行判定。"""
    import scan_log_core
    import tempfile
    from pathlib import Path
    n_threads = 8
    per = max(1, scale // n_threads)
    errs = [0]
    barrier = threading.Barrier(n_threads)
    # 隔离日志文件（进程内临时替换——不污染共享 scan-log）
    _orig_env = os.environ.get("UNIFIED_RX_SCAN_LOG")
    iso = os.path.join(tempfile.gettempdir(),
                       f"rx-stress-log-{os.getpid()}-{int(time.time()*1000)}.jsonl")
    os.environ["UNIFIED_RX_SCAN_LOG"] = iso
    for f in (iso,):
        try:
            if os.path.exists(f):
                os.remove(f)
        except OSError:
            pass
    # 临时调大截断上限（压测容量判定不受 2000 行截断干扰），结束恢复
    _orig_max = getattr(scan_log_core, "_MAX_LOG_LINES", 2000)
    scan_log_core._MAX_LOG_LINES = max(scale * 2, 2000)

    def _count_mine():
        """隔离文件内行数。"""
        n = 0
        try:
            if os.path.exists(iso):
                with open(iso, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        if '"tool": "stress_scan"' in line:
                            n += 1
        except OSError:
            pass
        return n

    def worker():
        try:
            barrier.wait()
            for i in range(per):
                scan_log_core.append_scan(
                    {"tool": "stress_scan", "root": "__stress__",
                     "ok": True, "summary": f"stress {i}"})
        except Exception:  # noqa: BLE001
            errs[0] += 1

    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    elapsed = (time.perf_counter() - t0) * 1000
    # 恢复环境变量/截断上限 + 清理隔离文件
    scan_log_core._MAX_LOG_LINES = _orig_max
    if _orig_env is None:
        os.environ.pop("UNIFIED_RX_SCAN_LOG", None)
    else:
        os.environ["UNIFIED_RX_SCAN_LOG"] = _orig_env
    got = _count_mine()
    try:
        os.remove(iso)
    except OSError:
        pass
    total = n_threads * per
    lost = max(0, total - got)
    return _t("log", lost == 0 and errs[0] == 0, elapsed, total, errs[0],
              f"8线程并发 append {total} 条；读回 {got}；丢 {lost}")


def _stress_telemetry(scale: int = 5000) -> dict:
    """8 线程并发 tick_tool → flush → agg 验证条数（丢记录检测）。"""
    try:
        import telemetry_core
    except ImportError:
        # 环境缺失 ≠ 压力缺陷：Rust 桥接模块未构建时标记 skip（server 有降级路径）
        return _t("telemetry", True, 0, 0, 0,
                  "skip: telemetry_core 未构建（RX_TELEMETRY 未启用，server 降级）")
    n_threads = 8
    per = max(1, scale // n_threads)
    errs = [0]
    barrier = threading.Barrier(n_threads)

    def worker():
        try:
            barrier.wait()
            for i in range(per):
                telemetry_core.tick_tool("stress_scan", None, 0.5, True, "")
        except Exception:  # noqa: BLE001
            errs[0] += 1

    t0 = time.perf_counter()
    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    telemetry_core.flush()
    elapsed = (time.perf_counter() - t0) * 1000
    total = n_threads * per
    got = 0
    try:
        agg = telemetry_core.agg()
        got = agg.get("total_calls", 0) if agg else 0
    except Exception:  # noqa: BLE001
        errs[0] += 1
    lost = max(0, total - got)
    return _t("telemetry", lost == 0 and errs[0] == 0, elapsed, total,
              errs[0], f"8线程并发 tick {total} 条；聚合见 {got}；丢 {lost}")


def _walk_files(root: str, limit: int = 50000):
    """遍历文件（跳过噪音目录）。"""
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv",
            "target", "vendor", ".pytest_cache", "build", "dist"}
    n = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            yield os.path.join(dirpath, fn)
            n += 1
            if n >= limit:
                return


def _stress_index(path: str, scale: int = 100000) -> dict:
    """大仓库遍历/索引计时（文件数 + 字节数统计）。"""
    if not os.path.isdir(path):
        return _t("index", False, 0, 0, 1, f"路径不存在: {path}")
    t0 = time.perf_counter()
    files = 0
    bytes_total = 0
    max_file = ("", 0)
    for fp in _walk_files(path, scale):
        files += 1
        try:
            sz = os.path.getsize(fp)
            bytes_total += sz
            if sz > max_file[1]:
                max_file = (fp, sz)
        except OSError:
            pass
    elapsed = (time.perf_counter() - t0) * 1000
    return _t("index", True, elapsed, files, 0,
              f"遍历 {files} 文件 {bytes_total/1024/1024:.1f}MB；"
              f"最大 {os.path.basename(max_file[0])} {max_file[1]/1024:.0f}KB")


def _stress_file(path: str) -> dict:
    """最大文件整读计时（大文件处理流畅性）。"""
    if not os.path.isdir(path):
        return _t("file", False, 0, 0, 1, f"路径不存在: {path}")
    biggest = ("", 0)
    for fp in _walk_files(path, 50000):
        try:
            sz = os.path.getsize(fp)
            if sz > biggest[1]:
                biggest = (fp, sz)
        except OSError:
            pass
    if not biggest[0]:
        return _t("file", True, 0, 0, 0, "无文件")
    t0 = time.perf_counter()
    try:
        with open(biggest[0], "rb") as f:
            data = f.read()
        elapsed = (time.perf_counter() - t0) * 1000
        return _t("file", True, elapsed, len(data), 0,
                  f"读 {os.path.basename(biggest[0])} {len(data)/1024/1024:.1f}MB "
                  f"耗时 {elapsed:.0f}ms")
    except OSError as e:
        return _t("file", False, 0, 0, 1, str(e)[:80])


def stress_scan(path: str = "", mode: str = "auto",
                scale: int = 100000, timeout: int = 300) -> dict:
    """压力测试主入口。mode: auto|log|telemetry|index|file"""
    modes = ["log", "telemetry"] if not path else \
        ["log", "telemetry", "index", "file"]
    if mode != "auto":
        modes = [m for m in modes if m == mode] or [mode]
    results: list[dict] = []
    t0 = time.perf_counter()
    for m in modes:
        try:
            if m == "log":
                r = _stress_log(scale)
            elif m == "telemetry":
                r = _stress_telemetry(scale)
            elif m == "index":
                r = _stress_index(path, scale)
            else:
                r = _stress_file(path)
        except Exception as e:  # noqa: BLE001
            r = _t(m, False, 0, 0, 1, str(e)[:100])
        results.append(r)
        if time.perf_counter() - t0 > timeout:
            results.append(_t("timeout", False, 0, 0, 1,
                              f"超过 {timeout}s 总时限，截断"))
            break
    ok_all = all(r["ok"] for r in results)
    return {"ok": ok_all, "mode": mode, "scale": scale, "results": results,
            "hint": "任一场景 ok=False 即该组件在压力下丢数据/异常——优先修复"}


if __name__ == "__main__":  # CLI 调试入口
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else ""
    mode = sys.argv[2] if len(sys.argv) > 2 else "auto"
    scale = int(sys.argv[3]) if len(sys.argv) > 3 else 100000
    print(json.dumps(stress_scan(path, mode, scale),
                     ensure_ascii=False, indent=1))
