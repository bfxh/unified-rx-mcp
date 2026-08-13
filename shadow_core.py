#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""shadow_core.py — 影子扫描（模式⑤）：RX 调用哪一个文件，影子就跟着扫那一个。

原理：
  - RX/客户端通过 MCP 调用工具时会传 path/root 参数，每次调用自动落盘 scan-log
  - 影子循环每 30s 轮询 scan-log 新增记录，提取"被调用的文件"（fs_read/code_complete/
    locate_edit/bug_scan 等带 path 的工具）
  - 对**未扫描过**的文件立即补扫 bug_scan + std_check，结果落盘（知识共享）

效果："改了就扫"——RX 碰哪个文件，影子立刻扫哪个，不用等全盘周期。
纯标准库零依赖；失败静默。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import scan_log_core  # noqa: E402（同目录）

# 沙盒校验回调（daemon 注入 server._check_path；测试可替换为宽松校验）
_check_path = lambda p: p  # noqa: E731（默认不校验；daemon 注入真实沙盒）

# 关注的文件类工具（调用即触发影子扫描）——只含实际落盘 scan-log 的
# 扫描工具（_SCAN_LOG_TOOLS 交集；fs_read 等不落盘，不在此列）
_WATCH_TOOLS = {"bug_scan", "std_check", "locate_edit",
                "hallucination_guard", "ui_check"}
# 已扫描文件记录（path -> mtime:size），防重复扫
_SCANNED: dict[str, str] = {}
_SCANNED_FILE = os.environ.get("UNIFIED_RX_SHADOW_SCANNED", "") or os.path.join(
    os.environ.get("USERPROFILE") or os.environ.get("HOME") or ".",
    ".unified-rx", "shadow-scanned.json")
_loaded = False


def _load_scanned() -> None:
    global _SCANNED, _loaded
    if _loaded:
        return
    _loaded = True
    try:
        if os.path.exists(_SCANNED_FILE):
            _SCANNED = json.loads(open(_SCANNED_FILE, encoding="utf-8").read())
    except Exception:
        _SCANNED = {}


def _save_scanned() -> None:
    try:
        os.makedirs(os.path.dirname(_SCANNED_FILE), exist_ok=True)
        # 上限 2000 条 LRU 截断
        items = sorted(_SCANNED.items(), key=lambda kv: kv[1])[-2000:]
        open(_SCANNED_FILE, "w", encoding="utf-8").write(
            json.dumps(dict(items), ensure_ascii=False))
    except Exception:
        pass


def _file_sig(path: str) -> str | None:
    """文件指纹：mtime(纳秒):size（变化即失效）。"""
    try:
        st = os.stat(path)
        return f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return None


def _is_scanable(path: str) -> bool:
    """可扫描判定：存在 + 常见源码扩展名 + 非排除目录（目录段精确匹配）。"""
    if not path or not os.path.isfile(path):
        return False
    ext = os.path.splitext(path)[1].lower()
    if ext not in (".py", ".rs", ".go", ".ts", ".js", ".md", ".json",
                   ".toml", ".yml", ".yaml", ".gd", ".cs", ".java", ".c",
                   ".h", ".cpp", ".sh", ".txt"):
        return False
    # 目录段精确匹配（防子串误伤：AppData 是用户目录一部分，不能整个排除）
    segs = path.lower().replace("\\", "/").split("/")
    for excl in ("node_modules", "target", "dist", ".git", "__pycache__",
                 "steam", "steamapps", "games"):
        if excl in segs:
            return False
    return True


def shadow_scan_once(scan: callable) -> int:
    """影子扫描一轮：读 scan-log 新增记录 → 提取被调用文件 → 未扫过的补扫。

    scan(path) -> (ok, summary)：实际扫描回调（由 daemon 注入 server._call）。
    返回本轮补扫文件数。
    """
    _load_scanned()
    scanned_now = 0
    try:
        logs = scan_log_core.query_logs(limit=200)
        # 最近 200 条里找带 root/path 的文件类工具调用
        for log in logs:
            tool = log.get("tool", "")
            if tool not in _WATCH_TOOLS:
                continue
            root = str(log.get("root", ""))
            # root 可能是文件或目录：文件直接扫，目录取最新文件
            candidates = []
            if root and _is_scanable(root):
                candidates.append(root)
            elif root and os.path.isdir(root):
                # 目录：取最近修改的源码文件（最多 3 个）
                try:
                    files = sorted(
                        (os.path.join(root, f) for f in os.listdir(root)
                         if _is_scanable(os.path.join(root, f))),
                        key=lambda p: os.stat(p).st_mtime, reverse=True)[:3]
                    candidates.extend(files)
                except OSError:
                    pass
            # 沙盒校验：候选路径必须通过 server._check_path（防任意路径放大）
            for cand in list(candidates):
                try:
                    _check_path(cand)
                except Exception:
                    candidates.remove(cand)
            for cand in candidates:
                sig = _file_sig(cand)
                if sig is None:
                    continue
                if _SCANNED.get(cand) == sig:
                    continue  # 已扫且未变（缓存命中）
                try:
                    ok, summary = scan(cand)
                    _SCANNED[cand] = sig
                    scanned_now += 1
                    scan_log_core.append_scan({
                        "tool": "shadow_scan", "root": cand, "ok": ok,
                        "summary": f"shadow {os.path.basename(cand)}: {summary}",
                    })
                except Exception:
                    pass
        _save_scanned()
    except Exception:
        pass
    return scanned_now


def main() -> None:
    """CLI 自检。"""
    _load_scanned()
    print(f"已扫描缓存: {len(_SCANNED)} 文件")
    print(f"scan-log: {scan_log_core.log_path()}")
    print("OK: shadow_core selftest")


if __name__ == "__main__":
    main()
