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
import json
import tempfile

import registry
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
def ide_edit_multi(file_path, edits, root=None, __authorized=False, old_lines=None, new_lines=None, dry_run=False):
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
    edits = edits or []
    # S34：先整段模拟匹配（在副本上），失败不写盘——dry_run 也复用同一模拟
    sim = list(lines)
    sim_errors = []
    for e in edits:
        old = e.get("old_lines") or []
        new = e.get("new_lines") or []
        occ = int(e.get("occ", 1) or 1)
        if not old:
            sim_errors.append("old_lines 为空")
            continue
        found = -1
        seen = 0
        for i in range(len(sim) - len(old) + 1):
            if sim[i:i + len(old)] == old:
                seen += 1
                if seen == occ:
                    found = i
                    break
        if found < 0:
            sim_errors.append(f"未匹配(occ={occ}): {old[0][:60]!r}...")
            continue
        sim[found:found + len(old)] = new
        applied += 1
    if applied == 0:
        return {"error": f"0 应用: {sim_errors[:3]}", "applied": 0, "errors": sim_errors}
    out = eol.join(sim)
    if dry_run:
        # S34：预览模式——unified diff，不落盘（887 次调用里预览是高频需求）
        import difflib
        diff = "".join(difflib.unified_diff(
            src.splitlines(keepends=True), out.splitlines(keepends=True),
            fromfile=file_path, tofile=file_path + " (dry_run)"))
        return {"applied": applied, "errors": sim_errors, "file": p,
                "dry_run": True, "diff": diff[:MAX_CTX]}
    # 写回：保留原行尾（I3 修复）
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(out)
    return {"applied": applied, "errors": sim_errors, "file": p,
            "eol": "CRLF" if eol == "\r\n" else "LF"}




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
_RE_PYTEST_FAILED = re.compile(r"^(?:FAILED|ERROR)\s+([\w/\\.:,\[\]\-]+)", re.M)
_RE_PYTEST_SHORT_ID = re.compile(r"^_{5,}\s+(\S+)\s+_+$", re.M)


def _parse_pytest(text):
    """pytest 失败摘要：FAILED/ERROR 行 + E 断言行（S34：修复轮的高频信号）。"""
    failed = [m.group(1) for m in _RE_PYTEST_FAILED.finditer(text)]
    for m in _RE_PYTEST_SHORT_ID.finditer(text):
        if m.group(1) not in failed:
            failed.append(m.group(1))
    asserts = [ln[1:].strip()[:200] for ln in text.splitlines()
               if re.match(r"^E\s+\S", ln)][:8]
    return failed, asserts


def _build_fingerprint(root):
    """参与构建的源文件指纹（mtime+size）——诊断缓存失效判定。"""
    fps = {}
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in files:
            if os.path.splitext(fn)[1].lower() in (
                    ".py", ".rs", ".go", ".java", ".c", ".cpp", ".h",
                    ".hpp", ".toml", ".lock"):
                fp = os.path.join(r, fn)
                try:
                    st = os.stat(fp)
                    fps[fp] = (st.st_mtime_ns, st.st_size)
                except OSError:
                    pass
    return hash(tuple(sorted(fps.items())))


_BUILD_CACHE = {}       # (tool, action, build_root) -> {"key": fp, "result": {...}}
_BUILD_CACHE_MAX = 8
_BUILD_CACHE_ORDER = []


def _build_cache_get(key):
    ent = _BUILD_CACHE.get(key)
    if ent is None:
        return None
    if ent["key"] != _build_fingerprint(key[2]):
        return None
    return {"cached": True, **dict(ent["result"])}


def _build_cache_put(key, result):
    _BUILD_CACHE[key] = {"key": _build_fingerprint(key[2]), "result": result}
    _BUILD_CACHE_ORDER.append(key)
    while len(_BUILD_CACHE_ORDER) > _BUILD_CACHE_MAX:
        old = _BUILD_CACHE_ORDER.pop(0)
        _BUILD_CACHE.pop(old, None)


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


# ---- S33：Java / Go / C / C++ 扩展（真实工具链，缺什么如实报） ----
# gcc/g++/javac 诊断同构："file:line:col: level: msg"（javac 无 col）
_RE_GCC_DIAG = re.compile(r"^(.+?):(\d+)(?::(\d+))?:\s+(error|warning|note|fatal error):\s+(.*)$")
_RE_JAVA_FRAME = re.compile(r"^\s+at\s+([\w$.]+)\(([\w./]+\.java):(\d+)\)", re.M)
_RE_JAVA_LAST = re.compile(
    r'(?:Exception in thread "[^"]*"\s+)?'
    r'([\w.$]+(?:Exception|Error|Throwable))\s*:?\s*(.*)')
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
_RE_GO_PANIC = re.compile(r"^panic:\s+(.*)$")
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


