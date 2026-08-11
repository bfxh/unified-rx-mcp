#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""scan_log_core — 扫描结果日志（常驻工具自扫落盘，专项目对话可查）。

机制（对齐用户要求："工具本地常驻运行，打开阵地就在跑；扫完的都放到日志；
专门搞这个项目的对话框会看这个日志"）：

- **追加落盘**：每次扫描（bug_scan/std_check/vuln_scan/ui_check/cb_scan）
  完成后自动 append 一条 JSONL 到 `~/.unified-rx/scan-log.jsonl`
  （与 lse-state.json / stats.json 同目录，常驻状态区）。
- **项目维度**：每条记录带 root（项目根），专项目对话可按 root 过滤查询。
- **启动自扫**：server 常驻启动时后台线程对自己扫一轮（自扫=扫 server.py
  自身），结果同样落盘——"包括它自己也会扫自己"。
- **查询**：scan_log 工具按 root/工具名/limit 返回最近记录（供对话查看）。

纯标准库零依赖；写入失败静默（不拖垮工具调用）；JSONL 单行一条。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

# 日志上限（防膨胀：超过则截断保留最近 N 条）
_MAX_LOG_LINES = 2000
# 自扫对象（server.py 自身 + 核心 core 文件）
_SELF_SCAN_FILES = ["server.py", "guard_core.py", "std_core.py", "locate_core.py"]


def log_path() -> Path:
    """日志文件路径：~/.unified-rx/scan-log.jsonl（可被 UNIFIED_RX_SCAN_LOG 覆盖）。"""
    override = os.environ.get("UNIFIED_RX_SCAN_LOG", "")
    if override.strip():
        return Path(override)
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or "."
    return Path(home) / ".unified-rx" / "scan-log.jsonl"


def append_scan(entry: dict) -> None:
    """追加一条扫描日志（JSONL 单行）。失败静默——日志是尽力而为。"""
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tool": entry.get("tool", ""),
            "root": entry.get("root", ""),
            "ok": bool(entry.get("ok", True)),
            "summary": entry.get("summary", ""),
        }
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        _truncate(path)
    except Exception:
        pass  # 日志失败不影响扫描


def _truncate(path: Path) -> None:
    """超过上限截断保留最近 N 行（防长期膨胀）。"""
    try:
        if path.stat().st_size < 512 * 1024:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > _MAX_LOG_LINES:
            tail = lines[-_MAX_LOG_LINES:]
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("\n".join(tail) + "\n", encoding="utf-8")
            tmp.replace(path)
    except Exception:
        pass


def query_logs(root: str | None = None, tool: str | None = None,
               limit: int = 50) -> list[dict]:
    """查询最近扫描日志（可按 root/工具名过滤，limit 默认 50 上限 200）。"""
    try:
        path = log_path()
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        out = []
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if root and rec.get("root") != root:
                continue
            if tool and rec.get("tool") != tool:
                continue
            out.append(rec)
            if len(out) >= min(limit, 200):
                break
        return out
    except Exception:
        return []


def self_scan_files() -> list[str]:
    """自扫文件列表（server.py 同目录的自身核心文件）。"""
    base = os.path.dirname(os.path.abspath(__file__))
    return [os.path.join(base, f) for f in _SELF_SCAN_FILES]


def main() -> None:
    """CLI 自检。"""
    p = log_path()
    print(f"log: {p}")
    print(f"query: {query_logs(limit=5)}")
    print("OK: scan_log_core selftest")


if __name__ == "__main__":
    main()
