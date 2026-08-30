# -*- coding: utf-8 -*-
"""tools/ide_vscode.py —— VS Code 后手入口（S68）。

定位：工具链（build/lint/LSP/doctor）查不出或需要人工/AI 深查时，
把项目/文件/定位直接丢进 VS Code——最后的后手，不是常规检查器。
"""
import os
import subprocess

from registry import tool
from tools.fs import _resolve as _fs_resolve

DEFAULT_CODE_EXE = r"D:\rj\KF\IDE\Microsoft VS Code\Code.exe"


def _spawn(cmd):
    """分离式拉起 GUI 进程（不阻塞工具调用）。bat/cmd 需控制台语义 → cmd /c。"""
    exe = cmd[0].lower()
    if exe.endswith((".bat", ".cmd")):
        flags = subprocess.CREATE_NEW_PROCESS_GROUP
    elif os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        flags = 0
    subprocess.Popen(cmd, creationflags=flags, close_fds=True)


def _goto(target):
    """'path' 或 'path:line:col' → (绝对路径, goto 串 or None)。"""
    parts = target.rsplit(":", 2)
    tail = parts[1:] if len(parts) == 3 else []
    if tail and all(t.isdigit() for t in tail):
        real = _fs_resolve(parts[0])
        return real, ":".join([real] + tail)
    return _fs_resolve(target), None


@tool("ide_vscode", "VS Code 打开项目/文件/行列定位/双栏对比——工具链查不出问题时的"
      "最后后手（人工深查入口）；分离式拉起不阻塞", "ide",
      {"type": "object",
       "properties": {
           "action": {"type": "string", "enum": ["open", "diff"],
                      "description": "open=打开 paths（支持 path:line:col 用 -g 定位）；"
                                     "diff=双栏对比 a/b"},
           "paths": {"type": "array", "items": {"type": "string"},
                     "description": "open 的目标（项目目录/文件/path:line:col）"},
           "a": {"type": "string", "description": "diff 左侧"},
           "b": {"type": "string", "description": "diff 右侧"},
           "exe": {"type": "string",
                   "description": "Code.exe 路径覆盖（默认 D:\\rj\\KF\\IDE\\Microsoft VS Code\\Code.exe）"},
       },
       "required": ["action"]},
      requires_auth=True)
def ide_vscode(action="open", paths=None, a=None, b=None, exe=None,
               __authorized=False):
    exe = exe or os.environ.get("UNIFIED_RX_VSCODE", DEFAULT_CODE_EXE)
    if not os.path.isfile(exe):
        return {"error": f"VS Code 不存在: {exe}"}
    if action == "open":
        if not paths:
            return {"error": "open 需要 paths"}
        args, opened = [exe], []
        for t in paths:
            real, goto = _goto(t)
            if goto:
                args += ["-g", goto]
            else:
                args.append(real)
            opened.append(real)
        _spawn(args)
        return {"ok": True, "action": "open", "opened": opened, "exe": exe,
                "note": "分离式拉起；定位用 path:line:col"}
    if action == "diff":
        if not a or not b:
            return {"error": "diff 需要 a 与 b"}
        ra, rb = _fs_resolve(a), _fs_resolve(b)
        _spawn([exe, "--diff", ra, rb, "-n"])
        return {"ok": True, "action": "diff", "a": ra, "b": rb, "exe": exe}
    return {"error": f"未知 action: {action}（open/diff）"}