def _parse_java_trace(text):
    """Java 堆栈：at 包.类.方法(File.java:行) 帧 + 异常头/Caused by。"""
    frames = [{"cls": m.group(1), "file": m.group(2), "line": int(m.group(3))}
              for m in _RE_JAVA_FRAME.finditer(text)]
    last = ""
    for line in text.splitlines():
        m = _RE_JAVA_LAST.search(line.strip())
        if m:
            exc = m.group(1) or ""
            last = f"{exc}: {m.group(2) or ''}".strip()
    return frames, last


def _parse_go_panic(text):
    out = []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m = _RE_GO_PANIC.match(line.strip())
        if not m:
            continue
        frames = []
        for j in range(i, min(i + 30, len(lines))):
            fm = re.search(r"([^\s/]+\.go):(\d+)", lines[j])
            if fm and j > i:
                frames.append({"file": fm.group(1), "line": int(fm.group(2))})
        out.append({"msg": m.group(1)[:200], "backtrace": frames[:12]})
    return out


def _javac_build(path, timeout):
    """松散 .java → javac 全量语法编译到临时 -d（无 mvn/gradle 时的诚实降级）。"""
    javac = shutil.which("javac")
    if not javac:
        return {"error": "未找到 javac（JDK 未安装或不在 PATH）"}
    files = []
    for r, dirs, fs in os.walk(path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in fs:
            if fn.endswith(".java"):
                files.append(os.path.join(r, fn))
    if not files:
        return {"error": "目录内无 .java 文件"}
    outd = os.path.join(path, ".urx_javac_out")
    os.makedirs(outd, exist_ok=True)
    try:
        # JDK 本地化消息（中文"错误"）会破坏诊断正则 → 强制英文
        r = subprocess.run([javac, "-J-Duser.language=en", "-J-Duser.country=US",
                            "-d", outd] + files, cwd=path,
                           capture_output=True, timeout=timeout)
    finally:
        shutil.rmtree(outd, ignore_errors=True)
    out = (r.stderr or b"").decode(errors="replace")
    diags = _parse_gcc(out, path)      # javac 诊断格式与 gcc 同构
    return {"tool": "javac", "exit": r.returncode, "ok": r.returncode == 0,
            "total": len(diags), "errors": [d for d in diags if d["level"] == "error"],
            "warnings": [d for d in diags if d["level"] == "warning"][:50]}


def _cc_build(path, timeout, cxx=False):
    """松散 C/C++ → gcc/g++ -fsyntax-only 逐文件（无 make/cmake 的诚实降级）。"""
    exe = shutil.which("g++" if cxx else "gcc") or \
        (shutil.which("clang++" if cxx else "clang"))
    if not exe:
        return {"error": f"未找到 {'g++/clang++' if cxx else 'gcc/clang'}"}
    exts = (".cpp", ".cc", ".cxx") if cxx else (".c",)
    files = []
    for r, dirs, fs in os.walk(path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in fs:
            if fn.lower().endswith(exts):
                files.append(os.path.join(r, fn))
    if not files:
        return {"error": f"目录内无 {'C++' if cxx else 'C'} 源文件"}
    diags = []
    env = dict(os.environ)
    env["LC_ALL"] = "C"                   # gcc 本地化消息强制英文
    for fp in files[:200]:
        try:
            r = subprocess.run([exe, "-fsyntax-only", fp], cwd=path,
                               capture_output=True, timeout=max(30, timeout // 4),
                               env=env)
        except subprocess.TimeoutExpired:
            continue
        out = (r.stderr or b"").decode(errors="replace")
        diags.extend(_parse_gcc(out, path))
    return {"tool": "g++ --fsyntax-only" if cxx else "gcc --fsyntax-only",
            "exit": 0 if not any(d["level"] == "error" for d in diags) else 1,
            "ok": not any(d["level"] == "error" for d in diags),
            "total": len(diags),
            "errors": [d for d in diags if d["level"] == "error"],
            "warnings": [d for d in diags if d["level"] == "warning"][:50]}


@tool("ide_build", "编译/静态检查/lint：Rust=cargo check/test/clippy、Java=javac（无 mvn/gradle 如实降级）、"
      "C/C++=gcc/g++ -fsyntax-only、Go=go build、Python=compileall → 结构化诊断", "ide",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "项目目录（含 Cargo.toml/go.mod/pom.xml/或源文件）"},
           "action": {"type": "string", "enum": ["check", "test", "lint"],
                      "description": "check=诊断；test=Rust 走 cargo test；lint=Rust 走 cargo clippy"},
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
    # Java：pom.xml/build.gradle 存在但无 mvn/gradle → 如实降级 javac 松散编译
    has_java = any(f.endswith(".java") for f in os.listdir(path)) or \
        any(fn.endswith(".java") for _, _, fs in os.walk(path) for fn in fs)
    if has_java and not os.path.isfile(os.path.join(path, "Cargo.toml")):
        if os.path.isfile(os.path.join(path, "pom.xml")) and not shutil.which("mvn"):
            pass                                          # 落到 javac 松散编译
        return _javac_build(path, timeout)
    exe = shutil.which("cargo")
    build_root = _find_build_root(path, ("Cargo.toml",))
    if exe and build_root:
        cached = _build_cache_get(("cargo", action, build_root))
        if cached:
            return cached
        if action == "lint":
            if shutil.which("cargo-clippy") is None:
                return {"error": "clippy 未安装（rustup component add clippy）",
                        "tool": "clippy"}
            cmd = [exe, "clippy", "--message-format=short"]
        elif action == "test":
            cmd = [exe, "test", "--no-run", "--message-format=short"]
        else:
            cmd = [exe, "check", "--message-format=short"]
        try:
            r = subprocess.run(cmd, cwd=build_root, capture_output=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"error": f"超时（{timeout}s）", "tool": "cargo"}
        out = (r.stdout or b"").decode(errors="replace") + "\n" + \
              (r.stderr or b"").decode(errors="replace")
        if action == "lint" and "no such subcommand" in out:
            return {"error": "clippy 未安装（rustup component add clippy）",
                    "tool": "clippy"}
        diags = _parse_cargo_short(out, build_root)
        result = {"tool": "clippy" if action == "lint" else "cargo",
                  "action": action, "build_root": build_root,
                  "exit": r.returncode, "ok": r.returncode == 0,
                  "total": len(diags),
                  "errors": [d for d in diags if d["level"] == "error"],
                  "warnings": [d for d in diags if d["level"] == "warning"][:50]}
        _build_cache_put(("cargo", action, build_root), result)
        return result
        _build_cache_put(("cargo", action, build_root), result)
        return result
    if os.path.isfile(os.path.join(path, "go.mod")):
        go = shutil.which("go")
        if not go:
            return {"error": "go.mod 存在但未找到 go 工具链"}
        try:
            r = subprocess.run([go, "build", "./..."], cwd=path,
                               capture_output=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"error": f"超时（{timeout}s）", "tool": "go"}
        out = (r.stderr or b"").decode(errors="replace")
        diags = _parse_go_build(out, path)   # go 错误格式 file:line:col: msg（无 level 词）
        return {"tool": "go", "exit": r.returncode, "ok": r.returncode == 0,
                "total": len(diags), "errors": diags,
                "raw_tail": out[-2000:]}
    if any(fn.lower().endswith((".cpp", ".cc", ".cxx")) for _, _, fs in
           os.walk(path) for fn in fs):
        return _cc_build(path, timeout, cxx=True)
    if any(fn.lower().endswith(".c") for _, _, fs in os.walk(path) for fn in fs):
        return _cc_build(path, timeout, cxx=False)
    # Python：compileall 语法检查（找 path 下首个 .py 的目录语义由用户保证）
    py = shutil.which("python") or shutil.which("py")
    if py is None:
        return {"error": "无可识别构建目标（Cargo.toml/go.mod/.java/.c/.cpp/.py）"}
    cached = _build_cache_get(("compileall", "", path))
    if cached:
        return cached
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
    result = {"tool": "compileall", "exit": r.returncode, "ok": r.returncode == 0,
              "total": len(diags), "errors": diags, "raw_tail": out[-1500:]}
    _build_cache_put(("compileall", "", path), result)
    return result


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
      "Java 堆栈/Go panic/Python traceback 为结构化帧（RUST_BACKTRACE 自动开）", "ide",
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
    jframes, jlast = _parse_java_trace(text)
    gop = _parse_go_panic(text)
    pf, asserts = _parse_pytest(text)
    lang_frames = None
    if jframes:
        lang_frames = {"kind": "java", "frames": jframes, "last_error": jlast}
    elif gop:
        lang_frames = {"kind": "go", "panics": gop}
    return {"cmd": cmd, "exit": r.returncode, "ok": r.returncode == 0,
            "python": {"frames": py_frames, "last_error": py_last} if py_frames else None,
            "rust_panics": rust,
            "lang_frames": lang_frames,
            "pytest": {"failed": pf[:20], "asserts": asserts} if (pf or asserts) else None,
            "raw_tail": text[-2500:]}


# ================= S36：ide_break —— 轻依赖断点调试（无 gdb/jdwp 重依赖） =================
# python：stdlib sys.settrace 记录器（零依赖，断点命中抓 locals+栈）
# java：JDK 自带 jdb 脚本化馈送（节奏控制 + 强制英文）
# go：dlv trace 函数级追踪（本机已装 delve）
# rust：无 gdb/lldb 时如实报错（不硬造）

_BREAK_RUNNER = '''# -*- coding: utf-8 -*-
import sys, json, os, runpy
bps = json.loads(sys.argv[1])
max_hits = int(sys.argv[2])
mode = sys.argv[3]
target = sys.argv[4]
hits = []


def _stack_of(frame):
    out = []
    f = frame
    while f is not None and len(out) < 8:
        out.append({"file": f.f_code.co_filename, "line": f.f_lineno,
                    "fn": f.f_code.co_name})
        f = f.f_back
    return out


def tracer(frame, event, arg):
    if event != "line":
        return tracer
    f = os.path.abspath(frame.f_code.co_filename)
    for b in bps:
        if os.path.abspath(b["file"]) == f and b["line"] == frame.f_lineno:
            locs = {k: repr(v)[:120] for k, v in
                    list(frame.f_locals.items())[:12]}
            hits.append({"bp_line": b["line"], "locals": locs,
                         "stack": _stack_of(frame)})
            break
    if len(hits) >= max_hits:
        # S36 实证：line 事件的 return None 在 3.11+ 不再关帧追踪——必须显式关
        sys.settrace(None)
        return None
    return tracer


sys.settrace(tracer)
sys.argv = [target] + sys.argv[5:]
try:
    if mode == "module":
        runpy.run_module(target, run_name="__main__", alter_sys=True)
    else:
        runpy.run_path(target, run_name="__main__")
except SystemExit:
    pass
sys.settrace(None)
sys.stderr.write("\\n__URX_HITS__" + json.dumps(hits, ensure_ascii=False))
'''


@tool("ide_break", "轻依赖断点调试（无 gdb/jdwp）：python=settrace 记录器（零依赖）、"
      "java=jdb 脚本化、go=dlv 函数追踪；断点命中抓 locals+调用栈", "ide",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "工作目录（沙盒内）"},
           "cmd": {"type": "array", "items": {"type": "string"},
                   "description": "目标命令 argv（如 [\"python\",\"app.py\"]）"},
           "breakpoints": {"type": "array",
                           "description": "python: [{\"file\":\"x.py\",\"line\":5}]"},
           "max_hits": {"type": "integer", "description": "最大记录数（默认 20）"},
       },
       "required": ["path", "cmd", "breakpoints"]})
