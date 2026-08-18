from __future__ import annotations
import sys as _sys
for _m in ['daemon', 'dashboard', 'telemetry_core', 'storage_tiers', 'backup_core']:
    _sys.modules.setdefault(_m, _sys.modules[__name__])

"""infra_engine — 基础设施引擎。
新技术 = 往本模块增量加函数（不新建零散文件）。
"""


# ══════════════ daemon（合并） ══════════════
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


import argparse
import json
import os

# 引擎根（合并后 __file__ 在 engine/ 下——数据文件在仓库根）
_ENGINE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import sys
import threading
import time

# 仓库根 = daemon.py 所在目录（与 server.py 同目录）
ROOT = _ENGINE_ROOT
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

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


def _hb_tick(loop_name: str, t0: float) -> None:
    """遥测心跳（阶段1）：本轮循环耗时 → rx-telemetry（失败静默——监控不拖垮被监控者）。"""
    try:
        from telemetry_core import tick_hb
        tick_hb(loop_name, (time.perf_counter() - t0) * 1000)
    except Exception:  # noqa: BLE001
        pass


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
    except Exception:  # 尽力而为（吞错有注释——可追溯）
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
        except Exception:  # 尽力而为（吞错有注释——可追溯）
            pass
    if not hasattr(server, "_spawn_self_scan_once"):
        # 兜底：无则直接调模块函数（若已暴露）
        server._spawn_self_scan_once = lambda: None  # 最坏情况 no-op
    return server._spawn_self_scan_once


def _loop_self_scan() -> None:
    interval = _safe_interval("UNIFIED_RX_SCAN_INTERVAL_SELF", 300)
    once = _ensure_self_scan_once()
    while True:
        _hb0 = time.perf_counter()
        try:
            once()  # 单轮自扫（不启动循环，避免嵌套）
        except Exception:  # 尽力而为（吞错有注释——可追溯）
            pass
        finally:
            _hb_tick("daemon-self", _hb0)
        time.sleep(max(10, interval))


# ─────────────────────────────────────────────────────────────
# 2. 项目扫描（模式①④）——跟随话题 + 最活跃
# ─────────────────────────────────────────────────────────────
def _loop_project_scan() -> None:
    interval = _safe_interval("UNIFIED_RX_SCAN_INTERVAL_PROJECT", 120)
    while True:
        _hb0 = time.perf_counter()
        try:
            proj = os.environ.get("UNIFIED_RX_PROJECT", "").strip()
            if not proj:
                proj = _most_active_project()
            if proj:
                server._call("project_scan", {"path": proj, "max_files": 100})
        except Exception:  # 尽力而为（吞错有注释——可追溯）
            pass
        finally:
            _hb_tick("daemon-project", _hb0)
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
    except Exception:  # 尽力而为（吞错有注释——可追溯）
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
        _hb0 = time.perf_counter()
        try:
            server._call("full_scan", {"max_files": 100, "ui": False})
        except Exception:  # 尽力而为（吞错有注释——可追溯）
            pass
        finally:
            _hb_tick("daemon-full", _hb0)
        time.sleep(max(10, interval))


# ─────────────────────────────────────────────────────────────
# 4. 仓库管理（GitHub PR/CI/issue 状态轮询）
# ─────────────────────────────────────────────────────────────
def _loop_repo_manage() -> None:
    interval = _safe_interval("UNIFIED_RX_SCAN_INTERVAL_REPO", 300)
    while True:
        _hb0 = time.perf_counter()
        try:
            _repo_manage_once()
        except Exception:  # 尽力而为（吞错有注释——可追溯）
            pass
        finally:
            _hb_tick("daemon-repo", _hb0)
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
        _hb0 = time.perf_counter()
        try:
            n = shadow_core.shadow_scan_once(_shadow_scan_callback)
            if n:
                scan_log_core.append_scan({
                    "tool": "shadow_scan", "root": "batch", "ok": True,
                    "summary": f"影子扫描补扫 {n} 个文件",
                })
        except Exception:  # 尽力而为（吞错有注释——可追溯）
            pass
        finally:
            _hb_tick("daemon-shadow", _hb0)
        time.sleep(max(10, interval))


# ─────────────────────────────────────────────────────────────
# 模式② 按窗口扫：活动窗口项目 → project_scan（120s）
# ─────────────────────────────────────────────────────────────
def _loop_window_scan() -> None:
    import window_core
    interval = _safe_interval("UNIFIED_RX_SCAN_INTERVAL_WINDOW", 120)
    last_proj = None
    while True:
        _hb0 = time.perf_counter()
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
        except Exception:  # 尽力而为（吞错有注释——可追溯）
            pass
        finally:
            _hb_tick("daemon-window", _hb0)
        time.sleep(max(10, interval))


