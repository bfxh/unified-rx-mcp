#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""stats — 动作/任务统计 MCP（每秒每任务做了什么 + token 自动统计）。

工具：
- stats_record   记录一次动作 {task, tool, action, duration_ms, tokens_in, tokens_out, model}
- stats_summary  汇总：总动作/每任务/每秒吞吐/总 token/估算成本
- stats_status   状态：数据文件位置、记录数、最早/最新时间
- stats_clear    清空统计

存储：~/.unified-rx/stats.json（追加式，单用户本地）。
token 成本按内置单价表估算（可传 model 覆盖；未知模型按 0 不计成本）。
"""

import json
import os
import sys
import time
from pathlib import Path

from mcp.server import NotificationOptions, Server
from mcp.server.models import InitializationOptions
import mcp.server.stdio
import mcp.types as types

STATE_DIR = Path(os.environ.get("UNIFIED_RX_STATE", Path.home() / ".unified-rx"))
STATE_FILE = STATE_DIR / "stats.json"

# 单价表（USD/1K tokens；输入/输出）——可扩展，未知模型成本按 0
_MODEL_PRICES = {
    "deepseek-chat": (0.27, 1.10),
    "deepseek-reasoner": (0.55, 2.19),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-opus-4-6": (5.00, 25.00),
}

_MAX_RECORDS = 100000  # 防膨胀上限（超出丢弃最旧 10%）

_TC = lambda text: types.TextContent(type="text", text=str(text))  # noqa: E731


def _load() -> list[dict]:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save(records: list[dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # 原子写：临时文件 + rename（高并发下防半写/写坏 JSON）；
    # tmp 名含 pid——多进程共享 stats 文件时防 tmp 竞态（security review LOW）
    tmp = STATE_FILE.with_name(f"{STATE_FILE.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(records, ensure_ascii=False), encoding="utf-8")
    tmp.replace(STATE_FILE)


def _truncate(records: list[dict]) -> list[dict]:
    if len(records) > _MAX_RECORDS:
        return records[len(records) // 10:]
    return records


def _record(args: dict) -> str:
    rec = {
        "ts": time.time(),
        "task": str(args.get("task", "default")),
        "tool": str(args.get("tool", "")),
        "action": str(args.get("action", "")),
        "duration_ms": float(args.get("duration_ms", 0)),
        "tokens_in": int(args.get("tokens_in", 0)),
        "tokens_out": int(args.get("tokens_out", 0)),
        "model": str(args.get("model", "deepseek-chat")),
        "meta": args.get("meta", ""),
    }
    records = _truncate(_load() + [rec])
    _save(records)
    return json.dumps({"ok": True, "recorded": len(records)}, ensure_ascii=False)


def _summary(args: dict) -> str:
    records = _load()
    if not records:
        return json.dumps({"ok": True, "total": 0, "note": "无记录"}, ensure_ascii=False)

    total_in = sum(r.get("tokens_in", 0) for r in records)
    total_out = sum(r.get("tokens_out", 0) for r in records)
    n = len(records)
    span = max(r["ts"] for r in records) - min(r["ts"] for r in records)
    tps = n / span if span > 0 else 0.0

    # 每任务聚合（会话维度：task = 会话；含时长/起止）
    by_task: dict[str, dict] = {}
    for r in records:
        t = by_task.setdefault(r["task"], {"count": 0, "ms": 0.0, "tokens_in": 0, "tokens_out": 0,
                                           "tools": set(), "first_ts": None, "last_ts": None})
        t["count"] += 1
        t["ms"] += r.get("duration_ms", 0)
        t["tokens_in"] += r.get("tokens_in", 0)
        t["tokens_out"] += r.get("tokens_out", 0)
        t["tools"].add(r["tool"])
        ts = r.get("ts", 0)
        t["first_ts"] = ts if t["first_ts"] is None else min(t["first_ts"], ts)
        t["last_ts"] = ts if t["last_ts"] is None else max(t["last_ts"], ts)
    tasks = [{
        "task": k,
        "actions": v["count"],
        "duration_ms_total": round(v["ms"], 1),
        "session_span_seconds": round(v["last_ts"] - v["first_ts"], 1) if v["first_ts"] and v["last_ts"] else 0.0,
        "tokens_in": v["tokens_in"],
        "tokens_out": v["tokens_out"],
        "tools": sorted(t for t in v["tools"] if t),
    } for k, v in sorted(by_task.items(), key=lambda kv: -kv[1]["count"])]

    # 成本估算（按记录 model 单价）
    cost = 0.0
    for r in records:
        price = _MODEL_PRICES.get(r.get("model", ""))
        if price:
            cost += r.get("tokens_in", 0) / 1000 * price[0] + r.get("tokens_out", 0) / 1000 * price[1]

    # IDE 增强 114：工具调用 TOP10（活跃度一眼可见——什么工具最常用）
    tool_counter: dict[str, int] = {}
    for r in records:
        tool_counter[str(r.get("tool", "?"))] = tool_counter.get(str(r.get("tool", "?")), 0) + 1
    top_tools = sorted(tool_counter.items(), key=lambda kv: -kv[1])[:10]

    return json.dumps({
        "ok": True,
        "total_actions": n,
        "span_seconds": round(span, 1),
        "actions_per_second": round(tps, 2),
        "tokens_in": total_in,
        "tokens_out": total_out,
        "tokens_total": total_in + total_out,
        "estimated_cost_usd": round(cost, 4),
        "avg_duration_ms": round(sum(r.get("duration_ms", 0) for r in records) / n, 1),
        "top_tools": top_tools,
        "tasks": tasks,
    }, ensure_ascii=False)


def _status(args: dict) -> str:
    records = _load()
    first = records[0]["ts"] if records else None
    last = records[-1]["ts"] if records else None
    return json.dumps({
        "ok": True,
        "file": str(STATE_FILE),
        "records": len(records),
        "first_ts": first,
        "last_ts": last,
        "model_prices": {k: list(v) for k, v in _MODEL_PRICES.items()},
    }, ensure_ascii=False)


def _clear(args: dict) -> str:
    _save([])
    return json.dumps({"ok": True, "cleared": True}, ensure_ascii=False)


_TOOLS = {
    "stats_record": (_record, {
        "type": "object",
        "properties": {
            "task": {"type": "string", "description": "任务名（汇总按此分组）"},
            "tool": {"type": "string", "description": "工具名（如 fs_read）"},
            "action": {"type": "string", "description": "动作描述"},
            "duration_ms": {"type": "number", "description": "耗时毫秒"},
            "tokens_in": {"type": "integer", "description": "输入 token 数"},
            "tokens_out": {"type": "integer", "description": "输出 token 数"},
            "model": {"type": "string", "description": "模型名（成本估算用，默认 deepseek-chat）"},
            "meta": {"type": "string", "description": "附加信息"},
        },
        "required": [],
    }, "记录一次动作（每任务/每秒统计的原始数据）"),
    "stats_summary": (_summary, {
        "type": "object", "properties": {}, "required": [],
    }, "汇总：总动作/每任务/每秒吞吐/总 token/估算成本"),
    "stats_status": (_status, {
        "type": "object", "properties": {}, "required": [],
    }, "状态：数据文件、记录数、时间范围、单价表"),
    "stats_clear": (_clear, {
        "type": "object", "properties": {}, "required": [],
    }, "清空全部统计"),
}


def _call(name: str, arguments: dict | None) -> str:
    """纯函数风格（与 pr-oracle/tautest 扩展一致：返回 str，由宿主包 TextContent）。

    注意：不要返回 mcp TextContent 列表——unified-rx 的 pure 扩展约定是 str。
    """
    args = arguments or {}
    try:
        fn = _TOOLS[name][0]
        return str(fn(args))
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


def main() -> None:
    server = Server("stats")

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=n, description=d, inputSchema=s)
            for n, (_, s, d) in _TOOLS.items()
        ]

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict | None) -> "list[types.TextContent]":
        return [types.TextContent(type="text", text=_call(name, arguments))]

    async def run():
        async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                InitializationOptions(
                    server_name="stats",
                    server_version="1.0.0",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={},
                    ),
                ),
            )

    import asyncio
    asyncio.run(run())


if __name__ == "__main__":
    main()