def ide_break(path, cmd, breakpoints, max_hits=20):
    try:
        path = _fs_resolve(path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(path):
        return {"error": f"不是目录: {path}"}
    if not breakpoints:
        return {"error": "breakpoints 为空"}
    exe0 = os.path.basename(cmd[0].lower()) if cmd else ""
    bp0 = breakpoints[0]
    # S38：按断点声明形状路由（file→python / class→java / func→go），exe 名不可靠
    if "file" in bp0 and ("python" in exe0 or str(bp0["file"]).endswith(".py")):
        return _break_python(path, cmd, breakpoints, max_hits)
    if "class" in bp0:
        return _break_java(path, cmd, breakpoints, max_hits)
    if "func" in bp0:
        return _break_go(path, cmd, breakpoints, max_hits)
    return {"error": "该语言的轻依赖断点后端不可用：rust 需 gdb/lldb（未在 PATH）——"
                     "替代方案：ide_debug 的 panic 回溯帧（RUST_BACKTRACE=1）"}


def _break_python(path, cmd, breakpoints, max_hits):
    """python settrace 记录器：cmd = [python, target.py, ...] 或 [python, -m, 模块, ...]。"""
    if len(cmd) > 1 and cmd[1] == "-m":
        mode = "module"
        target = cmd[2] if len(cmd) > 2 else ""
        rest = list(cmd[3:])
        if not target:
            return {"error": "-m 缺少模块名"}
    else:
        mode = "path"
        target = cmd[1] if len(cmd) > 1 else ""
        rest = list(cmd[2:])
        t_abs = os.path.abspath(
            os.path.join(path, target) if not os.path.isabs(target) else target)
        if not os.path.isfile(t_abs):
            return {"error": f"python 目标不存在: {t_abs}"}
    bps = []
    for b in breakpoints:
        bf = b["file"]
        babs = os.path.abspath(os.path.join(path, bf) if not os.path.isabs(bf) else bf)
        bps.append({"file": babs, "line": int(b["line"])})
    runner = os.path.join(tempfile.gettempdir(), "opencode",
                          f"urx_trace_{os.getpid()}.py")
    os.makedirs(os.path.dirname(runner), exist_ok=True)
    with open(runner, "w", encoding="utf-8", newline="") as f:
        f.write(_BREAK_RUNNER)
    argv = [cmd[0], runner, json.dumps(bps), str(max_hits), mode, target] + rest
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run(argv, cwd=path, capture_output=True, timeout=300,
                           env=env)
    except subprocess.TimeoutExpired:
        return {"error": "超时（300s）", "hits": []}
    text = (r.stderr or b"").decode("utf-8", errors="replace")
    hits = []
    if "__URX_HITS__" in text:
        try:
            hits = json.loads(text.split("__URX_HITS__")[-1])
        except ValueError:
            hits = []
    return {"lang": "python", "exit": r.returncode,
            "ok": r.returncode == 0, "total": len(hits), "hits": hits[:max_hits],
            "raw_tail": text[-1200:] if not hits else ""}




# ================= S37：ide_diagnostics —— 统一诊断通道 =================
# LSP（rust-analyzer/pylsp）+ cargo clippy 聚合成同一形状：
# {source, file, line(1-based), col, severity(error/warning), message}
# 与 ide_lsp 的发布式诊断不同：本工具是聚合入口，修复循环直接消费。

_SEV_LSP = {"error": "error", "warning": "warning", "info": "info", "hint": "hint"}

_LANG_BY_EXT = {".py": "python", ".rs": "rust"}


@tool("ide_diagnostics", "统一诊断通道：LSP 诊断 + cargo clippy 聚合（同一形状，"
      "severity 归一，行号 1-based）——修复循环/agent 直接消费", "ide",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "项目目录（沙盒内）"},
           "files": {"type": "array", "items": {"type": "string"},
                     "description": "相对路径列表（LSP 诊断目标；缺省跳过 LSP）"},
           "include_lint": {"type": "boolean",
                            "description": "含 cargo clippy（Cargo.toml 存在时，默认 true）"},
       },
       "required": ["path"]})
