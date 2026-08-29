# -*- coding: utf-8 -*-
"""tools/ide_common.py —— ide 域共享助手与解析器（S48 职责拆分）。
本模块不含 @tool 注册；由 tools/ide.py 门面统一再导出。"""
import os
import re
import json
import tempfile
import subprocess
import shutil
import math

from tools.fs import _resolve as _fs_resolve

MAX_CTX = 5000
"""tools/ide.py —— IDE 增强域（8 工具）

收敛自旧版 ide_complete_chain/ide_continue/ide_jump_predict/ide_open_at 等。
重点修复旧版 0 应用问题：ide_edit_multi 用「内容匹配」而非「行号匹配」，
行号偏移不再导致编辑静默失败。
2026-08-25 修复（用户反馈 IDE 限制 AI）：
- I1: ide_edit_multi 支持 occ 参数（同内容多处，指定第几次出现）
- I2: 行数组块匹配（消除拼接字符串 find 的顺序依赖隐患）
- I3: 写回保留原行尾（CRLF/LF 不破坏）
- I4: locate_edit 等 max_files 只计代码文件
S32（用户"IDE 调试编译开搞"）：ide_build（cargo check/compileall/go build →
结构化诊断）+ ide_debug（argv 直跑不走 shell → panic/traceback 解析成帧）。
诚实边界：不是内置 VS——编译/调试走真实工具链（cargo/python/go），缺失即如实
报错；沙盒沿 fs 域 _fs_resolve，cmd 为 argv 列表不走 shell。
"""

MAX_CTX = 5000

_SKIP_DIRS = (".git", "node_modules", "target", "__pycache__", "dist", "build",
              ".unified-rx-index", ".codegraph", "backups", "assets", "data", "models", "docs")

def _read(path):
    # P0 修复：读路径也过沙盒（防读任意盘文件）
    try:
        path = _fs_resolve(path)
    except ValueError:
        return None
    if not os.path.isfile(path):
        return None
    # newline="" 保留原始行尾（CRLF 不被转 LF），供 _detect_eol 检测
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read()

def _lang_of(path):
    ext = os.path.splitext(path)[1].lower()
    return {"py": "python", "rs": "rust", "go": "go", "ts": "typescript",
            "tsx": "typescript", "js": "javascript", "jsx": "javascript",
            "gd": "gdscript", "cs": "csharp", "dart": "dart"}.get(ext.lstrip("."), "text")

def _iter_files(root, max_files, skip_dirs=None):
    """遍历代码文件：max_files 只计有语言的代码文件（I4 修复）。"""
    skip = set(_SKIP_DIRS)
    if skip_dirs:
        skip |= set(skip_dirs)
    count = 0
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip]
        for fn in files:
            fp = os.path.join(r, fn)
            if _lang_of(fp) == "text":
                continue
            if count >= max_files:
                return
            count += 1
            yield fp

def _detect_eol(src):
    """检测行尾：CRLF / LF。"""
    crlf = src.count("\r\n")
    lf = src.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"

_RE_PY_FRAME = re.compile(r'File "([^"]+)", line (\d+)(?:, in (\w+))?')

_RE_PY_LAST = re.compile(r"^([A-Za-z_.]+(?:Error|Exception|Interrupt|Exit))(?::\s*(.*))?$")

_RE_GCC_DIAG = re.compile(r"^(.+?):(\d+)(?::(\d+))?:\s+(error|warning|note|fatal error):\s+(.*)$")

_RE_GO_FRAME = re.compile(r"^\s+(?:[\w().*]+\.)?([\w().*]+)\(\)$|^\s+(.+?\.go):(\d+)")

def _parse_gcc(out, root):
    diags, seen = [], set()
    for line in out.splitlines():
        m = _RE_GCC_DIAG.match(line.strip())
        if not m:
            continue
        f, ln, col, level, msg = m.groups()
        fp = f if os.path.isabs(f) else os.path.join(root, f)
        key = (fp, ln, level, msg)
        if key in seen:
            continue
        seen.add(key)
        diags.append({"file": os.path.abspath(fp), "line": int(ln),
                      "col": int(col) if col else 0, "level": level,
                      "msg": msg.strip()[:200]})
    return diags


_RE_CARGO_SHORT = re.compile(
    r"^(.+?):(\d+):(\d+):\s+(error|warning|note)\[?[EW0-9]*\]?:\s+(.*)$")


def _parse_cargo_short(out, root):
    diags, seen = [], set()
    for line in out.splitlines():
        m = _RE_CARGO_SHORT.match(line.strip())
        if not m:
            continue
        f, ln, col, level, msg = m.groups()
        fp = f if os.path.isabs(f) else os.path.join(root, f)
        key = (fp, ln, level, msg)
        if key in seen:
            continue
        seen.add(key)
        diags.append({"file": os.path.abspath(fp), "line": int(ln), "col": int(col),
                      "level": level, "msg": msg.strip()[:200]})
    return diags


_RE_GO_BUILD = re.compile(r"^(.+?\.go):(\d+)(?::(\d+))?:\s+(.*)$")


def _parse_go_build(out, root):
    """go build 错误无 level 词（`file:line:col: msg`），专用解析。"""
    diags, seen = [], set()
    for line in out.splitlines():
        m = _RE_GO_BUILD.match(line.strip())
        if not m:
            continue
        f, ln, col, msg = m.groups()
        fp = f if os.path.isabs(f) else os.path.join(root, f)
        key = (fp, ln, msg)
        if key in seen:
            continue
        seen.add(key)
        diags.append({"file": os.path.abspath(fp), "line": int(ln),
                      "col": int(col) if col else 0, "level": "error",
                      "msg": msg.strip()[:200]})
    return diags
