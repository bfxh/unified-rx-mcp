#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""daemon.py — unified-rx 独立常驻守护（不依赖 RX 会话，打开电脑就在跑）。

用户要求："智能体如 RX 存在就会运行，就会去挖漏洞，然后生成日志；
如果打开 steam 就不会被扫到"——扫描不能依赖 RX 会话活跃。

本守护 = 独立进程/计划任务，4 个并发循环线程（多并发处理不同东西）：
  1. self-scan    模式⑤ 自扫全家（core+scripts+lse-engine 并发 + vendor 扩展目录）
  2. project-scan 模式①④ 跟随话题项目（UNIFIED_RX_PROJECT）+ 最活跃项目（stats 统计）
  3. full-scan    模式② 全盘扫（多项目根并发）
  4. repo-manage  仓库管理（GitHub PR/CI/issue 状态轮询，写入 repo-log）

用法：
  python daemon.py            # 常驻循环（默认）
  python daemon.py --once     # 跑一轮后退出（测试/计划任务单次）
  python daemon.py --repo     # 只跑仓库管理循环

循环间隔环境变量（秒）：UNIFIED_RX_SCAN_INTERVAL_SELF/PROJECT/FULL/REPO
日志：~/.unified-rx/scan-log.jsonl（扫描）+ ~/.unified-rx/repo-log.jsonl（仓库）
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time

# 仓库根 = daemon.py 所在目录（与 server.py 同目录）
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import server  # noqa: E402
import scan_log_core  # noqa: E402


def _safe_interval(name: str, default: float) -> float:
    """读取间隔环境变量（非法值兜底默认——2026-08-13 bug 修复：
    原 float(os.environ.get(...)) 在循环外解析，配置写错（如 "abc"）会抛
    ValueError 使守护线程启动即崩溃、常驻扫描静默失效）。"""
    raw = os.environ.get(name, "").strip()
    try:
        val = float(raw) if raw else default
    except ValueError:
        return default
    return max(10.0, val)

# 仓库管理日志路径
REPO_LOG = os.path.join(os.environ.get("USERPROFILE") or os.environ.get("HOME") or ".",
                        ".unified-rx", "repo-log.jsonl")