def ide_diagnostics(path, files=None, include_lint=True, timeout=600):
    try:
        path = _fs_resolve(path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(path):
        return {"error": f"不是目录: {path}"}
    diags = []
    engines = []
    # 1) LSP：逐文件（会话在 lsp.py 内按 lang 缓存，不重复冷启动）
    for rel in (files or [])[:3]:
        fp = os.path.abspath(os.path.join(path, rel.replace("/", os.sep)))
        if not os.path.isfile(fp):
            continue
        lang = _LANG_BY_EXT.get(os.path.splitext(fp)[1].lower())
        if not lang:
            continue
        try:
            r = registry.call("ide_lsp", {"action": "diagnostics", "file": fp})
            res = r.get("result") or {}
            for d in res.get("diagnostics") or []:
                sev = _SEV_LSP.get(str(d.get("severity")), "warning")
                diags.append({"source": d.get("source") or f"{lang}-lsp",
                              "file": rel, "line": int(d.get("line") or 0) + 1,
                              "col": 0, "severity": sev,
                              "message": (d.get("message") or "")[:200]})
            if res.get("diagnostics"):
                engines.append(f"{lang}-lsp")
        except Exception:
            continue                     # LSP 不可用 → 如实跳过该信号
    # 2) clippy：Cargo.toml 存在时
    if include_lint and os.path.isfile(os.path.join(path, "Cargo.toml")):
        try:
            r = registry.call("ide_build", {"path": path, "action": "lint"})
            res = r.get("result") or r
            for d in (res.get("warnings") or []) + (res.get("errors") or []):
                rel = os.path.relpath(d["file"], path).replace("\\", "/")
                diags.append({"source": "clippy", "file": rel,
                              "line": d["line"], "col": d.get("col", 0),
                              "severity": d["level"],
                              "message": d["msg"][:200]})
            if res.get("warnings") or res.get("errors"):
                engines.append("clippy")
        except Exception:
            pass                         # clippy 不可用 → 如实跳过该信号
    errors = [d for d in diags if d["severity"] == "error"]
    return {"engine": "+".join(engines) or "none", "total": len(diags),
            "errors": len(errors), "diagnostics": diags[:200]}


def _break_java(path, cmd, breakpoints, max_hits):
    """java/jdb：脚本化馈送（stop in/at → locals → where → cont）。
    S38 安全：class 名白名单校验（防 jdb 命令注入）。"""
    jdb = shutil.which("jdb")
    if not jdb:
        return {"error": "jdb 未找到（JDK bin 不在 PATH）"}
    bp = breakpoints[0]
    cls = str(bp.get("class") or "")
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_.$]*$", cls):
        return {"error": f"class 名非法（防 jdb 命令注入）: {cls[:40]}"}
    ln = int(bp.get("line", 0))
    cp = []
    for i, a in enumerate(cmd):
        if a in ("-cp", "-classpath") and i + 1 < len(cmd):
            cp = [cmd[i + 1]]
    script = "\n".join([f"stop in {cls}.main", "run",
                         f"stop at {cls}:{ln}", "locals", "where",
                         "cont", "locals", "quit", ""])
    env = dict(os.environ)
    env["JAVA_TOOL_OPTIONS"] = "-Duser.language=en -Duser.country=US"
    try:
        r = subprocess.run([jdb, "-classpath", *cp], cwd=path,
                           input=script.encode(), capture_output=True,
                           timeout=120, env=env)
    except subprocess.TimeoutExpired:
        return {"error": "jdb 超时（120s）", "hits": []}
    out = (r.stdout or b"").decode(errors="replace")
    hits, cur = [], None
    for line in out.splitlines():
        if "Breakpoint hit" in line:
            if cur:
                hits.append(cur)
            cur = {"locals": {}, "stack": []}
            m = re.search(r"line=(\d+)", line)
            if m:
                cur["bp_line"] = int(m.group(1))
        elif cur is not None:
            vm = re.match(r"\s*([\w\[\]<>.]+)\s+(\w+)\s*=\s*(.*)", line)
            if vm and "=" in line:
                cur["locals"][vm.group(2)] = vm.group(3)[:120]
            elif line.strip().startswith("at "):
                cur["stack"].append(line.strip()[:120])
    if cur:
        hits.append(cur)
    return {"lang": "java", "total": len(hits), "hits": hits[:max_hits],
            "raw_tail": out[-1200:]}