# ─────────────────────────────────────────────────────────────
# 缓存维护：扫描缓存 LRU 清理 + 状态
# ─────────────────────────────────────────────────────────────
def _loop_cache_maintain() -> None:
    import scan_cache
    interval = _safe_interval("UNIFIED_RX_SCAN_INTERVAL_CACHE", 600)
    while True:
        _hb0 = time.perf_counter()
        try:
            st = scan_cache.stats()
            scan_log_core.append_scan({
                "tool": "cache_maintain", "root": "cache", "ok": True,
                "summary": f"扫描缓存 {st['entries']} 条",
            })
        except Exception:  # 尽力而为（吞错有注释——可追溯）
            pass
        finally:
            _hb_tick("daemon-cache", _hb0)
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
    except Exception:  # 尽力而为（吞错有注释——可追溯）
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
# ══════════════ dashboard（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""unified-rx 运行仪表盘（零依赖 Web UI，证明 MCP 真在运行）。

纯标准库（http.server + json），无第三方依赖。读取 ~/.unified-rx/ 的
stats.json / scan-log.jsonl / telemetry.jsonl + 旁侧 tools.json，
提供 JSON API 与内嵌 HTML 仪表盘（3s 自动刷新）。

用法：
    python dashboard.py            # http://127.0.0.1:17300
    RX_DASH_PORT=9000 python dashboard.py
    RX_DASH_DATA=<dir> python dashboard.py   # 自定义数据目录
