# -*- coding: utf-8 -*-
"""server.py —— MCP stdio 协议薄层（纯 stdlib 零依赖，<300 行；S91 起含自检对账）

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
import re
import subprocess
import time
import threading
from concurrent.futures import ThreadPoolExecutor

import registry

# 导入 tools 包触发注册（tools/__init__.py 汇总所有域）
import tools  # noqa: F401

PROTOCOL_VERSION = "2025-03-26"
SERVER_NAME = "unified-rx-v2"
SERVER_VERSION = "2.22.0"

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


def _latest_v_tag(base_dir):
    """目录内最新 v* tag（组件数值序，v2.9.0 < v2.10.0）；非仓库/无 tag/git 不可用 → None。"""
    try:
        cp = subprocess.run(["git", "tag", "--list", "v*"], capture_output=True,
                            timeout=10, cwd=base_dir, input=b"")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if cp.returncode != 0:
        return None
    tags = [t for t in cp.stdout.decode("utf-8", "replace").split() if t]
    if not tags:
        return None

    def _key(t):
        return [int(x) if x.isdigit() else -1 for x in t[1:].split(".")]

    return max(tags, key=_key)


def _selftest_version_tag(base_dir=None):
    """SERVER_VERSION ↔ 最新 git tag 对账（S91：84034eb 教训工具化——serverInfo
    曾在 S53-S71 停更十八轮，靠 84034eb 事后对齐）。打印口径：
    OK=与最新 tag 一致；NEXT=版本领先（开发中待发版，正常）；DRIFT=版本落后
    （真实漂移信号，须对齐）；SKIP=非仓库/无 tag/git 不可用。只提示不改退出码。"""
    base = base_dir or os.path.dirname(os.path.abspath(__file__))
    latest = _latest_v_tag(base)
    if latest is None:
        return "SKIP", "-"
    mine = f"v{SERVER_VERSION}"

    def _key(t):
        return [int(x) if x.isdigit() else -1 for x in t[1:].split(".")]

    if mine == latest:
        return "OK", latest
    return ("NEXT", latest) if _key(mine) > _key(latest) else ("DRIFT", latest)


_SKILL_TOOL_PREFIXES = ("fs_", "ide_", "code_", "bug_", "app_", "game_", "ops_",
                        "rust_", "engine_", "guard_", "learn_", "attack_", "meta_",
                        "std_", "ui_", "ast_", "semantic_", "project_")


def _selftest_skills_docs(base_dir=None):
    """skills/*.md ↔ registry 工具名对账（S91：S88 手工补四域契约声明的教训
    工具化——文档漂移机器抓）。口径：每份域文档（除 README/workflow）至少命中
    1 个在册工具名；文档中疑似工具名（域前缀+下划线，且非 tools/ 模块名）若
    不在册 → 计陈旧名（改名/退役后文档没跟上）。返回 (stale 列表, 零命中文件列表)。"""
    import re
    base = base_dir or os.path.dirname(os.path.abspath(__file__))
    skills = os.path.join(base, "skills")
    tools_dir = os.path.join(base, "tools")
    if not os.path.isdir(skills):
        return None, None
    live = set(registry._TOOLS)
    modules = ({os.path.splitext(f)[0] for f in os.listdir(tools_dir)
                if f.endswith(".py")} if os.path.isdir(tools_dir) else set())
    pat = re.compile(r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b")
    stale, dead = [], []
    for f in sorted(os.listdir(skills)):
        if not f.endswith(".md") or f in ("README.md", "workflow.md"):
            continue
        with open(os.path.join(skills, f), encoding="utf-8") as fh:
            text = fh.read()
        tokens = set(pat.findall(text))
        # dead 判定用子串（覆盖无下划线的工具名，如 lesson）；stale 判定才用下划线词元
        if not any(t in text for t in live):
            dead.append(f)
        stale += [t for t in sorted(tokens)
                  if t.startswith(_SKILL_TOOL_PREFIXES) and t not in live
                  and t not in modules]
    return stale, dead


_RX_EXE_NAMES = ("rx-mcp.exe", "rx-taint.exe", "rx-fs.exe", "rx-ide.exe",
                 "rx-search.exe", "rx-semantic.exe", "rx-scan.exe",
                 "rx-audit.exe", "rx-appops.exe")


def _selftest_exe_tag():
    """rust exe ↔ SERVER_VERSION 对账（S94：「你不更新某一个东西当然出问题」
    的机器防呆——代码进了新版本、exe 还是旧的，此前没有任何对账能发现）。
    9 个 exe 逐个按工具层定位约定（UNIFIED_RX_RS_EXE 覆盖 →
    %TEMP%\\rx-rs-target\\{release,debug}，basename 须恰等）找到后跑
    --version 比对 SERVER_VERSION。打印 EXE_TAG ok=N drift=N missing=N
    （drift/missing 细节截断附后）；9 个全缺 → SKIP（纯 Python 环境未
    cargo build，不算漂移）。只提示不改退出码。"""
    import subprocess
    override = os.environ.get("UNIFIED_RX_RS_EXE")
    tmp = os.environ.get("TEMP", r"C:\Temp")
    dirs = [os.path.join(tmp, "rx-rs-target", kind)
            for kind in ("release", "debug")]
    ok, drift, missing = 0, [], []
    for name in _RX_EXE_NAMES:
        path = None
        for c in ([override] if override else []) + [os.path.join(d, name)
                                                     for d in dirs]:
            if os.path.isfile(c) and os.path.basename(c) == name:
                path = c
                break
        if path is None:
            missing.append(name)
            continue
        try:
            cp = subprocess.run([path, "--version"], capture_output=True,
                                timeout=15)
            ver = cp.stdout.decode("utf-8", "replace").strip()
        except Exception:
            ver = ""
        if ver == SERVER_VERSION:
            ok += 1
        else:
            drift.append(f"{name}({ver or '无输出'})")
    if ok + len(drift) == 0:
        return None
    return ok, drift, missing


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
    # S91 机器对账两件：版本漂移 + skills 文档漂移（只提示，不改退出码）
    vt, tag = _selftest_version_tag()
    print(f"VERSION_TAG {vt} latest={tag}")
    stale, dead = _selftest_skills_docs()
    if stale is None:
        print("SKILLS_DOCS SKIP")
    else:
        extra = (f" stale={stale[:8]}" if stale else "") + (f" dead={dead[:8]}" if dead else "")
        print(f"SKILLS_DOCS stale={len(stale)} dead={len(dead)}{extra}")
    # S94 机器对账第三件：rust exe 版本漂移（同纪律：只提示，不改退出码）
    exe = _selftest_exe_tag()
    if exe is None:
        print("EXE_TAG SKIP (9 个 exe 全缺——纯 Python 环境未 cargo build)")
    else:
        ok, drift, missing = exe
        extra = ""
        if drift:
            extra += f" drift={drift[:4]}"
        if missing:
            extra += f" missing={missing[:4]}"
        print(f"EXE_TAG ok={ok} drift={len(drift)} missing={len(missing)}{extra}")
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
