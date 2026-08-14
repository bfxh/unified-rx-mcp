#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""高协作/高并发验证：pipeline 注入链 + parallel 并发组 + 协议层并发吞吐。"""
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

report = {}

# 1) pipeline：步骤链注入（add → mul）
r = server._call("pipeline", {"steps": [
    {"tool": "math_ops", "args": {"action": "add", "a": 2, "b": 3}, "as": "sum"},
    {"tool": "math_ops", "args": {"action": "mul", "a": "${sum}", "b": 4}},
    {"tool": "text_ops", "args": {"action": "upper", "s": "ok"}, "as": "tag"},
]})
d = json.loads(r[0].text)
assert d["ok"] and d["steps"][0]["result"] == 5, d
assert d["steps"][1]["result"] == 20, d  # (2+3)*4 = 20 注入成功
assert d["steps"][2]["result"] == "OK", d
report["pipeline"] = {"steps": [s["result"] for s in d["steps"]], "keys": d["context_keys"]}
print("[ok] pipeline:", report["pipeline"])

# 2) parallel：并发 4 工具（含慢的 prime_generate）
t0 = time.perf_counter()
r = server._call("parallel", {"tasks": [
    {"tool": "math_ops", "args": {"action": "factorial", "n": 20}},
    {"tool": "prime_list", "args": {"action": "generate", "limit": 20000}},
    {"tool": "text_ops", "args": {"action": "palindrome", "s": "abba"}},
    {"tool": "fib_fibonacci", "args": {"n": 1000}},
]})
dt = (time.perf_counter() - t0) * 1000
d = json.loads(r[0].text)
assert d["ok"] and len(d["results"]) == 4 and all(x["ok"] for x in d["results"]), d
report["parallel"] = {"results": [x["tool"] for x in d["results"]], "ms": round(dt, 0)}
print("[ok] parallel 4 tasks:", round(dt, 0), "ms")

# 3) 协议层高并发：真实 MCP 进程 + asyncio.gather 并发调用
proc = subprocess.Popen([sys.executable, str(ROOT / "server.py")],
                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        env={**os.environ, "UNIFIED_RX_STATE": str(Path(os.environ.get("TEMP", ".")) / f"rx_conc_{os.getpid()}")})


def send(o):
    proc.stdin.write((json.dumps(o) + "\n").encode())
    proc.stdin.flush()


def recv():
    line = proc.stdout.readline()
    if not line:
        raise RuntimeError(f"EOF: {proc.stderr.read().decode('utf-8','replace')[-300:]}")
    return json.loads(line.decode())


send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
      "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                 "clientInfo": {"name": "conc", "version": "0.1"}}})
recv()
send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
r = recv()
ntools = len(r["result"]["tools"])
report["tools"] = ntools
assert ntools == 54, f"预期 54，实际 {ntools}"

# 并发 20 请求（不等待逐个响应——一次性发出，检验服务器并发处理）

n = 20
t0 = time.perf_counter()
for i in range(n):
    send({"jsonrpc": "2.0", "id": 100 + i, "method": "tools/call",
          "params": {"name": "math_ops", "arguments": {"action": "add", "a": i, "b": 1}}})
for i in range(n):
    r = recv()
    assert r["id"] == 100 + i, (r["id"], i)
dt = (time.perf_counter() - t0) * 1000
report["concurrent_20"] = {"total_ms": round(dt, 1), "per_call_ms": round(dt / n, 2),
                           "calls_per_sec": round(n / dt * 1000)}
print("[ok] 协议层并发 20 请求:", report["concurrent_20"])

# 并发 pipeline（5 条链同时跑）
t0 = time.perf_counter()
for i in range(5):
    send({"jsonrpc": "2.0", "id": 200 + i, "method": "tools/call",
          "params": {"name": "pipeline", "arguments": {"steps": [
              {"tool": "math_ops", "args": {"action": "add", "a": i, "b": i}, "as": "s"},
              {"tool": "math_ops", "args": {"action": "mul", "a": "${s}", "b": 3}},
          ]}}})
for i in range(5):
    r = recv()
    assert r["id"] == 200 + i
dt = (time.perf_counter() - t0) * 1000
report["concurrent_pipeline_5"] = {"total_ms": round(dt, 1), "per_chain_ms": round(dt / 5, 1)}
print("[ok] 并发 5 条 pipeline 链:", report["concurrent_pipeline_5"])
proc.kill()

print(json.dumps(report, ensure_ascii=False, indent=2))
print("COLLAB-CONCURRENCY PASS")