"""
import http.server
from collections import Counter, defaultdict

PORT = int(os.environ.get("RX_DASH_PORT", "17300"))
DATA_DIR = os.environ.get("RX_DASH_DATA") or os.path.join(
    os.path.expanduser("~"), ".unified-rx")
HERE = _ENGINE_ROOT
START_TS = time.time()

# 数据文件 → 最近 mtime（判断 server 是否活跃）
_FILES = ("stats.json", "scan-log.jsonl", "telemetry.jsonl", "repo-log.jsonl")


def _read_jsonl(path, limit):
    """读 JSONL 尾部 N 条（大文件只读尾——文件可能 GB 级）。"""
    out = []
    try:
        with open(path, "rb") as f:
            # 尾部流式读取：seek 到最后，往回读块
            f.seek(0, 2)
            size = f.tell()
            chunk = b""
            pos = size
            while pos > 0 and len(out) < limit * 4:
                read = min(65536, pos)
                pos -= read
                f.seek(pos)
                chunk = f.read(read) + chunk
                # 按行切出完整行
                lines = chunk.split(b"\n")
                chunk = lines[0]
                for ln in reversed(lines[1:]):
                    if ln.strip():
                        try:
                            out.append(json.loads(ln.decode("utf-8", "replace")))
                        except Exception:
                            pass
                        if len(out) >= limit * 4:
                            break
            return out[:limit]
    except OSError:
        return []


def _read_stats():
    """stats.json 全量聚合（本地文件，缓存友好）。"""
    p = os.path.join(DATA_DIR, "stats.json")
    if not os.path.exists(p):
        return {}
    try:
        with open(p, "r", encoding="utf-8") as f:
            recs = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    total = len(recs)
    by_tool = Counter(r.get("tool", "?") for r in recs)
    dur_by_tool = defaultdict(list)
    for r in recs:
        d = r.get("duration_ms") or 0
        dur_by_tool[r.get("tool", "?")].append(d)
    return {
        "total": total,
        "by_tool": dict(by_tool.most_common(30)),
        "avg_ms": {t: round(sum(v) / len(v), 3) for t, v in dur_by_tool.items()},
    }


def _tools():
    """工具清单（tools.json 旁侧文件，不 import server——零耦合）。"""
    p = os.path.join(HERE, "tools.json")
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d
    except (OSError, json.JSONDecodeError):
        return {"core_count": 0, "ext_count": 0, "total": 0,
                "core": [], "ext": []}


def _overview():
    """运行状态：数据新鲜度（server 活跃判定）+ 汇总。"""
    tools = _tools()
    stats = _read_stats()
    now = time.time()
    files = {}
    latest_ts = 0.0
    for name in _FILES:
        p = os.path.join(DATA_DIR, name)
        try:
            mt = os.path.getmtime(p)
            files[name] = {"mtime": mt, "age_s": round(now - mt, 1)}
            latest_ts = max(latest_ts, mt)
        except OSError:
            files[name] = {"mtime": 0, "age_s": -1}
    # 遥测心跳（daemon 循环活跃度）
    hbs = {}
    tel = _read_jsonl(os.path.join(DATA_DIR, "telemetry.jsonl"), 200)
    for r in tel:
        if r.get("kind") == "hb":
            hbs[r.get("loop", "?")] = r.get("ts", 0)
    return {
        "ok": True,
        "server_uptime_s": round(now - START_TS, 1),
        "data_latest_age_s": round(now - latest_ts, 1) if latest_ts else -1,
        "files": files,
        "heartbeats": hbs,
        "tools": {"core": tools.get("core_count", 0),
                  "ext": tools.get("ext_count", 0),
                  "total": tools.get("total", 0)},
        "stats_total": stats.get("total", 0),
    }


def _scanlog(limit=15):
    recs = _read_jsonl(os.path.join(DATA_DIR, "scan-log.jsonl"), limit)
    return [{"ts": r.get("ts"), "tool": r.get("tool"), "root": r.get("root"),
             "ok": r.get("ok"), "summary": (r.get("summary") or "")[:120]}
            for r in recs]


def _telemetry(limit=300):
    recs = _read_jsonl(os.path.join(DATA_DIR, "telemetry.jsonl"), limit)
    tools = [r for r in recs if r.get("kind") == "tool"]
    n = len(tools)
    err = sum(1 for r in tools if r.get("status") == "error")
    slow = sorted(tools, key=lambda r: -(r.get("wall_ms") or 0))[:8]
    return {
        "samples": n,
        "err_count": err,
        "err_rate": round(err / n, 3) if n else 0,
        "slowest": [{"tool": r.get("tool"), "ms": r.get("wall_ms"),
                     "status": r.get("status")} for r in slow],
    }


def _live(limit=20):
    """最近调用流（stats.json 尾部）。"""
    recs = _read_jsonl(os.path.join(DATA_DIR, "stats.json"), limit)
    # stats.json 是 JSON 数组而非 JSONL——用 read_stats 尾部
    if not recs:
        try:
            with open(os.path.join(DATA_DIR, "stats.json"), "r", encoding="utf-8") as f:
                allr = json.load(f)
            recs = allr[-limit:]
        except Exception:
            recs = []
    return [{"ts": r.get("ts"), "tool": r.get("tool"),
             "ms": round(r.get("duration_ms") or 0, 3)} for r in recs]


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默（不刷屏）
        pass

    def _json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/overview":
                return self._json(_overview())
            if path == "/api/tools":
                t = _tools()
                st = _read_stats()
                core = [{"name": n, "calls": st.get("by_tool", {}).get(n, 0)}
                        for n in t.get("core", [])]
                return self._json({"ok": True, "core": core,
                                   "ext": t.get("ext", [])})
            if path == "/api/scanlog":
                return self._json({"ok": True, "records": _scanlog()})
            if path == "/api/telemetry":
                return self._json({"ok": True, **_telemetry()})
            if path == "/api/live":
                return self._json({"ok": True, "records": _live()})
            if path == "/":
                body = _PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            return self._json({"ok": False, "error": f"未知路径 {path}"})
        except Exception as e:  # noqa: BLE001 —— API 单点异常不拖垮服务
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})


_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>unified-rx 运行仪表盘</title>
<style>
  :root { --bg:#0d1117; --card:#161b22; --line:#30363d; --fg:#e6edf3;
          --dim:#8b949e; --ok:#3fb950; --warn:#d29922; --err:#f85149;
          --acc:#58a6ff; }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--fg); font:14px/1.5 "Segoe UI",
         "Microsoft YaHei", sans-serif; padding:20px; }
  header { display:flex; align-items:center; gap:16px; flex-wrap:wrap;
           margin-bottom:18px; }
  h1 { font-size:20px; }
  .dot { width:10px; height:10px; border-radius:50%; background:var(--ok);
         display:inline-block; animation:pulse 2s infinite; }
  @keyframes pulse { 50% { opacity:.35; } }
  .pill { background:var(--card); border:1px solid var(--line); padding:4px 12px;
          border-radius:999px; font-size:13px; color:var(--dim); }
  .pill b { color:var(--fg); }
  .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr));
          gap:14px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:14px; }
  .card h2 { font-size:14px; color:var(--dim); margin-bottom:10px;
             font-weight:600; }
  .bar { display:flex; align-items:center; gap:8px; margin:4px 0; }
  .bar .name { width:130px; text-align:right; color:var(--dim); font-size:12px;
               overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .bar .track { flex:1; background:#21262d; border-radius:4px; height:14px; }
  .bar .fill { height:14px; border-radius:4px; background:var(--acc);
               transition:width .5s; }
  .bar .num { width:56px; color:var(--dim); font-size:12px; }
  table { width:100%; border-collapse:collapse; font-size:13px; }
  td, th { padding:4px 8px; border-bottom:1px solid var(--line); text-align:left; }
  .ok { color:var(--ok); } .warn { color:var(--warn); } .err { color:var(--err); }
  .live { max-height:280px; overflow-y:auto; font-family:Consolas,monospace;
          font-size:12px; }
  .live div { padding:2px 4px; border-bottom:1px dashed var(--line); }
  .muted { color:var(--dim); font-size:12px; }
  .stamp { margin-top:14px; color:var(--dim); font-size:11px; text-align:center; }
</style>
</head>
<body>
<header>
  <h1>&#9889; unified-rx 运行仪表盘</h1>
  <span class="dot" id="dot"></span>
  <span class="pill">工具 <b id="tools">&mdash;</b></span>
  <span class="pill">累计调用 <b id="calls">&mdash;</b></span>
  <span class="pill">数据新鲜度 <b id="fresh">&mdash;</b></span>
  <span class="pill">本页进程 <b id="uptime">&mdash;</b></span>
</header>
<div class="grid">
  <div class="card"><h2>&#128293; 工具调用热榜（TOP 10）</h2><div id="bars"></div></div>
  <div class="card"><h2>&#128225; 实时调用流（最近 20 条）</h2><div class="live" id="live"></div></div>
  <div class="card"><h2>&#128640; 遥测（最近 300 样本）</h2>
    <table><tr><th>指标</th><th>值</th></tr>
      <tr><td>样本</td><td id="tel_n">&mdash;</td></tr>
      <tr><td>错误率</td><td id="tel_err">&mdash;</td></tr>
      <tr><td>最慢工具</td><td id="tel_slow">&mdash;</td></tr>
    </table>
    <h2 style="margin-top:12px">&#128157; daemon 心跳</h2><div id="hbs" class="muted">&mdash;</div>
  </div>
  <div class="card"><h2>&#129513; 最近扫描（scan-log）</h2>
    <table><thead><tr><th>时间</th><th>工具</th><th>结果</th><th>摘要</th></tr></thead>
    <tbody id="scanlog"></tbody></table>
  </div>
</div>
<div class="stamp">unified-rx dashboard &middot; 数据目录 ~/.unified-rx &middot; 3s 自动刷新</div>
<script>
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmtTs = ts => ts ? new Date(ts * 1000).toLocaleTimeString("zh-CN") : "&mdash;";
const fmtAge = s => s < 0 ? "&mdash;" : (s < 60 ? s + "s" : Math.round(s/60) + "m");
async function jget(p) { const r = await fetch(p); return r.json(); }
function bars(data) {
  const top = data.slice(0, 10);
  const max = Math.max(...top.map(t => t.calls), 1);
  document.getElementById("bars").innerHTML = top.map(t =>
    `<div class="bar"><span class="name" title="${esc(t.name)}">${esc(t.name)}</span>
     <span class="track"><span class="fill" style="width:${Math.round(t.calls/max*100)}%"></span></span>
     <span class="num">${t.calls}</span></div>`).join("");
}
function live(recs) {
  document.getElementById("live").innerHTML = recs.slice().reverse().map(r =>
    `<div>${fmtTs(r.ts)} &middot; ${esc(r.tool)} &middot; ${r.ms}ms</div>`).join("")
    || "<div class='muted'>暂无调用</div>";
}
function telemetry(t) {
  document.getElementById("tel_n").textContent = t.samples;
  document.getElementById("tel_err").textContent = t.samples
    ? (t.err_rate*100).toFixed(1) + "%（" + t.err_count + " 次）" : "&mdash;";
  document.getElementById("tel_slow").textContent = t.slowest.length
    ? t.slowest.map(s => `${esc(s.tool)} ${s.ms}ms`).join(" &middot; ") : "&mdash;";
}
function hbs(h) {
  const now = Date.now()/1000;
  const items = Object.entries(h).map(([k, ts]) =>
    `<span class="${now-ts<300?"ok":"err"}">${esc(k)} ${fmtAge(now-ts)}前</span>`)
    .join("&nbsp; ");
  document.getElementById("hbs").innerHTML = items
    || "<span class='muted'>无心跳记录</span>";
}
function scanlog(recs) {
  document.getElementById("scanlog").innerHTML = recs.map(r =>
    `<tr><td>${fmtTs(typeof r.ts === "string" ? Date.parse(r.ts)/1000 : r.ts)}</td>
     <td>${esc(r.tool)}</td>
     <td class="${r.ok ? "ok" : "err"}">${r.ok ? "OK" : "FAIL"}</td>
     <td class="muted">${esc(r.summary)}</td></tr>`).join("")
    || "<tr><td colspan=4 class='muted'>暂无扫描记录</td></tr>";
}
async function tick() {
  try {
    const [ov, tools, tel, sl, lv] = await Promise.all([
      jget("/api/overview"), jget("/api/tools"), jget("/api/telemetry"),
      jget("/api/scanlog"), jget("/api/live")]);
    document.getElementById("dot").style.background =
      (ov.data_latest_age_s >= 0 && ov.data_latest_age_s < 600) ? "var(--ok)" : "var(--warn)";
    document.getElementById("tools").textContent = ov.tools.total
      ? ov.tools.total + "（核心 " + ov.tools.core + "）" : "&mdash;";
    document.getElementById("calls").textContent = ov.stats_total;
    document.getElementById("fresh").textContent = fmtAge(ov.data_latest_age_s);
    document.getElementById("uptime").textContent = Math.round(ov.server_uptime_s) + "s";
    bars(tools.core); telemetry(tel); hbs(ov.heartbeats);
    scanlog(sl.records); live(lv.records);
  } catch (e) { /* 首帧可能失败，下轮重试 */ }
}
tick(); setInterval(tick, 3000);
</script>
</body>
</html>"""


