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

# UPGRADE-C1：出口信噪上限。列表结果默认最多保留 200 项，超出截断并附分页游标。
MAX_RESULT_ITEMS = 200
_MAX_BYTES = 50 * 1024

_TOOLS = {}


def tool(name, description="", group="misc", schema=None, requires_auth=False):
    """工具注册装饰器。schema 为 JSON Schema（inputSchema），缺省空对象。

    requires_auth=True：写/执行类工具。call() 统一强制 args["__authorized"] is True，
    一层防线——工具函数不再各自手写 if 检查，新增工具漏配在 selftest 即暴露。
    """
    def deco(fn):
        _TOOLS[name] = {
            "handler": fn,
            "description": description,
            "group": group,
            "schema": schema or {"type": "object", "properties": {}, "required": []},
            "requires_auth": requires_auth,
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


def _clamp(result, args):
    """出口裁剪（UPGRADE-C1）：列表超限 → 截断 + next_cursor 分页游标。

    只裁剪 result 内的 list 值；args.cursor 指定起点（客户端续读用）。
    单值结果不裁剪——fs_read 已有自己的 1MB 上限。
    """
    if not isinstance(result, dict):
        return result
    cursor = 0
    try:
        cursor = int((args or {}).get("cursor") or 0)
    except (TypeError, ValueError):
        cursor = 0
    out = dict(result)
    for k, v in result.items():
        if isinstance(v, list) and len(v) > MAX_RESULT_ITEMS:
            start = max(0, min(cursor, len(v)))
            page = v[start:start + MAX_RESULT_ITEMS]
            nxt = start + MAX_RESULT_ITEMS
            out[k] = page
            out["total_items"] = len(v)
            # truncated 契约：仅当【还有下一页】为 True；末页不带该字段（消费方以 next_cursor 为准）
            if nxt < len(v):
                out["truncated"] = True
                out["next_cursor"] = nxt
            break  # 单次只对一个主列表字段分页，防多重截断语义混乱
    return out


def call(name, args):
    """统一分发。args 为 dict。返回 {ok, result} 或 {ok:false, error}。
    自动打点：每次调用记录 tool + 耗时（不阻塞、不拖垮主流程）。
    requires_auth 工具统一在此强制 __authorized is True（声明式授权）。"""
    if name not in _TOOLS:
        return {"ok": False, "error": f"未知工具: {name}"}
    entry = _TOOLS[name]
    a = dict(args or {})
    if entry.get("requires_auth") and a.get("__authorized") is not True:
        return {"ok": False, "error": "PermissionError: 写/执行操作需要授权：参数加 __authorized: true 确认后重试"}
    a.pop("cursor", None)  # 传输层分页参数，不是工具签名的一部分
    cursor_arg = (args or {}).get("cursor")  # 分页起点先取出（a 已剥除）
    t0 = time.time()
    try:
        result = entry["handler"](**a)
        result = _clamp(result, {"cursor": cursor_arg})
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
