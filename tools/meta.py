# -*- coding: utf-8 -*-
"""tools/meta.py —— 元域（2 工具）：local_run / process

local_run：白名单命令执行（收敛自旧版 local_run + local_tools）。
2026-08-24 修复（用户实测暴露）：
- P1: blender 模板用默认路径常量（不再依赖 {blender} 占位符）
- P2: subprocess 超时后 taskkill 杀进程树（防残留卡死 server）
- P3: 加 background 参数（Popen 立即返回，长驻命令不阻塞）
- P4: 新增 process 域（tasklist/taskkill）
- P5: blender 保留 headless_model 别名（旧名兼容）
- P7: _ALLOWED 含反斜杠（Windows 路径必需）
"""
import os
import subprocess
import threading
import time

from registry import tool
from registry import current_request_id  # S10：取消轮询用（经 server.cancel_flag 查 Event）

# Blender 默认路径（Windows）
_BLENDER = r"D:\rj\GJ\Blender 5.2\blender.exe"

# 白名单命令模板（domain → {name: cmd}）
_COMMANDS = {
    "python": {
        "script": "python -X utf8 {script}",
        "pytest": "python -X utf8 -m pytest {file} -q",
    },
    "cargo": {
        "check": "cargo check -p {pkg}",
        "check_all": "cargo check --workspace",
        "test": "cargo test -p {pkg}",
        "test_all": "cargo test --workspace",
        "run": "cargo run -p {pkg}",
        "build": "cargo build -p {pkg}",
        "build_all": "cargo build --workspace",
    },
    "git": {
        "status": "git status --short",
        "log": "git log --oneline -{n}",
    },
    "blender": {
        # 默认路径内置（P1 修复：不再需要调用方传 {blender}）
        "headless": '"{blender}" --background --python {script} -- {args}',
        "headless_model": '"{blender}" --background --python {script} -- {args}',  # 旧名兼容
    },
    "process": {
        "list": "tasklist",
        "list_filter": 'tasklist /FI "IMAGENAME eq {name}"',
        "kill": "taskkill /F /IM {name}",
        "kill_pid": "taskkill /F /PID {pid}",
    },
    "unifiedrx": {
        "selftest": "python -X utf8 server.py --selftest",
    },
}

_CHEATSHEET = {
    "cargo": [("check", "cargo check -p {pkg}", "单包编译检查"),
              ("check_all", "cargo check --workspace", "全量编译检查"),
              ("test", "cargo test -p {pkg}", "单包测试"),
              ("test_all", "cargo test --workspace", "全量测试"),
              ("run", "cargo run -p {pkg}", "运行（长驻命令用 background=true）"),
              ("build", "cargo build -p {pkg}", "编译"),
              ("build_all", "cargo build --workspace", "全量编译")],
    "python": [("script", "python -X utf8 {script}", "跑脚本（UTF-8）"),
               ("pytest", "python -X utf8 -m pytest {file} -q", "单文件测试")],
    "git": [("status", "git status --short", "工作区状态"),
            ("log", "git log --oneline -{n}", "提交历史")],
    "blender": [("headless", '"{blender}" --background --python {script}', "无头建模（默认 Blender 5.2）"),
                ("headless_model", '"{blender}" --background --python {script}', "无头建模（旧名）")],
    "process": [("list", "tasklist", "进程列表"),
                ("list_filter", 'tasklist /FI "IMAGENAME eq {name}"', "按名查进程"),
                ("kill", "taskkill /F /IM {name}", "按名杀进程"),
                ("kill_pid", "taskkill /F /PID {pid}", "按 PID 杀进程")],
    "unifiedrx": [("selftest", "python server.py --selftest", "注册表自检")],
}

# 安全字符白名单（防 shell 注入：无 & | > < ; $ ` 等；含 \\ 供 Windows 路径）
_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 /-_.:,=()\"'\\")

# S72：local_run 出口解码与上限。
# 旧版固定 GBK 解码且 stdout 只留尾部 3000 字符——UTF-8 输出乱码、
# 编译/测试报错的关键行被砍。现在 UTF-8 优先、GBK 回退；
# 失败（exit != 0）时放宽尾巴（UNIFIED_RX_RUN_TAIL_FAIL 可覆盖，默认 12000）。
_RUN_TAIL_OK_OUT = 3000
_RUN_TAIL_OK_ERR = 1000
_RUN_TAIL_FAIL_OUT_DEFAULT = 12000
_RUN_TAIL_FAIL_ERR = 4000


