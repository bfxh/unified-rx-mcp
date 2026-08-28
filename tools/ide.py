# -*- coding: utf-8 -*-
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
import os
import re

from registry import tool
from tools.fs import _resolve as _fs_resolve  # P0: 复用 fs 域沙盒校验

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


# ---------- locate_edit：自然语言/符号 → 位置 ----------
@tool("locate_edit", "定位：符号/关键词 → file:line + snippet（改代码引导）", "ide",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "代码库根目录"},
           "query": {"type": "string", "description": "要改的符号/关键词/报错片段"},
           "max_files": {"type": "integer", "description": "扫描上限（默认 100）"},
           "limit": {"type": "integer", "description": "候选数（默认 10）"},
       },
       "required": ["path", "query"]})
def locate_edit(path, query, max_files=100, limit=10):
    try:
        path = _fs_resolve(path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(path):
        return {"error": f"不是目录: {path}"}
    query = query.strip()
    if not query:
        # S7 攻击修复：空/纯空白查询会把全库前 N 行当"命中"返回（total=15/refs=12424 纯噪音）
        # 结构化失败（错误进 result.error 而非 ok 层），调用方语义一致
        return {"error": "query 为空——请提供符号或关键词"}
    hits = []
    all_sources = {}  # S6-D2: 引用计数需要全量文件内容（max_files 范围内）
    for fp in _iter_files(path, max_files):
        src = _read(fp)
        if not src:
            continue
        all_sources[fp] = src
        lines = src.split("\n")
        for idx, line in enumerate(lines, 1):
            # 符号/关键词命中（区分大小写优先精确，其次忽略大小写）
            if query in line or (query.lower() in line.lower() and query not in line):
                ctx = lines[max(0, idx - 2):idx + 3]
                hits.append({"file": fp, "line": idx, "snippet": "\n".join(ctx)})
                if len(hits) >= limit * 3:
                    break
        if len(hits) >= limit * 3:
            break
    # S6-D2 影响面事实：query 在扫描范围内的出现总次数。
    # 只提供计数事实，不判断该不该改——影响面决策留给智能体。
    ref_count = sum(src.count(query) for src in all_sources.values())
    return {"query": query, "total": len(hits), "references_in_scan": ref_count,
            "hits": hits[:limit]}


# ---------- code_context：光标上下文 ----------
@tool("code_context", "读取光标附近代码上下文（改前取上下文）", "ide",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "文件绝对路径"},
           "cursor_line": {"type": "integer", "description": "光标行号（1-based，0=无）"},
           "radius": {"type": "integer", "description": "半径行数（默认 30）"},
       },
       "required": ["path"]})
def code_context(path, cursor_line=0, radius=30):
    src = _read(path)
    if src is None:
        return {"error": f"文件不可读: {path}"}
    lines = src.split("\n")
    radius = max(5, min(int(radius or 30), 200))
    if not cursor_line:
        start, end = 0, min(len(lines), 80)
    else:
        start = max(0, cursor_line - 1 - radius)
        end = min(len(lines), cursor_line - 1 + radius)
    return {
        "file": path, "lang": _lang_of(path),
        "total_lines": len(lines),
        "start": start + 1, "end": end,
        "content": "\n".join(lines[start:end]),
    }


# ---------- ide_edit_multi：内容匹配多行编辑（核心修复） ----------
@tool("ide_edit_multi", "多行修改：内容匹配应用（支持 occ 指定第几次出现；保留原行尾）", "ide",
      {"type": "object",
       "properties": {
           "root": {"type": "string", "description": "仓库根（可选）"},
           "file_path": {"type": "string", "description": "文件"},
           "edits": {"type": "array",
                     "description": "[{old_lines: [...], new_lines: [...], occ?: 1}]——old_lines 逐行精确匹配；occ 指定匹配第几次出现（默认 1）"},
           "old_lines": {"type": "array", "description": "兼容写法：单处修改的旧行（等价于 edits 里的一项 old_lines）"},
           "new_lines": {"type": "array", "description": "兼容写法：单处修改的新行（等价于 edits 里的一项 new_lines）"},
       },
       "required": ["file_path", "edits"]},
      requires_auth=True)
