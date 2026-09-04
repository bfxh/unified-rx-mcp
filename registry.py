# -*- coding: utf-8 -*-
"""registry.py —— 注册=声明：name → (handler, description, group, schema)

设计原则：
- 工具注册即声明（装饰器），零反射
- 统一分发入口 call()，错误隔离（单工具异常 → {ok:false}，不拖垮协议层）
- group 用于工具面收敛统计与文档生成
  - 2026-08-25: call() 自动打点（duration_ms 写入 stats.jsonl，供 usage_stats 统计；S15 起 cost_report 已并入 usage_stats）
"""
import inspect
import json
import os
import threading
import time
import traceback

# UPGRADE-C1：出口信噪上限。列表结果默认最多保留 200 项，超出截断并附分页游标。
# S10：字符串值同样设上限——大块文本（code_search 行内容等）不再裸奔打爆 token。
MAX_RESULT_ITEMS = 200
_MAX_BYTES = 50 * 1024
MAX_STR_CHARS = 64 * 1024
# S62：输入侧对偶上限（_clamp 只管出口，入口此前裸奔）
_MAX_STR_ARG = 2 * 1024 * 1024       # 单个字符串参数 ≤2M 字符
_MAX_LIST_ARG = 10000                # 单个列表参数 ≤1 万项

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


def tool(name, description="", group="misc", schema=None, requires_auth=False,
         manual_gate=False):
    """工具注册装饰器。schema 为 JSON Schema（inputSchema），缺省空对象。

    requires_auth=True：写/执行类工具。call() 统一强制 args["__authorized"] is True，
    一层防线——工具函数不再各自手写 if 检查，新增工具漏配在 selftest 即暴露。
    manual_gate=True：单工具混合读写时（读开放 + 写动作在 handler 内自查
    __authorized，如 ide_lsp 的 rename_apply），向 auth_gate_sweep（S77）显式声明
    "门在 handler 里"——避免把合法手动门误报成假门；声明必须紧挨实现，防漂移。
    """
    def deco(fn):
        try:
            params = frozenset(inspect.signature(fn).parameters)
        except (TypeError, ValueError):
            params = frozenset()
        _TOOLS[name] = {
            "handler": fn,
            "description": description,
            "group": group,
            "schema": schema or {"type": "object", "properties": {}, "required": []},
            "requires_auth": requires_auth,
            "manual_gate": manual_gate,
            "params": params,
        }
        return fn
    return deco


def list_tools():
    """MCP tools/list 输出：按注册顺序。

    S72b：requires_auth 工具统一注入 __authorized 声明——部分工具（fs_write）
    手工 schema 已带，local_run/ide_edit_multi 等只写在 description 里，MCP
    宿主看不到参数就永远不会传 → 写/执行类工具在协议模式下被永久拒绝。
    """
    out = []
    for n, v in _TOOLS.items():
        schema = v["schema"]
        if v.get("requires_auth"):
            props = dict(schema.get("properties") or {})
            req = list(schema.get("required") or [])
            if "__authorized" not in props:
                props["__authorized"] = {
                    "type": "boolean",
                    "description": "写/执行操作授权确认：必须显式传 true（防 AI 幻觉乱写）",
                }
            if "__authorized" not in req:
                req.append("__authorized")
            schema = {**schema, "properties": props, "required": req}
        out.append({"name": n, "description": v["description"],
                    "inputSchema": schema, "_group": v["group"]})
    return out


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


def _clamp_str(v):
    """S70：保头 + 保尾——测试摘要（test result/summary）与 panic 消息
    都在输出尾部，纯保头会把最关键的结尾截丢。"""
    total = len(v)
    head = MAX_STR_CHARS - 16 * 1024
    tail = 16 * 1024
    cut = total - head - tail
    return v[:head] + f"\n…[truncated {cut} chars / {total} total]…\n" + v[-tail:]


# S72：嵌套层递归深度上限（再深不再扫，防极深结构的递归开销）
_CLAMP_MAX_DEPTH = 3