def _append_repo_log(entry: dict) -> None:
    try:
        os.makedirs(os.path.dirname(REPO_LOG), exist_ok=True)
        rec = {
            "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            "tool": entry.get("tool", "repo_manage"),
            "ok": bool(entry.get("ok", True)),
            "summary": entry.get("summary", ""),
        }
        with open(REPO_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────
# 1. 自扫（模式⑤）——复用 server 的循环实现
# ─────────────────────────────────────────────────────────────
def _ensure_self_scan_once():
    """确保 server._spawn_self_scan_once 可用（首次调用 _spawn_self_scan 触发赋值）。"""
    if not hasattr(server, "_spawn_self_scan_once"):
        # 直接调用一次触发全局赋值（SKIP 时手动赋值兜底）
        try:
            server._spawn_self_scan()
        except Exception:
            pass
    if not hasattr(server, "_spawn_self_scan_once"):
        # 兜底：无则直接调模块函数（若已暴露）
        server._spawn_self_scan_once = lambda: None  # 最坏情况 no-op
    return server._spawn_self_scan_once


def _loop_self_scan() -> None:
    interval = _safe_interval("UNIFIED_RX_SCAN_INTERVAL_SELF", 300)
    once = _ensure_self_scan_once()
    while True:
        try:
            once()  # 单轮自扫（不启动循环，避免嵌套）
        except Exception:
            pass
        time.sleep(max(10, interval))


# ─────────────────────────────────────────────────────────────
# 2. 项目扫描（模式①④）——跟随话题 + 最活跃
# ─────────────────────────────────────────────────────────────
def _loop_project_scan() -> None:
    interval = _safe_interval("UNIFIED_RX_SCAN_INTERVAL_PROJECT", 120)
    while True:
        try:
            proj = os.environ.get("UNIFIED_RX_PROJECT", "").strip()
            if not proj:
                proj = _most_active_project()
            if proj:
                server._call("project_scan", {"path": proj, "max_files": 100})
        except Exception:
            pass
        time.sleep(max(10, interval))


def _most_active_project() -> str | None:
    """最活跃项目：stats.json 统计调用最多的 root（≥3 次）。"""
    try:
        stats_path = os.path.join(os.environ.get("USERPROFILE") or ".",
                                  ".unified-rx", "stats.json")
        if os.path.exists(stats_path):
            data = json.loads(open(stats_path, encoding="utf-8").read())
            recs = data if isinstance(data, list) else data.get("records", [])
            counts: dict[str, int] = {}
            for r in recs:
                root = str(r.get("root", ""))
                if root:
                    counts[root] = counts.get(root, 0) + 1
            if counts:
                top = max(counts, key=counts.get)
                if counts[top] >= 3:
                    return top
    except Exception:
        pass
    # 缺省常见项目根
    for cand in (r"D:\开发\VoxelForge-Nexus", r"D:\开发\reasonix-src",
                 r"D:\开发\VoxelForge"):
        if os.path.isdir(cand):
            return cand
    return None


# ─────────────────────────────────────────────────────────────
# 3. 全盘扫（模式②）
# ─────────────────────────────────────────────────────────────
def _loop_full_scan() -> None:
    interval = _safe_interval("UNIFIED_RX_SCAN_INTERVAL_FULL", 600)
    while True:
        try:
            server._call("full_scan", {"max_files": 100, "ui": False})
        except Exception:
            pass
        time.sleep(max(10, interval))


# ─────────────────────────────────────────────────────────────
# 4. 仓库管理（GitHub PR/CI/issue 状态轮询）
# ─────────────────────────────────────────────────────────────
def _loop_repo_manage() -> None:
    interval = _safe_interval("UNIFIED_RX_SCAN_INTERVAL_REPO", 300)
    while True:
        try:
            _repo_manage_once()
        except Exception:
            pass
        time.sleep(max(10, interval))


# ─────────────────────────────────────────────────────────────
# 模式⑤ 影子扫描：RX 调用哪个文件，影子跟着扫哪个（30s 轮询）
# ─────────────────────────────────────────────────────────────
def _shadow_scan_callback(path: str):
    """影子扫描回调：对单个文件跑 bug_scan + std_check（走 server 工具）。"""
    try:
        r = server._call("bug_scan", {"path": path})
        d = json.loads(r[0].text if isinstance(r, list) else str(r))
        n = len(d.get("issues", [])) if isinstance(d, dict) else -1
        r2 = server._call("std_check", {"path": path})
        d2 = json.loads(r2[0].text if isinstance(r2, list) else str(r2))
        s = d2.get("summary", {}) if isinstance(d2, dict) else {}
        summary = f"bug={n} std={s.get('critical', 0)}/{s.get('warning', 0)}"
        return True, summary
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:60]


def _loop_shadow_scan() -> None:
    import shadow_core
    # 注入真实沙盒校验（shadow 候选路径必须通过 server._check_path）
    shadow_core._check_path = server._check_path
    interval = _safe_interval("UNIFIED_RX_SCAN_INTERVAL_SHADOW", 30)
    while True:
        try:
            n = shadow_core.shadow_scan_once(_shadow_scan_callback)
            if n:
                scan_log_core.append_scan({
                    "tool": "shadow_scan", "root": "batch", "ok": True,
                    "summary": f"影子扫描补扫 {n} 个文件",
                })
        except Exception:
            pass
        time.sleep(max(10, interval))


# ─────────────────────────────────────────────────────────────
# 模式② 按窗口扫：活动窗口项目 → project_scan（120s）
# ─────────────────────────────────────────────────────────────
def _loop_window_scan() -> None:
    import window_core
    interval = _safe_interval("UNIFIED_RX_SCAN_INTERVAL_WINDOW", 120)
    last_proj = None
    while True:
        try:
            proj = window_core.active_project()
            if proj and proj != last_proj:
                # 排除清单：不扫 Steam/AppData 下无关项目（复用 server 排除）
                if server._scan_excluded(proj):
                    last_proj = proj  # 记录但跳过（避免每轮重试）
                    continue
                last_proj = proj
                server._call("project_scan", {"path": proj, "max_files": 100})
                scan_log_core.append_scan({
                    "tool": "window_scan", "root": proj, "ok": True,
                    "summary": f"按窗口扫: {os.path.basename(proj)}",
                })
        except Exception:
            pass
        time.sleep(max(10, interval))


