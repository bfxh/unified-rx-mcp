# -*- coding: utf-8 -*-
"""tools/ide_autopilot.py —— 开发目录自动驾驶（S69）。

智能体（MCP server）启动即自动：扫描开发目录的项目 → 逐项目全量体检 →
快照常驻内存（ide_auto_report 随时可取）→ VS Code 顺带打开（一次）。
严苛边界：
- 只扫 root 一层目录，有项目标记（.git/Cargo.toml/pyproject.toml/go.mod）才算
- 快照带去重窗口（默认 10 分钟内复用，多客户端并发启动不重跑不重弹）
- 后台线程异常全捕获落快照（不许静默死）；体检用全量 doctor（基线已坏不静默）
- VS Code 自动打开默认开，UNIFIED_RX_AUTOPILOT_VSCODE=0 关闭
"""
import os
import threading
import time

from registry import tool
from tools.fs import _resolve as _fs_resolve

DEFAULT_ROOT = r"D:\开发"
PROJECT_MARKERS = (".git", "Cargo.toml", "pyproject.toml", "go.mod")
DEDUPE_WINDOW = 600.0

_LOCK = threading.Lock()
_SNAPSHOT = {
    "status": "idle",       # idle/running/done/error
    "root": None,
    "started": None,
    "finished": None,
    "projects": [],
    "vscode_opened": [],
    "error": None,
}
_thread_started = False


def discover_projects(root):
    """root 一层目录里带项目标记的 → 项目路径清单（沙盒内校验）。"""
    real = _fs_resolve(root)
    if not os.path.isdir(real):
        return []
    out = []
    try:
        for name in sorted(os.listdir(real)):
            d = os.path.join(real, name)
            if not os.path.isdir(d):
                continue
            if any(os.path.exists(os.path.join(d, mk)) for mk in PROJECT_MARKERS):
                try:
                    out.append(_fs_resolve(d))
                except ValueError:
                    continue
    except OSError:
        return []
    return out


def _run_autopilot(root, vscode):
    global _SNAPSHOT
    from registry import call

    started = time.time()
    try:
        projects = discover_projects(root)
        results = []
        for p in projects:
            r = call("ide_doctor", {"path": p, "run_tests": False,
                                    "__authorized": True})
            if r.get("ok"):
                res = r["result"]
                results.append({"path": res["path"], "verdict": res["verdict"],
                                "problems": res["problems"][:5],
                                "warns": res["warns"][:3]})
            else:
                results.append({"path": p, "verdict": "error",
                                "problems": [str(r.get("error"))[:120]]})
        order = {"issues": 0, "error": 0, "warn": 1, "clean": 2}
        results.sort(key=lambda x: order.get(x["verdict"], 3))
        opened = []
        if vscode:
            bad = [x["path"] for x in results
                   if x["verdict"] in ("issues", "error")]
            targets = bad or [x["path"] for x in results]
            if targets:
                v = call("ide_vscode", {"action": "open", "paths": targets,
                                        "__authorized": True})
                if v.get("ok"):
                    opened = v["result"]["opened"]
        snap = {
            "status": "done", "root": root,
            "started": started, "finished": time.time(),
            "projects": results, "vscode_opened": opened, "error": None,
        }
    except Exception as e:                                  # noqa: BLE001
        snap = {"status": "error", "root": root, "started": started,
                "finished": time.time(), "projects": [],
                "vscode_opened": [], "error": f"{type(e).__name__}: {e}"[:200]}
    with _LOCK:
        _SNAPSHOT = snap


def autopilot_run(root=None, force=False, sync=False, vscode=None):
    """触发自动驾驶。默认后台线程 + 去重窗口；sync=True 当前线程跑（测试）。"""
    global _thread_started, _SNAPSHOT
    root = root or os.environ.get("UNIFIED_RX_AUTOPILOT_ROOT", DEFAULT_ROOT)
    if vscode is None:
        vscode = os.environ.get("UNIFIED_RX_AUTOPILOT_VSCODE", "1") != "0"
    now = time.time()
    with _LOCK:
        fresh = (_SNAPSHOT["status"] == "done"
                 and _SNAPSHOT.get("root") == root
                 and _SNAPSHOT.get("finished")
                 and now - _SNAPSHOT["finished"] < DEDUPE_WINDOW)
        if fresh and not force:
            return dict(_SNAPSHOT, reused=True)
        if not sync:
            if _thread_started and _SNAPSHOT["status"] == "running":
                return dict(_SNAPSHOT, reused=True)
            _SNAPSHOT = {"status": "running", "root": root, "started": now,
                         "finished": None, "projects": [],
                         "vscode_opened": [], "error": None}
            t = threading.Thread(target=_run_autopilot, args=(root, vscode),
                                 daemon=True)
            t.start()
            _thread_started = True
            return dict(_SNAPSHOT, reused=False)
    _run_autopilot(root, vscode)
    with _LOCK:
        return dict(_SNAPSHOT, reused=False)


@tool("ide_auto_report", "开发目录自动驾驶快照：server 启动即自动体检全部项目"
      "（逐项目 doctor 全量）+ VS Code 顺带打开——智能体一进来就能看到所有项目状态",
      "ide",
      {"type": "object",
       "properties": {
           "root": {"type": "string",
                    "description": "开发目录（默认 D:\\开发，沙盒内）"},
           "force": {"type": "boolean", "description": "强制重跑（忽略去重窗口）"},
           "sync": {"type": "boolean", "description": "同步执行（阻塞到完成）"},
           "vscode": {"type": "boolean",
                      "description": "是否顺带打开 VS Code（默认跟随环境变量，开）"},
       }})
def ide_auto_report(root=None, force=False, sync=False, vscode=None):
    return autopilot_run(root=root, force=force, sync=sync, vscode=vscode)
