#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""replay_core —— 操作录制/重放（阶段3，BugCraft 式：偶现变必现）。

- replay_record(name, step)：追加一步操作（工具调用 or 命令）到
  `~/.unified-rx/replays/<name>.jsonl`
- replay_run(name)：逐条重放——工具调用走 server._call（权限层照常），
  命令步骤需显式 `__authorized: true`（L4 语义，默认跳过并标注）
- 用途：用户报"崩溃了/偶现 bug" → 回放操作序列 → 必现 → 定位第一步

安全：
  - name 白名单（字母数字-_，防路径穿越）
  - cmd 默认拒绝（需显式授权）；超时保护
  - 全部只读回放语义由调用方保证（工具本身已有权限层）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time

_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def replays_dir() -> str:
    d = os.environ.get("UNIFIED_RX_STATE_DIR", "").strip()
    if not d:
        d = os.path.join(os.environ.get("USERPROFILE") or
                         os.environ.get("HOME") or ".", ".unified-rx")
    return os.path.join(d, "replays")


def _path_for(name: str) -> str | None:
    if not _NAME_RE.match(name or ""):
        return None
    p = os.path.join(replays_dir(), f"{name}.jsonl")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    return p


def replay_record(name: str, step: dict) -> dict:
    """追加一步。step: {type: "tool", tool, args} | {type: "cmd", cmd, cwd, timeout}"""
    p = _path_for(name)
    if p is None:
        return {"ok": False, "error": "录制名非法（仅字母数字-_，≤64）"}
    stype = step.get("type", "")
    if stype not in ("tool", "cmd"):
        return {"ok": False, "error": "type 必须是 tool 或 cmd"}
    rec = {"ts": time.time(), "type": stype}
    if stype == "tool":
        tool = str(step.get("tool", "")).strip()
        if not tool:
            return {"ok": False, "error": "tool 步骤缺少 tool 字段"}
        rec["tool"] = tool
        rec["args"] = step.get("args") or {}
    else:
        cmd = str(step.get("cmd", "")).strip()
        if not cmd:
            return {"ok": False, "error": "cmd 步骤缺少 cmd 字段"}
        rec["cmd"] = cmd
        rec["cwd"] = str(step.get("cwd", "") or "")
        rec["timeout"] = int(step.get("timeout", 60) or 60)
        if step.get("authorized") or (step.get("args") or {}).get("__authorized"):
            rec["authorized"] = True  # 显式授权透传（L4 语义）
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    return {"ok": True, "replay": name, "recorded": rec,
            "total": sum(1 for _ in open(p, encoding="utf-8"))}


def _load_steps(name: str) -> tuple[list, str | None]:
    p = _path_for(name)
    if p is None:
        return [], "录制名非法"
    if not os.path.exists(p):
        return [], f"录制不存在: {name}（先 replay_record）"
    steps = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    steps.append(json.loads(line))
                except ValueError:
                    pass
    return steps, None


def replay_run(name: str, stop_on_fail: bool = True) -> dict:
    """逐条重放。工具步骤走 server._call；cmd 步骤需 __authorized。"""
    steps, err = _load_steps(name)
    if err:
        return {"ok": False, "error": err}
    if not steps:
        return {"ok": False, "error": f"录制为空: {name}"}
    results = []
    ok_all = True
    for i, step in enumerate(steps, 1):
        t0 = time.perf_counter()
        entry: dict = {"step": i, "type": step.get("type"),
                       "ts": step.get("ts", 0)}
        if step.get("type") == "tool":
            tool = step.get("tool", "")
            args = step.get("args") or {}
            entry["tool"] = tool
            try:
                import server
                out = server._call(tool, args)
                text = out[0].text if out else ""
                entry["ok"] = True
                entry["result"] = text[:200]
                # 结果含 Error → 视为复现失败点（偶现变必现）
                if text.startswith("Error:"):
                    entry["ok"] = False
                    entry["error"] = text[:200]
                    ok_all = False
            except Exception as e:  # noqa: BLE001
                entry["ok"] = False
                entry["error"] = str(e)[:200]
                ok_all = False
        elif step.get("type") == "cmd":
            if not (step.get("args") or {}).get("__authorized") and \
                    not str(step.get("authorized", "")).lower() in ("1", "true"):
                entry["ok"] = False
                entry["skipped"] = "cmd 步骤需显式授权（args.__authorized=true）"
                ok_all = False if stop_on_fail else ok_all
            else:
                try:
                    r = subprocess.run(
                        str(step.get("cmd", "")), shell=True,
                        cwd=step.get("cwd") or None,
                        capture_output=True, text=True, timeout=60,
                        encoding="utf-8", errors="replace")
                    entry["ok"] = r.returncode == 0
                    entry["returncode"] = r.returncode
                    entry["output"] = (r.stdout + r.stderr)[:200]
                    if r.returncode != 0:
                        ok_all = False
                except subprocess.TimeoutExpired:
                    entry["ok"] = False
                    entry["error"] = "超时 60s"
                    ok_all = False
        else:
            entry["ok"] = False
            entry["error"] = f"未知步骤类型: {step.get('type')}"
            ok_all = False
        entry["wall_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        results.append(entry)
        if stop_on_fail and not entry["ok"]:
            break
    return {
        "ok": ok_all, "replay": name, "steps": len(steps),
        "results": results,
        "failed_at": next((r["step"] for r in results if not r["ok"]), None),
        "hint": ("failed_at 即复现点——偶现变必现；对失败步骤用 "
                 "failure_analyze/bug_bisect 定位根因"),
    }


def replay_list() -> dict:
    """列出所有录制（名称 + 步数 + 最近时间）。"""
    d = replays_dir()
    out = []
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".jsonl"):
                p = os.path.join(d, fn)
                try:
                    n = sum(1 for _ in open(p, encoding="utf-8"))
                    mtime = os.path.getmtime(p)
                except OSError:
                    n, mtime = 0, 0
                out.append({"name": fn[:-6], "steps": n,
                            "updated": time.strftime("%Y-%m-%d %H:%M",
                                                     time.localtime(mtime))})
    return {"ok": True, "replays": out}


if __name__ == "__main__":  # CLI 调试入口
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "list":
        print(json.dumps(replay_list(), ensure_ascii=False, indent=1))
    elif len(sys.argv) > 2 and sys.argv[1] == "run":
        print(json.dumps(replay_run(sys.argv[2]), ensure_ascii=False, indent=1))
    else:
        print("用法: replay_core.py list | run <name>")