def _clamp_nested(value, depth):
    """S72：嵌套层钳制。旧版只扫 result 顶层且 break，嵌套在子 dict 里的
    超大 list/str 完全漏网。嵌套层不引入 cursor 契约（避免与顶层分页语义
    混淆），list 超限截断后打 sibling 标记：<k>_total_items / <k>_truncated，
    消费方应缩小查询条件而非翻页。"""
    if depth > _CLAMP_MAX_DEPTH:
        return value
    if isinstance(value, str) and len(value) > MAX_STR_CHARS:
        return _clamp_str(value)
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if isinstance(v, list) and len(v) > MAX_RESULT_ITEMS:
                out[k] = [_clamp_nested(x, depth + 1) for x in v[:MAX_RESULT_ITEMS]]
                out[f"{k}_total_items"] = len(v)
                out[f"{k}_truncated"] = True
            else:
                out[k] = _clamp_nested(v, depth + 1)
        return out
    return value


def _clamp(result, args):
    """出口裁剪（UPGRADE-C1）：顶层 list 超限 → 截断 + next_cursor 分页游标。

    args.cursor 指定起点（客户端续读用）。单值结果不裁剪——fs_read 已有自己的
    1MB 上限。S10 契约保持：顶层 list 走 cursor 分页、末页不带 truncated；
    S70：字符串保头保尾；S72：改为全字段独立处理 + 嵌套限深递归
    （旧版单字段 break，第一个超限字段之后的大结果全部漏网）。
    """
    if not isinstance(result, dict):
        return result
    cursor = 0
    try:
        cursor = int((args or {}).get("cursor") or 0)
    except (TypeError, ValueError):
        cursor = 0
    out = {}
    for k, v in result.items():
        if isinstance(v, list) and len(v) > MAX_RESULT_ITEMS:
            # 顶层 list 保持 S10 分页契约：cursor 续读；truncated 仅当还有下一页
            start = max(0, min(cursor, len(v)))
            page = v[start:start + MAX_RESULT_ITEMS]
            nxt = start + MAX_RESULT_ITEMS
            out[k] = page
            out["total_items"] = len(v)
            if nxt < len(v):
                out["truncated"] = True
                out["next_cursor"] = nxt
            continue
        if isinstance(v, str) and len(v) > MAX_STR_CHARS:
            out[k] = _clamp_str(v)
            continue
        out[k] = _clamp_nested(v, 1)
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
    # S61：__authorized 不是工具签名一部分时剥掉——授权确认是传输层语义，
    # 调用方可以放心对任意工具统一附带，不撑爆 handler 签名
    if "__authorized" in a and "__authorized" not in entry.get("params", frozenset()):
        a.pop("__authorized")
    # S62 入口尺寸门：超大字符串/列表参数在这里死掉，不再穿透进工具内部
    for k, v in a.items():
        if isinstance(v, str) and len(v) > _MAX_STR_ARG:
            _record_stats(name, 0.0)
            return {"ok": False,
                    "error": f"SchemaError: 参数 {k} 过大（>{_MAX_STR_ARG // (1024 * 1024)}MB 字符）"}
        if isinstance(v, list) and len(v) > _MAX_LIST_ARG:
            _record_stats(name, 0.0)
            return {"ok": False,
                    "error": f"SchemaError: 参数 {k} 列表过长（>{_MAX_LIST_ARG} 项）"}
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
        # S61：砍掉 len<=2 魔数——{"error","applied","errors"} 三键错误形状
        # 曾穿透此检查（ok:true 藏错误，编辑 0 应用看起来像成功）
        if isinstance(result, dict) and isinstance(result.get("error"), str):
            _record_stats(name, (time.time() - t0) * 1000)
            return {"ok": False, "error": result["error"], "result": result}
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
        # S72：附堆栈尾部（异常行 + 最近 3 帧）——单行 error 没有出错位置，
        # 模型修 bug 只能瞎猜重试；traceback 可能巨大，钳到 1000 字符
        tb_lines = traceback.format_exc().strip().splitlines()
        detail = "\n".join(tb_lines[-4:])[:1000]
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "error_detail": detail}


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