def main_das() -> int:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), _Handler)
    print(f"unified-rx dashboard: http://127.0.0.1:{PORT}  (数据目录 {DATA_DIR})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main_das())
# ══════════════ telemetry_core（合并） ══════════════
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

import subprocess

# ── exe 查找（对齐 server.py rx-core 的 4 候选约定） ──────────────
_TELEMETRY_EXE = None
for _cand in (
    os.path.join(_ENGINE_ROOT, "rx-telemetry", "target", "release", "rx-telemetry.exe"),
    os.path.join(_ENGINE_ROOT, "rx-telemetry", "target", "debug", "rx-telemetry.exe"),
    os.path.join(_ENGINE_ROOT, "rx-telemetry", "target", "release", "rx-telemetry"),
    os.path.join(_ENGINE_ROOT, "rx-telemetry", "target", "debug", "rx-telemetry"),
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


# ── 健康检查（卡死检测——阶段2） ─────────────────────────────────
# daemon 循环最大间隔 600s（daemon-full），允许 1.5× 余量：
# 心跳 age 超过 STALE_AFTER_SEC 即判定循环异常（卡死/挂了）。
STALE_AFTER_SEC = 900.0


def health_check() -> dict:
    """卡死检测：每个 daemon 循环最近心跳的 age（秒）+ stale 判定。
    输出供 telemetry_snapshot 使用（AI 一键体检）。"""
    a = agg()
    if not a:
        return {"ok": False, "loops": {}}
    now = time.time()
    loops = {}
    for name, hb in (a.get("heartbeats") or {}).items():
        age = max(0.0, now - hb.get("last_ts", 0.0))
        loops[name] = {
            "count": hb.get("count", 0),
            "age_sec": round(age, 1),
            "last_cycle_ms": round(hb.get("last_cycle_ms", 0.0), 1),
            "stale": age > STALE_AFTER_SEC,
        }
    return {"ok": True, "loops": loops}


def recent_errors(n: int = 5) -> list:
    """最近 n 条错误记录（供 snapshot/RCA 关联）。"""
    recs = tail(max(n * 4, 20)) or []
    errs = [r for r in recs if r.get("kind") == "tool" and r.get("status") == "error"]
    return errs[-n:]


def alarms_path() -> str:
    """告警文件路径（~/.unified-rx/alarms.jsonl）。"""
    d = os.environ.get("UNIFIED_RX_STATE_DIR", "").strip()
    if not d:
        d = os.path.join(os.environ.get("USERPROFILE") or
                         os.environ.get("HOME") or ".", ".unified-rx")
    return os.path.join(d, "alarms.jsonl")


def read_alarms(n: int = 10) -> list:
    """读最近 n 条告警（alarms.jsonl 尾部，流式不整载）。"""
    path = alarms_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        out = []
        for line in lines[-n:]:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        return out
    except OSError:
        return []


# ── 告警规则引擎（阶段2：自动监控告警） ───────────────────────────
# 规则（AI 可读结构化告警 → alarms.jsonl，供 snapshot/RCA 消费）：
#   1. tool_p95_slow   某工具 P95 耗时超阈值（默认 5000ms）      → WARN
#   2. tool_err_rate   某工具错误率超阈值（默认 50%，需 ≥3 次）   → WARN
#   3. daemon_stale    某 daemon 循环心跳过期（卡死）            → CRITICAL
#   4. overall_err_rate 总错误率超阈值（需 ≥10 次调用）           → WARN
# 去重：同 (rule, target) 30 分钟内不重复追加（防刷屏）。
_ALARM_LOCK = threading.Lock()


def _append_alarm(alarm: dict) -> None:
    """追加一条告警（alarms.jsonl，线程安全追加）。"""
    try:
        path = alarms_path()
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        with _ALARM_LOCK:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(alarm, ensure_ascii=False) + "\n")
    except OSError:
        pass


def check_alarms(thresholds: dict | None = None) -> dict:
    """规则引擎一轮：读遥测聚合 + 健康 → 新告警落盘（去重）。
    返回 {checked_at, active_rules, new, total_alarms}。"""
    t = thresholds or {}
    p95_slow_ms = float(t.get("p95_slow_ms", 5000))
    err_rate_high = float(t.get("err_rate_high", 0.5))
    a = agg()
    health = health_check()
    now = time.time()
    alarms: list[dict] = []
    tools = (a or {}).get("tools", {})
    for name, ta in tools.items():
        if ta.get("p95_ms", 0) > p95_slow_ms:
            alarms.append({"rule": "tool_p95_slow", "target": name,
                           "level": "WARN",
                           "msg": f"{name} P95={ta['p95_ms']:.0f}ms 超阈值 {p95_slow_ms:.0f}ms"})
        if ta.get("count", 0) >= 3 and ta.get("count", 1) > 0 and \
                ta.get("err_count", 0) / ta["count"] > err_rate_high:
            alarms.append({"rule": "tool_err_rate", "target": name,
                           "level": "WARN",
                           "msg": f"{name} 错误率 {ta['err_count']}/{ta['count']} 超阈值 {err_rate_high:.0%}"})
    for name, h in health.get("loops", {}).items():
        if h.get("stale"):
            alarms.append({"rule": "daemon_stale", "target": name,
                           "level": "CRITICAL",
                           "msg": f"循环 {name} 心跳过期 {h.get('age_sec', 0):.0f}s（可能卡死）"})
    if a and a.get("total_calls", 0) >= 10 and \
            a.get("overall_err_rate", 0) > err_rate_high:
        alarms.append({"rule": "overall_err_rate", "target": "*",
                       "level": "WARN",
                       "msg": f"总错误率 {a['overall_err_rate']:.0%} 超阈值 {err_rate_high:.0%}"})
    # 去重落盘（同 rule+target 30 分钟内已告警 → 跳过）
    existing = read_alarms(300)
    new = []
    for al in alarms:
        dup = any(e.get("rule") == al["rule"] and e.get("target") == al["target"]
                  and now - e.get("ts", 0) < 1800 for e in existing)
        if dup:
            continue
        al["ts"] = now
        new.append(al)
        _append_alarm(al)
    return {"checked_at": now, "active_rules": len(alarms),
            "new": new, "total_alarms": len(read_alarms(1000))}


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
# ══════════════ storage_tiers（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""storage_tiers.py — P2b 热/温/冷三层存储（抄 AetherStudio ai_hot_data 思路）。

设计：
  - 热数据（hot）：最近 N 条在内存 + 追加式 JSONL（mmap 思路的 Python 版：open append + flush）
  - 温数据（warm）：超过阈值自动归档进 SQLite（可查询、可过滤）
  - 冷数据（cold）：超长期不访问的旧数据压缩存储（gzip jsonl）
  - 查询自动合并三层（热优先，温次之，冷最后）

与 scan-log 的关系：scan_log_core 的 JSONL 落盘保持不变（兼容），
storage_tiers 作为升级版存储（daemon 日志/教训/索引元数据通用）。

用法：
  st = TieredStore(base_dir)
  st.append(record)          # 写热层（自动触发温归档）
  st.query(filter_fn)        # 三层合并查询
  st.stats()
"""
import gzip
import sqlite3

_HOT_MAX = 500       # 热层最大条数（超过触发温归档）
_WARM_MAX = 5000     # 温层最大条数（超过触发冷压缩）
_COLD_COMPRESS_AT = 10000  # 冷层压缩阈值


class TieredStore:
    """热/温/冷三层存储（线程安全）。"""

    def __init__(self, base_dir: str, name: str = "records"):
        self._lock = threading.Lock()
        self._base = str(base_dir)
        self._name = name
        os.makedirs(self._base, exist_ok=True)
        self._hot_file = os.path.join(self._base, f"{name}.hot.jsonl")
        self._warm_db = os.path.join(self._base, f"{name}.warm.db")
        self._cold_file = os.path.join(self._base, f"{name}.cold.jsonl.gz")
        self._hot_cache: list[dict] = []
        self._load_hot()
        with self._lock, sqlite3.connect(self._warm_db) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS warm("
                         "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                         "ts REAL, data TEXT)")

    # ── 热层 ──────────────────────────────────────────────
    def _load_hot(self):
        """启动时加载热层（最多 _HOT_MAX 条）。"""
        if not os.path.exists(self._hot_file):
            return
        try:
            with open(self._hot_file, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            self._hot_cache.append(json.loads(line))
                        except json.JSONDecodeError:  # 尽力而为（吞错有注释——可追溯）
                            pass
            self._hot_cache = self._hot_cache[-_HOT_MAX:]
        except OSError:  # 尽力而为（吞错有注释——可追溯）
            pass

    def append(self, record: dict) -> None:
        """写一条记录（带时间戳）。热层满自动温归档。"""
        rec = dict(record)
        rec.setdefault("ts", time.time())
        with self._lock:
            self._hot_cache.append(rec)
            try:
                with open(self._hot_file, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    fh.flush()
            except OSError:  # 尽力而为（吞错有注释——可追溯）
                pass
            if len(self._hot_cache) >= _HOT_MAX:
                self._warm_archive()

    # ── 温层 ──────────────────────────────────────────────
    def _warm_archive(self) -> None:
        """热层 → 温层（SQLite），清空热文件。"""
        if not self._hot_cache:
            return
        with sqlite3.connect(self._warm_db) as conn:
            conn.executemany(
                "INSERT INTO warm(ts, data) VALUES (?,?)",
                [(r.get("ts", time.time()), json.dumps(r, ensure_ascii=False))
                 for r in self._hot_cache])
            # 温层超限 → 最旧的一半转冷
            n = conn.execute("SELECT count(*) FROM warm").fetchone()[0]
            if n > _WARM_MAX:
                old = conn.execute(
                    "SELECT seq, data FROM warm ORDER BY seq LIMIT ?",
                    (n - _WARM_MAX,)).fetchall()
                self._cold_append([json.loads(d) for _, d in old])
                conn.execute("DELETE FROM warm WHERE seq IN (%s)" %
                             ",".join(str(s) for s, _ in old))
        self._hot_cache = []
        try:
            os.remove(self._hot_file)
        except OSError:  # 尽力而为（吞错有注释——可追溯）
            pass

    # ── 冷层 ──────────────────────────────────────────────
    def _cold_append(self, records: list[dict]) -> None:
        """温层 → 冷层（gzip jsonl，压缩存储）。"""
        if not records:
            return
        mode = "ab" if os.path.exists(self._cold_file) else "wb"
        # gzip 二进制模式不支持 encoding 参数：文本手动编码
        with gzip.open(self._cold_file, mode) as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False).encode("utf-8") + b"\n")

    # ── 查询 ──────────────────────────────────────────────
    def query(self, limit: int = 100, filter_fn=None) -> list[dict]:
        """三层合并查询（热优先 → 温 → 冷），可选 filter_fn(record)->bool。"""
        out: list[dict] = []
        with self._lock:
            # 热层（最新）
            for r in reversed(self._hot_cache):
                if filter_fn is None or filter_fn(r):
                    out.append(r)
                if len(out) >= limit:
                    return out
            # 温层（次新，倒序；全表扫描避免 LIMIT 截断漏掉旧记录——温层最多 _WARM_MAX 条，全读可接受）
            with sqlite3.connect(self._warm_db) as conn:
                rows = conn.execute(
                    "SELECT data FROM warm ORDER BY seq DESC").fetchall()
            for (data,) in rows:
                try:
                    r = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if filter_fn is None or filter_fn(r):
                    out.append(r)
                if len(out) >= limit:
                    return out
            # 冷层（最旧，gzip 读）
            if os.path.exists(self._cold_file):
                try:
                    with gzip.open(self._cold_file, "rt", encoding="utf-8") as fh:
                        for line in fh:
                            line = line.strip()
                            if not line:
                                continue
                            try:
                                r = json.loads(line)
                            except json.JSONDecodeError:
                                continue
                            if filter_fn is None or filter_fn(r):
                                out.append(r)
                            if len(out) >= limit:
                                return out
                except OSError:  # 尽力而为（吞错有注释——可追溯）
                    pass
        return out

    def stats(self) -> dict:
        with self._lock:
            with sqlite3.connect(self._warm_db) as conn:
                n_warm = conn.execute("SELECT count(*) FROM warm").fetchone()[0]
            n_cold = 0
            if os.path.exists(self._cold_file):
                try:
                    with gzip.open(self._cold_file, "rt", encoding="utf-8") as fh:
                        n_cold = sum(1 for _ in fh)
                except OSError:  # 尽力而为（吞错有注释——可追溯）
                    pass
            return {"hot": len(self._hot_cache), "warm": n_warm, "cold": n_cold,
                    "base": self._base, "name": self._name}
# ══════════════ backup_core（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""backup_core.py — 每日备份 + 回溯（2026-08-17）。

用户要求（2026-08-17）："搞项目的时候会每天备份，备份不会太多" +
"增加回溯的效果"。

- daily_backup(root)：① git 仓库自动 commit + tag（daily-YYYYMMDD）
  ② 项目快照压缩到 ~/.unified-rx/backups/<slug>/<YYYYMMDD>.zip（限量 keep 份，
  默认 7——"备份不会太多"；排除 node_modules/.git/target 等大目录）
- list_snapshots(root)：备份时间线
- rollback(root, date)：回溯到指定快照（恢复前自动把当前状态另存
  .pre-restore-<ts>.zip——防误操作不可逆）
"""
import datetime
import shutil
import zipfile

STATE_DIR = os.path.join(os.path.expanduser("~"), ".unified-rx")
BACKUP_ROOT = os.path.join(STATE_DIR, "backups")

# 快照排除目录（大/可再生内容——控制备份体积）
_EXCLUDE_DIRS = {"node_modules", ".git", "__pycache__", "dist", "build",
                 "target", "models", ".venv", "venv", "env", ".idea",
                 ".vscode", ".mypy_cache", ".pytest_cache", ".ruff_cache",
                 "lse-engine/target", ".unified-rx"}


def _slug(root: str) -> str:
    return os.path.basename(os.path.normpath(root)).replace(" ", "_") or "project"


def _date_str() -> str:
    return datetime.date.today().strftime("%Y%m%d")


def _git(root: str, *args: str) -> str:
    try:
        r = subprocess.run(["git", "-C", root, *args], capture_output=True,
                           text=True, timeout=30)
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _zip_dir(root: str, zip_path: str) -> int:
    """压缩项目目录（排除大目录），返回文件数。

    zip_slip 说明（vuln-scan 2026-08-17）：zip 成员名由 os.path.relpath
    生成（相对 root，无用户输入、无 ../）——**写入方向**无越界可能；
    解压方向（rollback）已做 startswith(root_abs) 防护。
    """
    count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d not in _EXCLUDE_DIRS and not d.startswith(".")]
            for fn in filenames:
                fp = os.path.join(dirpath, fn)
                try:
                    if os.path.getsize(fp) > 100_000_000:  # 单文件 >100MB 跳过
                        continue
                    zf.write(fp, os.path.relpath(fp, root))
                    count += 1
                except OSError:
                    continue
    return count