# ─────────────────────────────────────────────────────────────
# 缓存维护：扫描缓存 LRU 清理 + 状态
# ─────────────────────────────────────────────────────────────
def _loop_cache_maintain() -> None:
    import scan_cache
    interval = _safe_interval("UNIFIED_RX_SCAN_INTERVAL_CACHE", 600)
    while True:
        try:
            st = scan_cache.stats()
            scan_log_core.append_scan({
                "tool": "cache_maintain", "root": "cache", "ok": True,
                "summary": f"扫描缓存 {st['entries']} 条",
            })
        except Exception:
            pass
        time.sleep(max(10, interval))


def _repo_token() -> str:
    """仓库管理 token：环境变量 GH_TOKEN > gh CLI keyring（gh auth token）。"""
    t = os.environ.get("GH_TOKEN", "").strip()
    if t:
        return t
    try:
        import subprocess
        r = subprocess.run(["gh", "auth", "token"], capture_output=True,
                           text=True, timeout=10, encoding="utf-8", errors="replace")
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return ""


def _repo_manage_once() -> None:
    """仓库管理一轮：列 bfxh 各仓库 open PR + 最近 CI 状态，写入 repo-log。"""
    token = _repo_token()
    if not token:
        _append_repo_log({
            "tool": "repo_manage", "ok": False,
            "summary": "未找到 token（GH_TOKEN 或 gh auth login），跳过仓库轮询",
        })
        return
    repos = ["unified-rx-mcp", "-BOYADENAXIESHI", "arch-optimize", "ai-platform",
             "AE-ENGINE", "XY", "DeepSeek-Reasonix"]

    def api(url):
        import urllib.request
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "rx-daemon",
            "Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    for repo in repos:
        try:
            prs = api(f"https://api.github.com/repos/bfxh/{repo}/pulls?state=open&per_page=5")
            n_pr = len(prs)
            _append_repo_log({
                "tool": "repo_manage", "ok": True,
                "summary": f"{repo}: {n_pr} open PRs"
                           + (f"（最新: #{prs[0]['number']} {prs[0]['title'][:40]}）" if prs else ""),
            })
        except Exception as e:  # noqa: BLE001
            _append_repo_log({
                "tool": "repo_manage", "ok": False,
                "summary": f"{repo}: 查询失败 {type(e).__name__}",
            })


def _run_scan_once() -> None:
    """单轮：全部扫描模式跑一遍（--once 用，测试/计划任务单次）。"""
    print("[daemon] once: self_scan + project_scan + full_scan")
    try:
        _ensure_self_scan_once()()
    except Exception as e:
        print(f"[daemon] self_scan 失败: {e}")
    proj = os.environ.get("UNIFIED_RX_PROJECT", "").strip() or _most_active_project()
    if proj:
        try:
            server._call("project_scan", {"path": proj, "max_files": 100})
        except Exception as e:
            print(f"[daemon] project_scan 失败: {e}")
    try:
        server._call("full_scan", {"max_files": 100, "ui": False})
    except Exception as e:
        print(f"[daemon] full_scan 失败: {e}")


def main() -> None:
    ap = argparse.ArgumentParser(description="unified-rx 独立常驻守护")
    ap.add_argument("--once", action="store_true", help="跑一轮后退出")
    ap.add_argument("--repo", action="store_true", help="只跑仓库管理")
    args = ap.parse_args()

    if args.once:
        _run_scan_once()
        _repo_manage_once()
        print("[daemon] once done")
        return

    if args.repo:
        _loop_repo_manage()
        return

    # 常驻：8 个并发循环线程（多并发处理不同东西，互不打扰）
    threads = [
        threading.Thread(target=_loop_self_scan, daemon=True, name="daemon-self"),
        threading.Thread(target=_loop_project_scan, daemon=True, name="daemon-project"),
        threading.Thread(target=_loop_full_scan, daemon=True, name="daemon-full"),
        threading.Thread(target=_loop_repo_manage, daemon=True, name="daemon-repo"),
        threading.Thread(target=_loop_shadow_scan, daemon=True, name="daemon-shadow"),
        threading.Thread(target=_loop_window_scan, daemon=True, name="daemon-window"),
        threading.Thread(target=_loop_cache_maintain, daemon=True, name="daemon-cache"),
    ]
    for t in threads:
        t.start()
    print(f"[daemon] {len(threads)} 并发循环已启动: self/project/full/repo/shadow/window/cache")
    print(f"[daemon] 日志: {scan_log_core.log_path()} / {REPO_LOG}")
    # 主线程保持（daemon 线程随进程退出，这里用 join 常驻）
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("[daemon] 退出")


if __name__ == "__main__":
    main()
