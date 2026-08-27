# -*- coding: utf-8 -*-
"""registry.py —— 注册=声明：name → (handler, description, group, schema)

设计原则：
- 工具注册即声明（装饰器），零反射
- 统一分发入口 call()，错误隔离（单工具异常 → {ok:false}，不拖垮协议层）
- group 用于工具面收敛统计与文档生成
  - 2026-08-25: call() 自动打点（duration_ms 写入 stats.jsonl，供 usage_stats 统计；S15 起 cost_report 已并入 usage_stats）
"""
import json
import os
import threading
import time

# UPGRADE-C1：出口信噪上限。列表结果默认最多保留 200 项，超出截断并附分页游标。
# S10：字符串值同样设上限——大块文本（code_search 行内容等）不再裸奔打爆 token。
MAX_RESULT_ITEMS = 200
_MAX_BYTES = 50 * 1024
MAX_STR_CHARS = 64 * 1024

# S3-B2：logging 通知出口（server.main 注入；直跑 pytest 时为 None → 静默跳过）
_NOTIFIER = None


def set_notifier(fn):
    """注入 logging 出口（server 主循环调用）。fn(level, message)。"""
    global _NOTIFIER
    _NOTIFIER = fn


def notify(level, message):
    """工具内部发协议日志（本地_run 后台启动/引擎降级等）。永不抛错。"""
    fn = _NOTIFIER
    if fn is not None:
        try:
            fn(level, message)
        except Exception:
            pass


_TOOLS = {}

# ---- S10 请求上下文：tools/call 分发线程 ↔ 工具内部（取消轮询等）----
_REQ_LOCAL = threading.local()


def set_request_context(msg_id):
    """server 分发层在调用 registry.call 前绑定请求 id（线程本地）。"""
    import sys
    _REQ_LOCAL.msg_id = msg_id
    if os.environ.get("URX_CTX_DEBUG"):
        print(f"[ctx] SET {msg_id} tid={threading.get_ident()} rl={id(_REQ_LOCAL)}",
              file=sys.stderr, flush=True)


def clear_request_context():
    try:
        del _REQ_LOCAL.msg_id
    except AttributeError:
        pass
    try:
        del _REQ_LOCAL.progress_token
    except AttributeError:
        pass


def set_progress_context(token):
    """带 progressToken 的 tools/call 绑定（S12：local_run 等长任务心跳）。"""
    _REQ_LOCAL.progress_token = token


# 进度发送器由 server 注入（stdout 归属协议层）
_PROGRESS_SENDER = [None]


def set_progress_sender(fn):
    _PROGRESS_SENDER[0] = fn


def notify_progress(progress, message=None):
    """工具内调用；无 token/无 sender 时静默 no-op——直调测试不受影响。"""
    tok = getattr(_REQ_LOCAL, "progress_token", None)
    fn = _PROGRESS_SENDER[0]
    if tok is not None and fn is not None:
        fn(tok, progress, message)


def current_request_id():
    v = getattr(_REQ_LOCAL, "msg_id", None)
    return v


# ---- S10 取消登记：唯一事实源在 registry ——
# 教训（端到端探针实测）：server 以 __main__ 运行时，工具内 `import server` 会
# 触发二次模块执行得到【新的空表】，登记永远查不到。任何跨层状态都收进本模块。
_CANCELS = {}
_CANCEL_LOCK = threading.Lock()


def register_cancel(msg_id):
    """tools/call 分发时登记该请求的取消 Event。"""
    ev = threading.Event()
    with _CANCEL_LOCK:
        _CANCELS[msg_id] = ev
    return ev


def set_cancelled(msg_id):
    """notifications/cancelled 到达：置位对应请求的旗标。"""
    with _CANCEL_LOCK:
        ev = _CANCELS.get(msg_id)
    if ev is not None:
        ev.set()
    return ev is not None


def release_cancel(msg_id):
    """响应发出后清理登记（无论完成或取消）。"""
    with _CANCEL_LOCK:
        _CANCELS.pop(msg_id, None)