def ide_edit_multi(file_path, edits, root=None, __authorized=False, old_lines=None, new_lines=None):
    # 授权已由 registry.call 的 requires_auth 强制；此处信任进入即已授权
    if old_lines or new_lines:
        edits = (edits or []) + [{"old_lines": old_lines, "new_lines": new_lines or []}]
    p = file_path
    if root and not os.path.isabs(p):
        p = os.path.join(root, p)
    try:
        p = _fs_resolve(p)
    except ValueError as e:
        return {"error": str(e)}
    src = _read(p)
    if src is None:
        return {"error": f"文件不可读: {p}"}
    eol = _detect_eol(src)
    lines = src.split("\n")
    # 去掉每行尾部的 \r（CRLF 时），统一成 \n 数组
    lines = [ln[:-1] if ln.endswith("\r") else ln for ln in lines]
    applied = 0
    errors = []
    for e in edits or []:
        old = e.get("old_lines") or []
        new = e.get("new_lines") or []
        occ = int(e.get("occ", 1) or 1)
        if not old:
            errors.append("old_lines 为空")
            continue
        # 逐行块匹配（第 occ 次出现）——I1/I2 修复
        found = -1
        seen = 0
        for i in range(len(lines) - len(old) + 1):
            if lines[i:i + len(old)] == old:
                seen += 1
                if seen == occ:
                    found = i
                    break
        if found < 0:
            errors.append(f"未匹配(occ={occ}): {old[0][:60]!r}...")
            continue
        lines[found:found + len(old)] = new
        applied += 1
    if applied == 0:
        return {"error": f"0 应用: {errors[:3]}", "applied": 0, "errors": errors}
    # 写回：保留原行尾（I3 修复）
    out = eol.join(lines)
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(out)
    return {"applied": applied, "errors": errors, "file": p, "eol": "CRLF" if eol == "\r\n" else "LF"}




# ---------- ide_rename：安全重命名（建议不落盘） ----------
@tool("ide_rename", "安全重命名：全库找引用→建议（L3 不落盘）", "ide",
      {"type": "object",
       "properties": {
           "root": {"type": "string"},
           "symbol": {"type": "string"},
           "new_name": {"type": "string"},
           "include_plan": {"type": "boolean", "description": "生成批量应用计划（默认 false）"},
       },
       "required": ["root", "symbol", "new_name"]})
def ide_rename(root, symbol, new_name, include_plan=False):
    try:
        root = _fs_resolve(root)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(root):
        return {"error": f"不是目录: {root}"}
    plan = []
    for fp in _iter_files(root, 200):
        src = _read(fp)
        if not src:
            continue
        if symbol in src:
            plan.append({"file": fp, "occurrences": src.count(symbol)})
    return {
        "symbol": symbol, "new_name": new_name,
        "files_affected": len(plan), "total_occurrences": sum(p["occurrences"] for p in plan),
        "plan": plan if include_plan else None,
        "note": "L3 只建议不落盘；确认后可用 fs_write 应用",
    }


# ================= S32：ide_build / ide_debug —— 编译与调试（真实工具链） =================
# 诚实边界：不是内置 VS——Rust 走 cargo check、Python 走 compileall、Go 走 go build，
# 诊断行解析成结构化 {file,line,level,msg}；调试=跑命令抓 panic/traceback 解析成帧。
import subprocess  # noqa: E402
import shutil  # noqa: E402

_RE_CARGO_SHORT = re.compile(
    r"^(.+?):(\d+):(\d+):\s+(error|warning|note)\[?[EW0-9]*\]?:\s+(.*)$")
_RE_PY_FRAME = re.compile(r'File "([^"]+)", line (\d+)(?:, in (\w+))?')
_RE_PY_LAST = re.compile(r"^([A-Za-z_.]+(?:Error|Exception|Interrupt|Exit))(?::\s*(.*))?$")
_RE_RUST_PANIC = re.compile(r"panicked at\s+(?:'([^']*)',\s*)?([^\s:]+):(\d+):(\d+)")
_RE_RUST_FRAME = re.compile(r"^\s+at\s+.+?:(\d+):(\d+)")


def _find_build_root(path, markers):
    """path 向上找最近的构建标记（Cargo.toml / go.mod）。"""
    p = os.path.abspath(path)
    while True:
        for m in markers:
            if os.path.isfile(os.path.join(p, m)):
                return p
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


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


@tool("ide_build", "编译/静态检查：Rust=cargo check、Python=compileall 语法检查、"
      "Go=go build → 结构化诊断 {file,line,level,msg}", "ide",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "项目目录（含 Cargo.toml/go.mod/或 .py）"},
           "action": {"type": "string", "enum": ["check", "test"],
                      "description": "check=诊断；test=Rust 走 cargo test"},
           "timeout": {"type": "integer", "description": "秒（默认 600）"},
       },
       "required": ["path"]})
