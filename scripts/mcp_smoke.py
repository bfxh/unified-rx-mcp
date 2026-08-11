#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""unified-rx MCP stdio 冒烟测试：initialize + tools/list + tools/call。

mcp python SDK >=1.9 的 stdio 传输是 newline-delimited JSON（每行一个
JSON-RPC 消息，非 LSP 的 Content-Length 帧）。验证真实协议层不再崩溃。
"""
import json
import subprocess
import sys

SERVER = r"C:\Users\lbx13\AppData\Roaming\reasonix\global-workspace\mcp-servers\unified-rx\server.py"
PY = sys.executable


def main() -> int:
    proc = subprocess.Popen(
        [PY, SERVER],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        def send(obj: dict):
            proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
            proc.stdin.flush()

        def recv() -> dict:
            line = proc.stdout.readline()
            if not line:
                err = proc.stderr.read().decode("utf-8", "replace")
                raise RuntimeError(f"server EOF, stderr: {err[:500]}")
            return json.loads(line.decode("utf-8", "replace"))

        # 1) initialize
        send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "rx-smoke", "version": "0.1"},
            },
        })
        resp = recv()
        assert resp.get("id") == 1, f"initialize id mismatch: {resp}"
        assert resp["result"].get("serverInfo", {}).get("name") == "unified-rx", resp
        print("[ok] initialize ->", resp["result"]["serverInfo"])

        # 2) initialized notification
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        # 3) tools/list —— 之前在这里崩溃（asyncio.run in event loop）
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        resp = recv()
        assert resp.get("id") == 2, f"tools/list id mismatch: {resp}"
        tools = resp["result"]["tools"]
        names = {t["name"] for t in tools}
        print(f"[ok] tools/list -> {len(tools)} tools")
        assert len(tools) >= 48, f"工具数不足: {len(tools)}"
        for expect in ("math_ops", "vuln_scan", "lesson_recall_lse", "cae_lsp_query", "pr_oracle_map_pr"):
            assert expect in names, f"缺工具 {expect}"

        # 4) tools/call math_ops（组合工具真实调用）
        send({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "math_ops", "arguments": {"action": "add", "a": 2, "b": 3}},
        })
        resp = recv()
        assert resp.get("id") == 3, f"tools/call id mismatch: {resp}"
        content = resp["result"]["content"]
        assert any("5" in c.get("text", "") for c in content), f"math_ops 结果不对: {content}"
        print("[ok] tools/call math_ops add ->", [c.get("text") for c in content][:1])

        # 5) tools/call vuln_scan（统一入口冒烟）
        send({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "vuln_scan", "arguments": {"path": __file__, "max_files": 5}},
        })
        resp = recv()
        assert resp.get("id") == 4, f"vuln_scan id mismatch: {resp}"
        print("[ok] tools/call vuln_scan ->", resp["result"]["content"][0]["text"][:60])

        print("SMOKE PASS")
        return 0
    finally:
        proc.kill()


if __name__ == "__main__":
    sys.exit(main())
