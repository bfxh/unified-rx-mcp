# -*- coding: utf-8 -*-
"""server.py —— MCP stdio 协议薄层（纯 stdlib 零依赖，<250 行）

协议：newline-delimited JSON-RPC 2.0（现代 MCP stdio 标准）
方法：initialize / notifications/initialized / tools/list / tools/call / ping
设计：
- 不依赖 mcp SDK（旧版 7462 行 + mcp 依赖的根源）——stdlib 手写协议层
- 注册表分发（registry.call），错误隔离
- 常驻：stdio 循环读行，EOF 退出
- --selftest：不进入协议循环，跑注册表自检

运行：python server.py
"""
import sys
import json
import os

import registry

# 导入 tools 包触发注册（tools/__init__.py 汇总所有域）
import tools  # noqa: F401

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "unified-rx-v2"
SERVER_VERSION = "2.0.0"


def _read_line():
    """读一行（newline-delimited JSON）。EOF 返回 None。"""
    line = sys.stdin.readline()
    if not line:
        return None
    return line.strip()


def _send(obj):
    """写一行 JSON 到 stdout 并 flush。"""
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _handle(msg):
    """处理单条消息，返回响应（或 None 表示无需响应）。"""
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        tools_list = [
            {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
            for t in registry.list_tools()
        ]
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": tools_list}}
    if method == "tools/call":
        name = params.get("name", "")
        args = params.get("arguments") or {}
        # __authorized 支持：透传授权字段（fs_write 等写工具用）
        if "__authorized" in args:
            authorized = args.pop("__authorized")
            args["__authorized"] = authorized
        result = registry.call(name, args)
        if result.get("ok"):
            content = [{"type": "text", "text": json.dumps(result["result"], ensure_ascii=False)}]
        else:
            content = [{"type": "text", "text": f"ERROR: {result.get('error')}"}]
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"content": content, "isError": not result.get("ok")},
        }
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": {"content": [{"type": "text", "text": f"UNKNOWN_METHOD {method}"}], "isError": True},
    }


def selftest():
    """注册表自检：工具数 + 每个工具 schema 合法 + 抽样调用。"""
    n = registry.tool_count()
    print(f"SELFTEST tools={n}")
    groups = registry.groups()
    print(f"GROUPS {len(groups)}: " + ", ".join(f"{k}({len(v)})" for k, v in sorted(groups.items())))
    # 抽样调用 fs_stat
    r = registry.call("fs_stat", {"path": __file__})
    print(f"FS_STAT {r}")
    bad = [t for t in registry.list_tools() if not t["name"] or not isinstance(t["inputSchema"], dict)]
    print(f"SCHEMA_BAD {len(bad)}")
    return 0 if (n > 0 and not bad and r.get("ok")) else 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    # 协议主循环
    while True:
        line = _read_line()
        if line is None:
            break
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = _handle(msg)
        if resp is not None:
            _send(resp)


if __name__ == "__main__":
    main()
