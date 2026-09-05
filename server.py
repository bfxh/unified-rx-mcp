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
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import registry

# 导入 tools 包触发注册（tools/__init__.py 汇总所有域）
import tools  # noqa: F401

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "unified-rx-v2"
SERVER_VERSION = "2.14.0"

# 所有 stdout 写入统一加锁：后台线程完成工具调用时与主线程并发 _send，防止一行 JSON 被拆散
_SEND_LOCK = threading.Lock()

# S3-B3+S10：取消登记唯一事实源迁移至 registry（__main__/import 双世界陷阱见 registry 注释）
def cancel_flag(msg_id):
    """兼容出口：等价 registry.cancel_flag。"""
    return registry.cancel_flag(msg_id)


def _notify(method, params):
    """S3-B2 服务器 → 客户端通知（logging/progress/cancelled 语义复用同一出口）。"""
    _send({"jsonrpc": "2.0", "method": method, "params": params})


def log_msg(level, message, logger="unified-rx"):
    """S3-B2 MCP logging 能力：协议内通知而非 stderr（宿主日志面板可见）。"""
    if level not in ("debug", "info", "warning", "error"):
        level = "info"
    try:
        _notify("notifications/message", {"level": level, "logger": logger, "data": str(message)})
    except Exception:
        pass  # 通知失败绝不拖垮主流程


_MAX_LINE_BYTES = 64 * 1024 * 1024   # S62：单条协议消息上限（宿主异常/敌意输入不撑内存）


def _read_line():
    """读一行（newline-delimited JSON）。EOF 返回 None；触顶未到行尾则丢弃整行。"""
    buf = sys.stdin.buffer
    line = buf.readline(_MAX_LINE_BYTES + 1)
    if not line:
        return None
    if not line.endswith(b"\n") and len(line) > _MAX_LINE_BYTES:
        # 未到行尾就触顶 → 丢弃到行尾，防残留污染下一条消息
        while True:
            rest = buf.readline(_MAX_LINE_BYTES)
            if not rest or rest.endswith(b"\n"):
                break
        return ""
    return line.decode("utf-8", errors="replace").strip()


def _send(obj):
    """写一行 JSON 到 stdout 并 flush。"""
    with _SEND_LOCK:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def _handle(msg):
    """处理单条消息，返回响应（或 None 表示无需响应）。"""
    # P3 修复：协议版本校验（非 2.0 拒绝，防协议混淆/畸形客户端）
    if msg.get("jsonrpc") not in (None, "2.0"):
        return {"jsonrpc": "2.0", "id": msg.get("id"),
                "error": {"code": -32600, "message": "Invalid Request: jsonrpc must be 2.0"}}
    method = msg.get("method")
    msg_id = msg.get("id")
    # S78 加固①：params 非对象一律按缺省处理（fuzz 实锤 notifications/cancelled
    # 的 list params 会在 (params or {}).get 上炸掉主循环）
    raw_params = msg.get("params")
    params = raw_params if isinstance(raw_params, dict) else {}

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL_VERSION,
                # S3: capabilities 声明 logging；tools.listChanged 供宿主订阅工具面变化
                "capabilities": {"tools": {"listChanged": True}, "logging": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        }
    if method == "notifications/initialized":
        return None
    if method == "notifications/cancelled":
        # S3-B3/S10：客户端取消请求 → 置位 registry 层旗标，长任务（local_run 等）轮询退出
        rid = (params or {}).get("requestId")
        registry.set_cancelled(rid)
        return None
    if method == "logging/setLevel":
        # S3-B2：级别协商（实现为全部放行，过滤留给 log_msg 调用方）
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
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
        # __authorized 授权由 registry.call 的 requires_auth 统一强制（UPGRADE-A1）
        # S10：绑定请求上下文——工具内部（local_run 取消轮询）可查 cancel_flag(request_id)
        registry.set_request_context(msg_id)
        # S12：progressToken 透传（MCP 规范 notifications/progress）
        ptoken = (params.get("_meta") or {}).get("progressToken")
        registry.set_progress_context(ptoken)
        try:
            result = registry.call(name, args)
        finally:
            registry.clear_request_context()
        if result.get("ok"):
            content = [{"type": "text", "text": json.dumps(result["result"], ensure_ascii=False)}]
        else:
            # S72：附 error_detail（堆栈尾部）——单行 error 只有类型+消息，
            # 模型修 bug 时看不到出错位置，只能瞎猜重试
            text = f"ERROR: {result.get('error')}"
            detail = result.get("error_detail")
            if detail:
                text += f"\nDETAIL: {detail}"
            content = [{"type": "text", "text": text}]
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {"content": content, "isError": not result.get("ok")},
        }
    # S78 加固②：通知（无 id）永不回包——未知通知回 UNKNOWN_METHOD 会以 id:null
    # 污染宿主的响应配对（fuzz 电池实锤，与 Rust 协议层纪律对齐）
    if "id" not in msg:
        return None
    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "result": {"content": [{"type": "text", "text": f"UNKNOWN_METHOD {method}"}], "isError": True},
    }


