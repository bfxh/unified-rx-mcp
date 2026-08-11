#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""性能基准：unified-rx 工具真实调用性能（进程内 + MCP stdio 协议层）。"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(r"C:\Users\lbx13\AppData\Roaming\reasonix\global-workspace\mcp-servers\unified-rx")
sys.path.insert(0, str(ROOT))
os.environ["UNIFIED_RX_SANDBOX"] = ""
import server

results = {}

# 1) 进程内直接调用（函数级）
n = 2000
t0 = time.perf_counter()
for _ in range(n):
    server._call("math_ops", {"action": "add", "a": 1, "b": 2})
dt = (time.perf_counter() - t0) * 1000
results["inprocess_math_ops"] = {"calls": n, "total_ms": round(dt, 1), "per_call_ms": round(dt / n, 3), "calls_per_sec": round(n / dt * 1000)}

# 2) MCP stdio 协议层（真实进程间）
proc = subprocess.Popen(
    [sys.executable, str(ROOT / "server.py")],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
)

def send(obj):
    proc.stdin.write((json.dumps(obj) + "\n").encode())
    proc.stdin.flush()

def recv():
    line = proc.stdout.readline()
    if not line:
        err = proc.stderr.read().decode("utf-8", "replace")
        raise RuntimeError(f"server EOF: {err[-1500:]}")
    return json.loads(line.decode())

send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                 "clientInfo": {"name": "bench", "version": "0.1"}}})
recv()
send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

# tools/list 耗时（48 工具）
t0 = time.perf_counter()
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
r = recv()
tools_ms = (time.perf_counter() - t0) * 1000
ntools = len(r["result"]["tools"])
results["mcp_tools_list"] = {"tools": ntools, "ms": round(tools_ms, 1)}

# 单次 tools/call 往返
n = 30
t0 = time.perf_counter()
for i in range(n):
    send({"jsonrpc": "2.0", "id": 100 + i, "method": "tools/call",
          "params": {"name": "math_ops", "arguments": {"action": "add", "a": 1, "b": 2}}})
    recv()
dt = (time.perf_counter() - t0) * 1000
results["mcp_call_math_ops"] = {"calls": n, "per_call_ms": round(dt / n, 2), "calls_per_sec": round(n / dt * 1000)}
proc.kill()

# 3) 工具全量清单（28 核心 + 20 扩展）
core = sorted(server._TOOLS.keys())
if not server._EXT_DEFS:
    server._ext_definitions()
ext = sorted(server._EXT_DEFS.keys())
results["tool_inventory"] = {"core": len(core), "ext": len(ext), "total": len(core) + len(ext)}

print(json.dumps(results, ensure_ascii=False, indent=2))