def cancel_flag(msg_id):
    """工具内部轮询入口：返回该请求的取消 Event（未登记 → None）。"""
    with _CANCEL_LOCK:
        return _CANCELS.get(msg_id)


# ---- S10 取消登记结束 ----

# ---- S10 入口门禁：tools/list 已声明 JSON Schema，分发层从此真的校验它 ----
_TYPE_MAP = {"string": str, "integer": int, "number": (int, float),
             "boolean": bool, "object": dict, "array": list}


def _validate_schema(schema, a):
    """stdlib 最小校验：required 缺失 + 显式 type 违型。返回错误串或 None。

    设计边界：只对【显式声明】的字段做类型拒绝；未声明参数放行（传输层扩展位，
    如 __authorized/cursor 早已在别处处理）。bool 是 int 子类的 Python 坑在此挡住。
    """
    props = schema.get("properties")
    props = props if isinstance(props, dict) else {}
    for req in schema.get("required") or []:
        if req not in a or a[req] is None:
            return f"SchemaError: 缺少必填参数 {req}"
    for k, v in a.items():
        spec = props.get(k)
        if not isinstance(spec, dict) or v is None:
            continue
        t = spec.get("type")
        want = _TYPE_MAP.get(t)
        if want is None:
            continue
        if t == "integer":
            if isinstance(v, bool):
                return f"SchemaError: 参数 {k} 需要 integer, 得到 boolean"
            if isinstance(v, int):
                continue
            if isinstance(v, float) and float(v).is_integer():
                continue  # JSON 数传 2.0 == 整数，容忍
            return f"SchemaError: 参数 {k} 需要 integer, 得到 {type(v).__name__}"
        if t == "number":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                return f"SchemaError: 参数 {k} 需要 number, 得到 {type(v).__name__}"
            continue
        if not isinstance(v, want):
            return f"SchemaError: 参数 {k} 需要 {t}, 得到 {type(v).__name__}"
    return None


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
    """工具调用打点（usage_stats 的数据源）。"""
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
        # S10 扩展契约：单次只对一个超限字段做裁剪——列表走分页、字符串走截断
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
            break  # 单次只对一个主字段裁剪，防多重截断语义混乱
        if isinstance(v, str) and len(v) > MAX_STR_CHARS:
            total = len(v)
            out[k] = v[:MAX_STR_CHARS] + f"\n…[truncated {total - MAX_STR_CHARS} chars / {total} total]"
            break
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
    # S10-D0：入口 schema 门禁（错误类型在这里死掉，不再穿透进工具内部）
    verr = _validate_schema(entry["schema"], a)
    if verr:
        _record_stats(name, 0.0)
        return {"ok": False, "error": verr}
    t0 = time.time()
    try:
        result = entry["handler"](**a)
        # S7 错误语义统一：工具返回 {"error": ...}（成功形状里的错误）→ 转 ok:false，
        # 调用方只看 ok 一个字段即可，不必二次探测 result.error
        if isinstance(result, dict) and isinstance(result.get("error"), str) and len(result) <= 2:
            _record_stats(name, (time.time() - t0) * 1000)
            return {"ok": False, "error": result["error"]}
        # S10：工具【显式标记】ok:false（local_run 取消/超时等带详情的失败）→
        # 上浮顶层，调用方只看一个字段；详情留在 result 里不丢。
        if isinstance(result, dict) and result.get("ok") is False:
            rest = {k: v for k, v in result.items() if k != "ok"}
            msg = rest.get("error") or f"{name} 执行失败"
            _record_stats(name, (time.time() - t0) * 1000)
            return {"ok": False, "error": str(msg), "result": rest}
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


def call_with_context(name, args, request_id):
    """S10：显式绑定请求上下文后调用——测试与嵌入式宿主用，
    与 server 协议分发线程的行为等价（取消轮询可用）。"""
    set_request_context(request_id)
    try:
        return call(name, args)
    finally:
        clear_request_context()
