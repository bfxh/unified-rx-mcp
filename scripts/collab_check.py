#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""协作链路验证：unified-rx 调工具 → stats 自动打点 → stats_summary 可见。"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(r"C:\Users\lbx13\AppData\Roaming\reasonix\global-workspace\mcp-servers\unified-rx")
STATE_DIR = Path(os.environ.get("TEMP", ".")) / f"rx_stats_collab_{os.getpid()}"
import shutil
shutil.rmtree(STATE_DIR, ignore_errors=True)
STATE_DIR.mkdir(parents=True)
env = {**os.environ, "UNIFIED_RX_STATE": str(STATE_DIR)}

proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)


def send(o):
    proc.stdin.write((json.dumps(o) + "\n").encode())
    proc.stdin.flush()


def recv():
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError(f"EOF: {proc.stderr.read().decode('utf-8','replace')[-500:]}")
    try:
        return json.loads(line.decode())
    except json.JSONDecodeError:
        raise RuntimeError(f"非 JSON 行: {line[:200]!r}; stderr 尾部: {proc.stderr.read().decode('utf-8','replace')[-300:]!r}")


def call_text(name, args, rid):
    """返回裸文本的工具（math/text 等）。"""
    send({"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": args}})
    r = recv()
    assert r.get("id") == rid, r
    return r["result"]["content"][0]["text"]


def call_json(name, args, rid):
    """返回 JSON 文本的工具（stats/fs 等）。"""
    t = call_text(name, args, rid)
    try:
        return json.loads(t)
    except json.JSONDecodeError as e:
        print(f"[dbg] {name} rid={rid} text={t[:120]!r}")
        raise


def call(name, args, rid):
    return call_json(name, args, rid)

try:
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                     "clientInfo": {"name": "collab", "version": "0.1"}}})
    recv()
    send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
    send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    tools = recv()["result"]["tools"]
    names = {t["name"] for t in tools}
    print("tools total:", len(tools))
    assert len(tools) == 52, f"预期 52（48+stats 4），实际 {len(tools)}"
    for expect in ("stats_record", "stats_summary", "stats_status", "stats_clear"):
        assert expect in names, f"缺 {expect}"

    # 调 3 个核心工具（自动打点；math/text 返回裸串，stats/fs 返回 JSON）
    call("math_ops", {"action": "add", "a": 1, "b": 2}, 10)
    call("fs_stat", {"path": str(ROOT / "server.py")}, 11)
    call_text("text_ops", {"action": "reverse", "s": "abc"}, 12)

    # 用户主动记录一条带 token 的（补充 token 统计）
    call("stats_record", {"task": "manual", "tool": "llm", "action": "think",
                          "tokens_in": 1500, "tokens_out": 800, "duration_ms": 300}, 13)

    # 汇总：应含 3 条自动打点（task=unified-rx）+ 1 条手动
    s = call("stats_summary", {}, 14)
    assert s["total_actions"] == 4, f"预期 4 条（3 自动+1 手动），实际 {s['total_actions']}"
    tasks = {t["task"]: t for t in s["tasks"]}
    assert "unified-rx" in tasks and tasks["unified-rx"]["actions"] == 3, tasks
    assert "manual" in tasks and tasks["manual"]["tokens_in"] == 1500, tasks
    assert s["tokens_total"] == 2300, s
    print("collab OK: 自动打点 3 条 + 手动 1 条 =", s["total_actions"], "| tokens:", s["tokens_total"])
    print("per-task:", {k: v["actions"] for k, v in tasks.items()})

    print("COLLAB PASS")
finally:
    proc.kill()
    try:
        shutil.rmtree(STATE_DIR, ignore_errors=True)
    except OSError:
        pass
