# -*- coding: utf-8 -*-
"""tools/meta.py —— 元域（2 工具）：local_run / cmd_cheatsheet

local_run：白名单命令执行（收敛自旧版 local_run + local_tools）。
修复旧版 python 域卡死问题：统一走 subprocess + 超时 + PYTHONUTF8。
"""
import os
import subprocess
import shlex

from registry import tool

# 白名单命令模板（domain → {name: cmd}）
_COMMANDS = {
    "python": {
        "script": "python -X utf8 {script}",
        "pytest": "python -X utf8 -m pytest {file} -q",
    },
    "cargo": {
        "check": "cargo check -p {pkg}",
        "test": "cargo test -p {pkg}",
        "test_all": "cargo test --workspace",
    },
    "git": {
        "status": "git status --short",
        "log": "git log --oneline -{n}",
    },
    "blender": {
        "headless": "\"{blender}\" --background --python {script} -- {args}",
    },
    "unifiedrx": {
        "selftest": "python -X utf8 server.py --selftest",
    },
}

_CHEATSHEET = {
    "cargo": [("check", "cargo check -p {pkg}", "单包编译检查"),
              ("test", "cargo test -p {pkg}", "单包测试"),
              ("test_all", "cargo test --workspace", "全量测试")],
    "python": [("script", "python -X utf8 {script}", "跑脚本（UTF-8）"),
               ("pytest", "python -X utf8 -m pytest {file} -q", "单文件测试")],
    "git": [("status", "git status --short", "工作区状态"),
            ("log", "git log --oneline -{n}", "提交历史")],
    "unifiedrx": [("selftest", "python server.py --selftest", "注册表自检")],
}


@tool("cmd_cheatsheet", "内建命令手册（省 token，不用试错找命令）", "meta",
      {"type": "object",
       "properties": {"domain": {"type": "string", "description": "cargo/git/python/blender/unifiedrx（缺省全部）"}},
       "required": []})
def cmd_cheatsheet(domain=None):
    if domain:
        cmds = _CHEATSHEET.get(domain, [])
        return {"domain": domain, "commands": [{"name": n, "cmd": c, "desc": d} for n, c, d in cmds]}
    out = {}
    for d, cmds in _CHEATSHEET.items():
        out[d] = [n for n, _, _ in cmds]
    return {"domains": list(_CHEATSHEET), "total": sum(len(v) for v in _CHEATSHEET.values()),
            "by_domain": out}


@tool("local_run", "执行内建命令模板（白名单，subprocess+超时）", "meta",
      {"type": "object",
       "properties": {
           "domain": {"type": "string", "description": "命令域（查 cmd_cheatsheet）"},
           "name": {"type": "string", "description": "命令名"},
           "args": {"type": "object", "description": "占位符参数 {pkg}/{script} 等"},
           "workdir": {"type": "string", "description": "工作目录（默认当前）"},
           "timeout": {"type": "integer", "description": "超时秒（默认 120）"},
       },
       "required": ["domain", "name"]})
def local_run(domain, name, args=None, workdir=None, timeout=120, __authorized=False):
    cmds = _COMMANDS.get(domain, {})
    template = cmds.get(name)
    if not template:
        return {"error": f"未知命令: {domain}/{name}；查 cmd_cheatsheet"}
    # 填充占位符
    cmd = template
    for k, v in (args or {}).items():
        cmd = cmd.replace("{" + k + "}", str(v))
    # 参数安全校验：仅允许字母数字/空格/-_.:,/（防注入）
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 /-_.:,=")
    if any(c not in allowed for c in cmd):
        return {"error": f"命令含不安全字符，拒绝执行: {cmd}"}
    try:
        r = subprocess.run(cmd, shell=True, cwd=workdir,
                           capture_output=True, text=True,
                           timeout=max(10, min(int(timeout), 600)),
                           env={**os.environ, "PYTHONUTF8": "1"})
        return {
            "ok": r.returncode == 0,
            "exit": r.returncode,
            "stdout_tail": r.stdout[-3000:],
            "stderr_tail": r.stderr[-1000:],
            "cmd": cmd,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"超时（>{timeout}s）", "cmd": cmd}
    except Exception as e:
        return {"ok": False, "error": str(e)}