def _run_decode(raw: bytes) -> str:
    """S72：优先 UTF-8，失败按 GBK 回退（replace 兜底，永不抛异常）。"""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", errors="replace")


def _run_tail(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[-limit:]


def _run_caps(failed: bool):
    """失败时放宽出口（报错内容是修 bug 的原料），成功时维持小尾巴防上下文膨胀。"""
    if not failed:
        return _RUN_TAIL_OK_OUT, _RUN_TAIL_OK_ERR
    try:
        out_cap = int(os.environ.get("UNIFIED_RX_RUN_TAIL_FAIL") or _RUN_TAIL_FAIL_OUT_DEFAULT)
    except ValueError:
        out_cap = _RUN_TAIL_FAIL_OUT_DEFAULT
    return max(out_cap, _RUN_TAIL_OK_OUT), _RUN_TAIL_FAIL_ERR


def _fill_defaults(domain, name, cmd, args):
    """填充占位符：优先 args，其次模板默认值。"""
    defaults = {"blender": _BLENDER}
    merged = dict(defaults)
    if args:
        merged.update(args)
    for k, v in merged.items():
        cmd = cmd.replace("{" + k + "}", str(v))
    return cmd


def _cancel_event():
    """S10-B3 收线：当前请求的取消 Event（登记实现在 registry，双模块世界免疫）。"""
    mid = current_request_id()
    if mid is None:
        return None
    from registry import cancel_flag
    return cancel_flag(mid)


@tool("local_run", "执行内建命令模板（白名单，subprocess+超时；需 __authorized=True；长驻命令用 background=true）", "meta",
      {"type": "object",
       "properties": {
            "domain": {"type": "string", "description": "命令域（python/git/fs 等内建模板）"},
           "name": {"type": "string", "description": "命令名"},
           "args": {"type": "object", "description": "占位符参数 {pkg}/{script} 等"},
           "workdir": {"type": "string", "description": "工作目录（默认当前）"},
           "timeout": {"type": "integer", "description": "超时秒（默认 60）"},
           "background": {"type": "boolean", "description": "后台运行（Popen 立即返回 PID，不阻塞；适合 cargo run 等长驻命令）"},
       },
       "required": ["domain", "name"]},
      requires_auth=True)
def local_run(domain, name, args=None, workdir=None, timeout=60, background=False, __authorized=False):
    del __authorized  # 执行授权由 registry.call 的 requires_auth 统一强制
    cmds = _COMMANDS.get(domain, {})
    template = cmds.get(name)
    if not template:
        return {"error": f"未知命令: {domain}/{name}；可用模板以内建白名单为准"}
    # 填充占位符（P1：内置默认值，blender 无需传路径）
    cmd = _fill_defaults(domain, name, template, args)
    # 残留未填充占位符 → 明确报错（不误报"不安全字符"）
    if "{" in cmd or "}" in cmd:
        return {"error": f"命令含未填充占位符: {cmd}；请补全 args"}
    # 参数安全校验（防 shell 注入）
    if any(c not in _ALLOWED for c in cmd):
        return {"error": f"命令含不安全字符，拒绝执行: {cmd}"}
    env = {**os.environ, "PYTHONUTF8": "1"}
    try:
        if background:
            # P3：后台运行，立即返回 PID（长驻命令专用）
            from registry import notify
            notify("info", f"local_run 后台启动: {cmd[:80]}")
            p = subprocess.Popen(cmd, shell=True, cwd=workdir, env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return {"ok": True, "background": True, "pid": p.pid,
                    "cmd": cmd, "note": "后台运行中；用 process/list_filter 或 process/kill_pid 管理"}
        # 同步运行（S10 重写）：读者线程收流不丢输出；主循环节拍轮询【取消/超时】，
        # 命中即 taskkill 进程树。P1 修复的独立进程组保留。
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.Popen(cmd, shell=True, cwd=workdir,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, creationflags=flags)

        def _pump(dst, src):
            try:
                for chunk in iter(lambda: src.read(4096), b""):
                    dst.append(chunk)
            except Exception:
                pass

        out_buf, err_buf = [], []
        t_out = threading.Thread(target=_pump, args=(out_buf, proc.stdout), daemon=True)
        t_err = threading.Thread(target=_pump, args=(err_buf, proc.stderr), daemon=True)
        t_out.start()
        t_err.start()

        def _kill_tree():
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=10, env=env)
            except Exception:
                pass
            try:
                proc.kill()
            except Exception:
                pass

        cancel_ev = _cancel_event()
        from registry import notify, notify_progress
        notify("info", f"S10 cancel-watch rid={current_request_id()} armed={cancel_ev is not None}")
        deadline = time.monotonic() + max(5, min(int(timeout), 600))
        started = time.monotonic()
        last_beat = started
        cancelled = timed_out = False
        while proc.poll() is None:
            if cancel_ev is not None and cancel_ev.is_set():
                cancelled = True
                break
            if time.monotonic() >= deadline:
                timed_out = True
                break
            now = time.monotonic()
            if now - last_beat >= 1.5:      # S12：1.5s 心跳（无 token 时 no-op）
                notify_progress(round(now - started, 1), f"running {cmd[:60]}")
                last_beat = now
            time.sleep(0.25)
        if proc.poll() is None or cancelled or timed_out:
            _kill_tree()
        try:
            proc.wait(timeout=3)
        except Exception:
            pass
        try:
            t_out.join(timeout=2)
            t_err.join(timeout=2)
        except Exception:
            pass
        # S72：UTF-8 优先解码（旧版固定 GBK，UTF-8 输出必乱码）；
        # 失败时放宽尾巴——报错关键行是修 bug 的原料，成功时维持小尾巴
        failed = proc.returncode not in (0, None)
        out_cap, err_cap = _run_caps(failed)
        out = _run_tail(_run_decode(b"".join(out_buf)), out_cap)
        err = _run_tail(_run_decode(b"".join(err_buf)), err_cap)
        base = {"exit": proc.returncode, "stdout_tail": out,
                "stderr_tail": err, "cmd": cmd}
        if cancelled:
            return {**base, "ok": False, "cancelled": True,
                    "error": "运行已被取消（notifications/cancelled），进程树已清理"}
        if timed_out:
            return {**base, "ok": False,
                    "error": f"超时（>{timeout}s），已清理本次进程树；长驻命令请用 background=true"}
        return {**base, "ok": proc.returncode == 0}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@tool("process", "进程管理（list 查询 / kill 结束）", "meta",
      {"type": "object",
       "properties": {
           "action": {"type": "string", "description": "list/kill"},
           "name": {"type": "string", "description": "进程名（如 vxl_app.exe / blender.exe）"},
           "pid": {"type": "integer", "description": "kill 用：PID（可选，比 name 精确）"},
       },
       "required": ["action"]},
      requires_auth=True)  # S75：taskkill /F 可杀任意进程（含宿主自身），破坏性动作须显式授权
def process(action, name=None, pid=None, __authorized=False):
    del __authorized  # S75：执行授权由 registry.call 的 requires_auth 统一强制
    env = {**os.environ, "PYTHONUTF8": "1"}
    try:
        if action == "list":
            if name:
                r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}"],
                                   capture_output=True, text=True, timeout=30, env=env,
                                   encoding="gbk", errors="replace")
            else:
                r = subprocess.run(["tasklist"], capture_output=True, text=True, timeout=30, env=env,
                                   encoding="gbk", errors="replace")
            procs = []
            for line in (r.stdout or "").split("\n"):
                parts = line.split()
                if len(parts) >= 2 and parts[0].lower().endswith((".exe", ".py")):
                    try:
                        procs.append({"name": parts[0], "pid": int(parts[1])})
                    except ValueError:
                        continue
            return {"ok": True, "count": len(procs), "processes": procs[:50]}
        if action == "kill":
            if pid:
                r = subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                                   capture_output=True, text=True, timeout=30, env=env,
                                   encoding="gbk", errors="replace")
            elif name:
                r = subprocess.run(["taskkill", "/F", "/IM", name],
                                   capture_output=True, text=True, timeout=30, env=env,
                                   encoding="gbk", errors="replace")
            else:
                return {"error": "kill 需要 name 或 pid"}
            return {"ok": r.returncode == 0, "exit": r.returncode,
                    "output": (r.stdout or "")[:300] + (r.stderr or "")[:200]}
        return {"error": f"未知 action: {action}（list/kill）"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "tasklist/taskkill 超时"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
