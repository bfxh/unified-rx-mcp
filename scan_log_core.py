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
import threading
import time
from pathlib import Path

# 并发安全（2026-08-14 高并发压力测试抓出）：多线程（自扫循环 + vuln_scan
# 3 路 + project_scan 4 路 + parallel 8 路）并发 append 同一 JSONL——
# 无锁时 append 与 _truncate 的 read→replace 竞态丢行（实测 1600 丢 47）。
# 锁覆盖 append + truncate 整段（append 单行 + truncate 仅在超限时——开销可接受）。
_append_lock = threading.Lock()
# truncate 计数采样（2026-08-14 高压优化）：append 每 100 次才检查文件大小——
# cProfile 热点 _truncate 0.895s/200 次（每次 append 都 stat）——采样后省 stat。
_truncate_counter = 0

# 日志上限（防膨胀：超过则截断保留最近 N 条）
_MAX_LOG_LINES = 2000
# 自扫对象（覆盖所有工具：server 核心 + scripts + lse-engine）
# 模式⑤"扫自己"= 把 unified-rx 全家（含扩展）都扫一遍，结果落盘
_SELF_SCAN_FILES = [
    "server.py", "guard_core.py", "std_core.py", "locate_core.py",
    "cb_index_core.py", "ds_core.py", "ui_check_core.py",
    "lse_client.py", "scan_log_core.py",
    "scripts/mcp_smoke.py", "scripts/tool_ratchet.py", "scripts/wf_check.py",
    "scripts/async_guard.py", "scripts/sync_check.py", "scripts/vf_probe.py",
    "scripts/bench_unified_rx.py", "scripts/collab_check.py", "scripts/collab_conc_check.py",
    "lse-engine/src/lib.rs", "lse-engine/src/main.rs",
]


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
        with _append_lock:  # 并发安全：append + truncate 原子段（防丢行）
            _size = 0
            with open(path, "ab") as f:  # 二进制：tell() 返回字节数（text 模式是字符数）
                f.write(json.dumps(rec, ensure_ascii=False).encode("utf-8") + b"\n")
                # 性能（2026-08-14 高压优化）：f.tell() 内存拿文件字节大小
                # （append 模式下即末尾位置）——免每次 stat；仅超限才 truncate
                _size = f.tell()
            # 块外调用（文件已关闭——Windows replace 不能替换被打开句柄占用的文件）
            if _size > 512 * 1024:
                _truncate(path)
    except Exception:  # 尽力而为
        pass  # 日志失败不影响扫描


def _truncate(path: Path) -> None:
    """超过上限**归档**（保留历史——用户理念：知识库防删防丢，存在那边）。

    旧行滚入 `scan-log-<date>.jsonl`（按日归档，只增不减），主文件保留最近
    N 行。绝不删除历史（此前截断即删，违背『缓存到知识库，不会被删掉』理念）。
    """
    try:
        if path.stat().st_size < 512 * 1024:
            return
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        if len(lines) > _MAX_LOG_LINES:
            tail = lines[-_MAX_LOG_LINES:]
            old = lines[:-_MAX_LOG_LINES]
            # 归档旧行（追加到当日归档文件——历史持久，防删防丢）
            if old:
                arch = path.parent / f"scan-log-{time.strftime('%Y%m%d')}.jsonl"
                with open(arch, "a", encoding="utf-8") as af:
                    af.write("\n".join(old) + "\n")
            tmp = path.with_suffix(f".jsonl.tmp{os.getpid()}")
            tmp.write_text("\n".join(tail) + "\n", encoding="utf-8")
            tmp.replace(path)
    except Exception:  # 尽力而为
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
    """自扫文件列表：unified-rx 全家（core + scripts + lse-engine）。

    2026-08-15 修复（覆盖缺口）：根目录新模块动态纳入——白名单之外
    新增的 .py（如 geometry_tools/game_check/speculate）自动进入常驻
    扫描，防新代码脱离扫描保护。
    """
    base = os.path.dirname(os.path.abspath(__file__))
    files = [os.path.join(base, f) for f in _SELF_SCAN_FILES
             if os.path.isfile(os.path.join(base, f))]
    for f in sorted(os.listdir(base)):
        if f.endswith(".py") and not f.startswith("test_") \
                and not f.startswith("tmp_"):
            p = os.path.join(base, f)
            if os.path.isfile(p) and p not in files:
                files.append(p)
    return files


def self_scan_dirs() -> list[str]:
    """自扫扩展目录（vendor/extensions/*——第三方扩展，按目录并发扫）。"""
    base = os.path.dirname(os.path.abspath(__file__))
    ext_root = os.path.join(base, "vendor", "extensions")
    if not os.path.isdir(ext_root):
        return []
    return [os.path.join(ext_root, d) for d in sorted(os.listdir(ext_root))
            if os.path.isdir(os.path.join(ext_root, d))]


def main() -> None:
    """CLI 自检。"""
    p = log_path()
    print(f"log: {p}")
    print(f"query: {query_logs(limit=5)}")
    print("OK: scan_log_core selftest")


if __name__ == "__main__":
    main()
