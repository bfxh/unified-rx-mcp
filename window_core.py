#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""window_core.py — 按窗口扫（模式②）：活动窗口对应项目 → project_scan。

原理：前台窗口进程的可执行文件路径 → 所在项目根（向上找 .git/Cargo.toml/
go.mod/package.json 等）→ 对该项目跑 project_scan。不可用时回退
scan-log 最近活跃项目（与最活跃扫描一致）。

纯标准库 + PowerShell 探测（Windows）；失败静默回退。
"""

from __future__ import annotations

import os
import subprocess
import sys

# 项目标记文件（向上探测项目根）
_PROJECT_MARKERS = (".git", "Cargo.toml", "go.mod", "package.json",
                    "pyproject.toml", "requirements.txt", "project.godot")


def _foreground_process() -> str | None:
    """前台窗口进程的可执行路径（PowerShell）。失败返回 None。"""
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Process | Where-Object {$_.MainWindowTitle} | "
             "Sort-Object StartTime -Descending | Select-Object -First 1).Path"],
            capture_output=True, text=True, timeout=8,
            encoding="utf-8", errors="replace")
        if r.returncode == 0:
            p = r.stdout.strip()
            return p if p and p.lower() != "null" else None
    except Exception:
        pass
    return None


def _project_root(path: str) -> str | None:
    """从文件/目录向上找项目根（含标记文件/目录的最近祖先）。"""
    p = path if os.path.isdir(path) else os.path.dirname(path)
    while p and os.path.dirname(p) != p:
        for marker in _PROJECT_MARKERS:
            if os.path.exists(os.path.join(p, marker)):
                return p
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    return None


def active_project() -> str | None:
    """当前活动窗口对应项目根；失败回退 None（由调用方决定是否回退 scan-log）。"""
    exe = _foreground_process()
    if exe:
        root = _project_root(exe)
        if root and os.path.isdir(root):
            return root
    return None


def main() -> None:
    """CLI 自检。"""
    exe = _foreground_process()
    print("前台进程:", exe or "（无活动窗口）")
    proj = active_project()
    print("活动项目:", proj or "（未探测到项目根）")
    print("OK: window_core selftest")


if __name__ == "__main__":
    main()
