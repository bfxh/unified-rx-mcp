# -*- coding: utf-8 -*-
"""registry.py —— 注册=声明：name → (handler, description, group, schema)

设计原则：
- 工具注册即声明（装饰器），零反射
- 统一分发入口 call()，错误隔离（单工具异常 → {ok:false}，不拖垮协议层）
- group 用于工具面收敛统计与文档生成
- 2026-08-25: call() 自动打点（duration_ms 写入 stats.jsonl，供 cost_report/usage 统计）
"""
import json
import os
import time

_TOOLS = {}


def tool(name, description="", group="misc", schema=None):
    """工具注册装饰器。schema 为 JSON Schema（inputSchema），缺省空对象。"""
    def deco(fn):
        _TOOLS[name] = {
            "handler": fn,
            "description": description,
            "group": group,
            "schema": schema or {"type": "object", "properties": {}, "required": []},
        }
        return fn
    return deco


def list_tools():
    """MCP tools/list 输出：按注册顺序。"""
    return [
        {
            "name": n,
            "description": v["description"],
            "inputSchema": v["schema"],
            "_group": v["group"],
        }
        for n, v in _TOOLS.items()
    ]


def groups():
    """按 group 聚合工具名（收敛统计用）。"""
    g = {}
    for n, v in _TOOLS.items():
        g.setdefault(v["group"], []).append(n)
    return g


def _record_stats(tool_name, duration_ms):
    """工具调用打点（cost_report/usage_stats 的数据源）。"""
    try:
        home = os.path.join(os.path.expanduser("~"), ".unified-rx")
        os.makedirs(home, exist_ok=True)
        with open(os.path.join(home, "stats.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "tool": tool_name, "duration_ms": int(duration_ms),
                "ts": int(time.time()),
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


def call(name, args):
    """统一分发。args 为 dict。返回 {ok, result} 或 {ok:false, error}。
    自动打点：每次调用记录 tool + 耗时（不阻塞、不拖垮主流程）。"""
    if name not in _TOOLS:
        return {"ok": False, "error": f"未知工具: {name}"}
    t0 = time.time()
    try:
        result = _TOOLS[name]["handler"](**(args or {}))
        _record_stats(name, (time.time() - t0) * 1000)
        return {"ok": True, "result": result}
    except TypeError as e:
        _record_stats(name, (time.time() - t0) * 1000)
        return {"ok": False, "error": f"参数错误: {e}"}
    except Exception as e:
        _record_stats(name, (time.time() - t0) * 1000)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def tool_count():
    return len(_TOOLS)