def daily_backup(root: str, keep: int = 7, do_git: bool = True) -> dict:
    """每日备份：git commit + tag + 限量快照 zip。"""
    if not root or not os.path.isdir(root):
        return {"ok": False, "error": f"目录不存在: {root}"}
    if not 1 <= keep <= 30:
        return {"ok": False, "error": "keep 须在 1..30"}
    root = os.path.normpath(root)
    slug = _slug(root)
    date = _date_str()
    report: dict = {"ok": True, "root": root, "date": date, "slug": slug}

    # 1) git 自动提交 + tag
    git_ok = False
    if do_git and os.path.isdir(os.path.join(root, ".git")):
        _git(root, "add", "-A")
        diff = _git(root, "diff", "--cached", "--stat")
        if diff:
            _git(root, "commit", "-q", "-m", f"daily backup {date}")
        _git(root, "tag", f"daily-{date}", force=True)
        git_ok = True
    report["git"] = {"ok": git_ok, "tag": f"daily-{date}",
                     "note": "非 git 仓库跳过 git 提交" if not git_ok else "已提交+打 tag"}

    # 2) 快照 zip（限量 keep 份）
    proj_dir = os.path.join(BACKUP_ROOT, slug)
    os.makedirs(proj_dir, exist_ok=True)
    zip_path = os.path.join(proj_dir, f"{date}.zip")
    if os.path.exists(zip_path):
        report["snapshot"] = {"path": zip_path, "skipped": "今日已备份"}
    else:
        files = _zip_dir(root, zip_path)
        report["snapshot"] = {"path": zip_path, "files": files,
                              "size_mb": round(os.path.getsize(zip_path) / 1_048_576, 1)}

    # 3) 限量清理（删最旧，保留 keep 份）
    snaps = sorted(f for f in os.listdir(proj_dir) if f.endswith(".zip"))
    removed = []
    while len(snaps) > keep:
        old = snaps.pop(0)
        try:
            os.remove(os.path.join(proj_dir, old))
            removed.append(old)
        except OSError:
            continue
    report["snapshots"] = snaps
    report["removed_old"] = removed
    report["keep"] = keep
    report["backup_root"] = proj_dir
    return report


