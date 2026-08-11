#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""stats MCP 冒烟：initialize + tools/list + record/summary/status/clear。"""
import json
import os
import subprocess
import sys
from pathlib import Path

SERVER = str(Path(__file__).resolve().parent / "server.py")
# 隔离状态文件，不碰生产 ~/.unified-rx/stats.json
TEST_STATE = Path(os.environ.get("TEMP", ".")) / f"rx_stats_test_{os.getpid()}.json"
os.environ["UNIFIED_RX_STATE"] = str(TEST_STATE.parent)
STATE_FILE = TEST_STATE.parent / "stats.json"
try:
    STATE_FILE.unlink()
except OSError:
    pass


def main() -> int:
    proc = subprocess.Popen(
        [sys.executable, SERVER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env={**os.environ, "UNIFIED_RX_STATE": str(TEST_STATE.parent)},
    )
    try:
        def send(obj: dict):
            proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            proc.stdin.flush()

        def recv() -> dict:
            line = proc.stdout.readline()
            if not line:
                raise RuntimeError(f"EOF: {proc.stderr.read().decode('utf-8', 'replace')[:400]}")
            return json.loads(line.decode("utf-8", "replace"))

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "stats-smoke", "version": "0.1"}}})
        r = recv()
        assert r["result"]["serverInfo"]["name"] == "stats", r
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        r = recv()
        names = {t["name"] for t in r["result"]["tools"]}
        assert names == {"stats_record", "stats_summary", "stats_status", "stats_clear"}, names
        print("[ok] tools/list:", sorted(names))

        def call(tool: str, args: dict, rid: int) -> dict:
            send({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                  "params": {"name": tool, "arguments": args}})
            r = recv()
            assert r.get("id") == rid, r
            return json.loads(r["result"]["content"][0]["text"])

        # 记录 3 条（2 个任务）
        for i, (task, tin, tout, ms) in enumerate([
            ("t1", 1000, 500, 120.0), ("t1", 2000, 800, 80.0), ("t2", 500, 200, 40.0)]):
            r = call("stats_record", {"task": task, "tool": "fs_read", "action": f"read {i}",
                                      "tokens_in": tin, "tokens_out": tout, "duration_ms": ms}, 10 + i)
            assert r["ok"], r
        print("[ok] stats_record ×3")

        r = call("stats_summary", {}, 20)
        assert r["total_actions"] == 3, r
        assert r["tokens_total"] == 5000, r
        assert r["estimated_cost_usd"] > 0, r
        assert r["actions_per_second"] > 0, r
        assert len(r["tasks"]) == 2, r
        print("[ok] stats_summary:", {k: r[k] for k in ("total_actions", "tokens_total", "estimated_cost_usd", "actions_per_second")})

        r = call("stats_status", {}, 21)
        assert r["records"] == 3, r
        print("[ok] stats_status records=3")

        r = call("stats_clear", {}, 22)
        assert r["cleared"], r
        r = call("stats_summary", {}, 23)
        assert r["total"] == 0, r
        print("[ok] stats_clear + summary empty")

        print("STATS SMOKE PASS")
        return 0
    finally:
        proc.kill()
        try:
            STATE_FILE.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
