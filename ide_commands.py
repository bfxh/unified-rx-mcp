#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""ide_commands.py — 内建命令手册 + 本地执行（MCP_OPTIMIZATION_PLAN M3）。

智能体做项目时不用反复试错/搜索"该用什么命令"——常用命令写死：
  cmd_cheatsheet — 按域查命令手册（cargo/blender/git/python）
  local_run     — 执行内建命令模板（参数化，结果结构化返回）

省 token：每次任务省 3-5 轮"怎么编译/怎么测试"的试错。
"""

import json
import os
import subprocess
import shutil

# ── 命令手册（按域）──────────────────────────────────────
_CHEATSHEET: dict[str, list[dict]] = {
    "cargo": [
        {"name": "build", "cmd": "cargo build", "desc": "编译（debug）"},
        {"name": "build_release", "cmd": "cargo build --release -p {pkg}", "desc": "编译 release（产物在 target/release）"},
        {"name": "check", "cmd": "cargo check -p {pkg}", "desc": "快速类型检查（不产出二进制）"},
        {"name": "test", "cmd": "cargo test --workspace", "desc": "全量测试"},
        {"name": "test_one", "cmd": "cargo test -p {pkg} {test_name}", "desc": "单测试"},
        {"name": "clippy", "cmd": "cargo clippy --workspace", "desc": "Lint 检查"},
        {"name": "run", "cmd": "cargo run -p {pkg}", "desc": "运行 debug 版"},
        {"name": "fmt", "cmd": "cargo fmt --check", "desc": "格式检查"},
    ],
    "git": [
        {"name": "status", "cmd": "git status --short", "desc": "工作区状态"},
        {"name": "commit", "cmd": "git add -A; git commit -m \"{msg}\"", "desc": "提交"},
        {"name": "log", "cmd": "git log --oneline -{n}", "desc": "最近提交"},
        {"name": "diff", "cmd": "git diff {path}", "desc": "查看改动"},
    ],
    "python": [
        {"name": "pytest_all", "cmd": "python -X utf8 -m pytest {tests} -q", "desc": "全量测试"},
        {"name": "pytest_one", "cmd": "python -X utf8 -m pytest {file} -q", "desc": "单文件测试"},
        {"name": "script", "cmd": "python -X utf8 {script}", "desc": "跑脚本"},
    ],
    "blender": [
        {"name": "headless_model", "cmd": r'"D:\rj\GJ\Blender 5.2\blender.exe" --background --python {script} -- {args}',
         "desc": "Blender 无头建模（D:\rj\GJ\Blender 5.2）"},
        {"name": "export_glb", "cmd": "Blender 内 io_bevy_export.py（N 面板/Ctrl+Shift+E）",
         "desc": "Bevy 直通导出（assets/models/）"},
    ],
    "voxelforge": [
        {"name": "release_deploy", "cmd": "cargo build --release -p nexus_app; Stop-Process -Name nexus_app; Copy-Item target\\release\\nexus_app.exe release\\; Copy-Item assets\\models release\\assets\\models -Recurse",
         "desc": "发布流程：编译→杀进程→复制 exe→同步资产（VoxelForge）"},
        {"name": "test_workspace", "cmd": "cargo test --workspace", "desc": "全量测试（207 目标）"},
        {"name": "run_release", "cmd": "Start-Process release\\nexus_app.exe -WorkingDirectory release", "desc": "运行发布版"},
    ],
    "unifiedrx": [
        {"name": "test", "cmd": "python -X utf8 -m pytest test_unified_rx.py test_enhancements.py test_enhancements2.py test_rustscan.py test_ide_*.py -q",
         "desc": "unified-rx 全量测试（183+）"},
        {"name": "sync_e", "cmd": "Copy-Item *.py test_*.py E:\\共享\\51\\unified-rx\\",
         "desc": "同步运行版 E:"},
        {"name": "bug_hunt", "cmd": "pipeline({preset: bug_hunt, path: ...})", "desc": "默认挖漏洞链"},
    ],
}


def cheatsheet(domain: str | None = None) -> dict:
    """命令手册查询。domain=None 返回全部。"""
    if domain:
        return {"ok": True, "domain": domain,
                "commands": _CHEATSHEET.get(domain, []),
                "hint": "用 local_run 执行（name + args）"}
    return {"ok": True, "domains": list(_CHEATSHEET.keys()),
            "total": sum(len(v) for v in _CHEATSHEET.values()),
            "hint": "按域查：cmd_cheatsheet({domain: 'cargo'})"}


def local_run(domain: str, name: str, args: dict | None = None,
              workdir: str | None = None, timeout: int = 300) -> dict:
    """执行内建命令模板。args 里的 {key} 占位符由参数填充。

    安全：只允许 _CHEATSHEET 里定义的命令（参数注入占位符——命令名不可控）。
    """
    args = args or {}
    cmds = _CHEATSHEET.get(domain, [])
    entry = next((c for c in cmds if c["name"] == name), None)
    if entry is None:
        return {"ok": False, "error": f"未知命令: {domain}/{name}",
                "available": [c["name"] for c in cmds]}
    template = entry["cmd"]
    try:
        cmd = template.format(**args)
    except KeyError as e:
        return {"ok": False, "error": f"缺少参数: {e}",
                "template": template}
    cwd = workdir or os.getcwd()
    try:
        # 分号连接的多命令（如 release_deploy）在 PowerShell 语义下由 shell 执行
        r = subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True,
                           text=True, timeout=timeout, encoding="utf-8", errors="replace")
        return {"ok": r.returncode == 0,
                "domain": domain, "name": name,
                "cmd": cmd,
                "exit": r.returncode,
                "stdout_tail": (r.stdout or "")[-1500:],
                "stderr_tail": (r.stderr or "")[-500:]}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"超时（>{timeout}s）", "cmd": cmd}
    except OSError as e:
        return {"ok": False, "error": f"执行失败: {e}", "cmd": cmd}