def ide_build(path, action="check", timeout=600):
    try:
        path = _fs_resolve(path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(path):
        return {"error": f"不是目录: {path}"}
    exe = shutil.which("cargo")
    build_root = _find_build_root(path, ("Cargo.toml",))
    if exe and build_root:
        cmd = [exe, "check", "--message-format=short"]
        if action == "test":
            cmd = [exe, "test", "--no-run", "--message-format=short"]
        try:
            r = subprocess.run(cmd, cwd=build_root, capture_output=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"error": f"超时（{timeout}s）", "tool": "cargo"}
        out = (r.stdout or b"").decode(errors="replace") + "\n" + \
              (r.stderr or b"").decode(errors="replace")
        diags = _parse_cargo_short(out, build_root)
        return {"tool": "cargo", "action": action, "build_root": build_root,
                "exit": r.returncode, "ok": r.returncode == 0,
                "total": len(diags),
                "errors": [d for d in diags if d["level"] == "error"],
                "warnings": [d for d in diags if d["level"] == "warning"][:50]}
    if os.path.isfile(os.path.join(path, "go.mod")) and shutil.which("go"):
        try:
            r = subprocess.run(["go", "build", "./..."], cwd=path,
                               capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"error": f"超时（{timeout}s）", "tool": "go"}
        out = (r.stderr or b"").decode(errors="replace")
        return {"tool": "go", "exit": r.returncode, "ok": r.returncode == 0,
                "raw_tail": out[-2000:]}
    # Python：compileall 语法检查（找 path 下首个 .py 的目录语义由用户保证）
    py = shutil.which("python") or shutil.which("py")
    if py is None:
        return {"error": "无可识别构建目标（Cargo.toml/go.mod）且无 python"}
    try:
        r = subprocess.run([py, "-m", "compileall", "-q", path],
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"超时（{timeout}s）", "tool": "compileall"}
    # compileall 的语法错误走 stdout（不是 stderr）——两路都收
    out = ((r.stdout or b"").decode(errors="replace") + "\n" +
           (r.stderr or b"").decode(errors="replace"))
    diags = []
    for m in _RE_PY_FRAME.finditer(out):
        fp, ln = m.group(1), int(m.group(2))
        diags.append({"file": os.path.abspath(fp), "line": ln, "level": "error",
                      "msg": "syntax"})
    return {"tool": "compileall", "exit": r.returncode, "ok": r.returncode == 0,
            "total": len(diags), "errors": diags, "raw_tail": out[-1500:]}


def _parse_py_traceback(text):
    frames = [{"file": os.path.abspath(f), "line": int(n), "fn": fn or "?"}
              for f, n, fn in _RE_PY_FRAME.findall(text)]
    last = ""
    for line in text.splitlines():
        m = _RE_PY_LAST.match(line.strip())
        if m:
            last = f"{m.group(1)}: {m.group(2) or ''}".strip()
    return frames, last


def _parse_rust_panic(text):
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = _RE_RUST_PANIC.search(line)
        if m:
            msg, f, ln, col = m.groups()
            if not msg and i + 1 < len(lines):
                msg = lines[i + 1].strip()
            frames = []
            for j in range(i, min(i + 40, len(lines))):
                fm = _RE_RUST_FRAME.match(lines[j])
                if fm:
                    frames.append({"line": int(fm.group(1)), "col": int(fm.group(2))})
            out.append({"msg": (msg or "")[:200], "file": f, "line": int(ln),
                        "col": int(col), "backtrace": frames[:12]})
    return out


@tool("ide_debug", "调试捕获：跑命令（argv 列表，不走 shell）→ 解析 Rust panic/"
      "Python traceback 为结构化帧（RUST_BACKTRACE 自动开）", "ide",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "工作目录（沙盒内）"},
           "cmd": {"type": "array", "items": {"type": "string"},
                   "description": "命令 argv 列表，如 [\"cargo\",\"test\"] 或 [\"python\",\"x.py\"]"},
           "timeout": {"type": "integer", "description": "秒（默认 300）"},
       },
       "required": ["path", "cmd"]})
def ide_debug(path, cmd, timeout=300):
    try:
        path = _fs_resolve(path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(path):
        return {"error": f"不是目录: {path}"}
    if (not isinstance(cmd, list) or not cmd
            or not all(isinstance(x, str) for x in cmd)):
        return {"error": "cmd 必须是非空 argv 字符串列表（不走 shell，防注入）"}
    env = dict(os.environ)
    env["RUST_BACKTRACE"] = "1"
    try:
        r = subprocess.run(cmd, cwd=path, capture_output=True, timeout=timeout,
                           env=env)
    except subprocess.TimeoutExpired:
        return {"error": f"超时（{timeout}s）", "cmd": cmd}
    except OSError as e:
        return {"error": f"无法启动: {e}", "cmd": cmd}
    out = (r.stdout or b"").decode(errors="replace")
    err = (r.stderr or b"").decode(errors="replace")
    text = out + "\n" + err
    py_frames, py_last = _parse_py_traceback(text)
    rust = _parse_rust_panic(text)
    return {"cmd": cmd, "exit": r.returncode, "ok": r.returncode == 0,
            "python": {"frames": py_frames, "last_error": py_last} if py_frames else None,
            "rust_panics": rust,
            "raw_tail": text[-2500:]}