def selftest():
    """注册表自检：工具数 + 每个工具 schema 合法 + 抽样调用。"""
    # fail-closed 下自检自身也会被拦：未显式配沙盒时临时放开（仅本进程）
    # S43 安全修复：缺省不再 "*" 全开——忘配沙盒 = fail-closed 拒绝
    # （S0 设计本意；可信宿主须显式 UNIFIED_RX_SANDBOX="*" 或列白名单）
    os.environ.setdefault("UNIFIED_RX_SANDBOX", "__URX_UNSET__")
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
    # 通知 stdout 由 main 持有；log_msg 从任意线程安全发送
    registry.set_notifier(lambda level, msg: log_msg(level, msg))

    def _send_progress(token, progress, message=None):
        p = {"progressToken": token, "progress": progress}
        if message:
            p["message"] = str(message)[:120]
        _send({"jsonrpc": "2.0", "method": "notifications/progress", "params": p})

    registry.set_progress_sender(_send_progress)
    # 协议主循环：tools/call 交给线程池执行，主循环继续读 stdin。
    # 慢工具（local_run/fs_list/engine_query）不再阻塞 ping/keepalive，
    # 否则 Hermes 会判定服务器失联并重连，最终把 in-flight 调用掐成 300s 超时。
    executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rxmcp")
    while True:
        line = _read_line()
        if line is None:
            break
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            # S78 加固③：深嵌套触发 RecursionError 与畸形 JSON 同待遇——吞掉不崩
            continue
        # S78 加固④：顶层非对象（[]/123/"x"）不是合法消息，静默跳过
        # （fuzz 实锤 [] 会在下方 msg.get 上炸掉主循环）
        if not isinstance(msg, dict):
            continue
        if msg.get("method") == "tools/call" and "id" in msg:
            msg_id = msg.get("id")
            # S3-B3/S10：登记可取消旗标；完成/取消后清理（实现已迁至 registry）
            ev = registry.register_cancel(msg_id)

            def _done(fut, _id=msg_id):
                try:
                    resp = fut.result()
                except Exception as e:
                    resp = {"jsonrpc": "2.0", "id": _id,
                            "error": {"code": -32603, "message": str(e)}}
                if resp is not None:
                    _send(resp)
                registry.release_cancel(_id)

            executor.submit(_handle, msg).add_done_callback(_done)
            continue
        resp = _handle(msg)
        if resp is not None:
            _send(resp)


if __name__ == "__main__":
    # S69：开发目录自动驾驶——server 启动即后台自动体检全部项目 + 顺带打开
    # VS Code（去重窗口防多客户端弹窗风暴；UNIFIED_RX_AUTOPILOT_VSCODE=0 关闭）。
    # 只在 stdio 服务模式跑：测试直接 import server 不会触发。
    try:
        from tools.ide_autopilot import autopilot_run

        def _autopilot_boot():
            time.sleep(3.0)
            autopilot_run()

        threading.Thread(target=_autopilot_boot, daemon=True).start()
    except Exception:                                        # noqa: BLE001
        pass                                                  # 预热失败不影响服务
    main()