def list_snapshots(root: str) -> dict:
    """备份时间线（该项目的快照列表 + 大小）。"""
    slug = _slug(root)
    proj_dir = os.path.join(BACKUP_ROOT, slug)
    snaps = []
    if os.path.isdir(proj_dir):
        for fn in sorted(f for f in os.listdir(proj_dir) if f.endswith(".zip")):
            p = os.path.join(proj_dir, fn)
            try:
                st = os.stat(p)
                snaps.append({"snapshot": fn[:-4], "path": p,
                              "size_mb": round(st.st_size / 1_048_576, 1),
                              "ts": st.st_mtime})
            except OSError:
                continue
    return {"ok": True, "root": root, "slug": slug, "snapshots": snaps,
            "count": len(snaps), "backup_root": proj_dir}


def rollback(root: str, date: str) -> dict:
    """回溯到指定日期快照（恢复前先把当前状态另存 .pre-restore-<ts>.zip）。"""
    if not root or not os.path.isdir(root):
        return {"ok": False, "error": f"目录不存在: {root}"}
    root = os.path.normpath(root)
    slug = _slug(root)
    zip_path = os.path.join(BACKUP_ROOT, slug, f"{date}.zip")
    if not os.path.isfile(zip_path):
        return {"ok": False, "error": f"快照不存在: {date}（list_snapshots 查看可用日期）"}
    # 1) 当前状态另存（可逆）
    pre = os.path.join(BACKUP_ROOT, slug, f".pre-restore-{int(time.time())}.zip")
    try:
        _zip_dir(root, pre)
    except Exception as e:
        return {"ok": False, "error": f"当前状态另存失败（中止恢复）: {e}"}
    # 2) 解压覆盖（先删目标内容再解压——排除大目录由 zip 内容决定）
    # zip_slip 防护（vuln-scan 2026-08-17 抓出）：成员路径必须落在 root 内，
    # 否则跳过（恶意 zip 的 ../ 成员会越界写文件）
    root_abs = os.path.abspath(root) + os.sep
    try:
        with zipfile.ZipFile(zip_path) as zf:
            for member in zf.namelist():
                target = os.path.abspath(os.path.join(root, member))
                if not target.startswith(root_abs):
                    continue  # 越界成员跳过（zip_slip 防护）
                if member.endswith("/"):
                    os.makedirs(target, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(member) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
    except Exception as e:
        return {"ok": False, "error": f"恢复失败: {e}", "pre_restore": pre}
    return {"ok": True, "restored": date, "root": root,
            "pre_restore": pre,
            "note": "当前状态已另存 .pre-restore zip——如需撤销恢复可手动还原"}


if __name__ == "__main__":
    import sys
    import json
    if len(sys.argv) > 1:
        root = sys.argv[2] if len(sys.argv) > 2 else os.getcwd()
        if sys.argv[1] == "backup":
            print(json.dumps(daily_backup(root), ensure_ascii=False, indent=2))
        elif sys.argv[1] == "list":
            print(json.dumps(list_snapshots(root), ensure_ascii=False, indent=2))
        elif sys.argv[1] == "rollback":
            print(json.dumps(rollback(root, sys.argv[3]), ensure_ascii=False, indent=2))
    else:
        print("用法: backup <root> | list <root> | rollback <root> <YYYYMMDD>")