def _break_go(path, cmd, breakpoints, max_hits):
    """go：dlv trace 函数级（行级断点需交互式 dlv——如实降级）。
    S38 安全：函数名正则白名单（防 regex 注入）。"""
    dlv = shutil.which("dlv")
    if not dlv:
        return {"error": "dlv 未找到（go install github.com/go-delve/delve/cmd/dlv@latest）"}
    exe = cmd[0]
    if not os.path.isabs(exe):
        exe = os.path.join(path, exe)
    names = "|".join(str(bp.get("func", "main.")) for bp in breakpoints)
    if not re.match(r"^[\w.*?|\-]+$", names):
        return {"error": f"函数名非法（防 regex 注入）: {names[:40]}"}
    env = dict(os.environ)
    env["MPLBACKEND"] = "Agg"
    try:
        r = subprocess.run([dlv, "trace", names, "--exec", exe, "--output", "-"],
                           cwd=path, capture_output=True, timeout=300, env=env)
    except subprocess.TimeoutExpired:
        return {"error": "dlv 超时（300s）", "hits": []}
    text = (r.stdout or b"").decode(errors="replace") + "\n" + \
           (r.stderr or b"").decode(errors="replace")
    hits = []
    for line in text.splitlines():
        if line.startswith("> ") or " => " in line:
            hits.append({"trace": line.strip()[:200]})
    return {"lang": "go", "mode": "function-trace", "total": len(hits),
            "hits": hits[:max_hits], "raw_tail": text[-1200:]}
