"""tools/ide_build.py —— 构建面（S48 拆分）。"""
"""tools/ide_build.py —— 构建面（S48 拆分）。"""

import os

import re

import subprocess

import shutil

from registry import tool

from tools.fs import _resolve as _fs_resolve

from tools.ide_common import (_SKIP_DIRS, _parse_cargo_short,
                              _parse_go_build, _RE_PY_FRAME,
                              _parse_gcc)

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

def _collect_src(path, exts, cap=200):
    """目录下按扩展名收集源文件（S44 拍平：walk 双层循环提出来）。"""
    out = []
    for r, dirs, fs in os.walk(path):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in fs:
            if fn.endswith(tuple(e.lower() for e in exts)):
                out.append(os.path.join(r, fn))
    return out[:cap]

def _javac_build(path, timeout):
    """松散 .java → javac 全量语法编译到临时 -d（无 mvn/gradle 时的诚实降级）。"""
    javac = shutil.which("javac")
    if not javac:
        return {"error": "未找到 javac（JDK 未安装或不在 PATH）"}
    files = _collect_src(path, (".java",))
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
    files = _collect_src(path, exts)
    if not files:
        return {"error": f"目录内无 {'C++' if cxx else 'C'} 源文件"}
    env = dict(os.environ)
    env["LC_ALL"] = "C"                   # gcc 本地化消息强制英文
    diags = []
    for fp in files[:200]:
        try:
            r = subprocess.run([exe, "-fsyntax-only", fp], cwd=path,
                               capture_output=True, timeout=max(30, timeout // 4),
                               env=env)
        except subprocess.TimeoutExpired:
            continue
        diags.extend(_parse_gcc(
            (r.stderr or b"").decode(errors="replace"), path))
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
           "watch": {"type": "boolean",
                     "description": "watch=轮询指纹变化自动重跑（输出每轮诊断，适合修错循环）"},
           "watch_poll": {"type": "integer", "description": "watch 轮询间隔秒（默认 5）"},
           "timeout": {"type": "integer", "description": "秒（默认 600）"},
       },
       "required": ["path"]},
      requires_auth=True)
def ide_build(path, action="check", timeout=600, watch=False, watch_poll=5):
    if watch:
        return _build_watch(path, action, timeout, watch_poll)
    """S44 ponytail：薄调度器 + 每语言构建器（原 95 行单体拆分）。"""
    try:
        path = _fs_resolve(path)
    except ValueError as e:
        return {"error": str(e)}
    if not os.path.isdir(path):
        return {"error": f"不是目录: {path}"}
    build_root = _find_build_root(path, ("Cargo.toml",))
    if build_root:
        return _build_rust(path, build_root, action, timeout)
    if os.path.isfile(os.path.join(path, "go.mod")):
        return _build_go(path, timeout)
    if _has_ext(path, (".cpp", ".cc", ".cxx")):
        return _cc_build(path, timeout, cxx=True)
    if _has_ext(path, (".c",)):
        return _cc_build(path, timeout, cxx=False)
    if _has_ext(path, (".java",)):
        return _javac_build(path, timeout)
    return _build_python(path, timeout)

def _build_watch(path, action, timeout, poll):
    """S50：watch 模式——指纹变化即重跑，输出增量诊断。Ctrl+C/超时退出。"""
    import time
    last = None
    rounds = 0
    while rounds < 120:
        result = ide_build(path, action, timeout)
        sig = result.get("total", 0)
        if last is None or sig != last:
            print(json.dumps({"round": rounds, "total": sig,
                              "errors": len(result.get("errors") or [])},
                             ensure_ascii=False), flush=True)
            last = sig
        time.sleep(poll)
        rounds += 1
    return {"error": "watch 轮询上限（120 轮）", "rounds": rounds}


def _has_ext(path, exts):
    for _r, _d, fs in os.walk(path):
        for fn in fs:
            if fn.lower().endswith(exts):
                return True
    return False

def _build_rust(path, build_root, action, timeout):
    exe = shutil.which("cargo")
    if not exe:
        return {"error": "Cargo.toml 存在但 cargo 不在 PATH"}
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

def _build_go(path, timeout):
    go = shutil.which("go")
    if not go:
        return {"error": "go.mod 存在但未找到 go 工具链"}
    try:
        r = subprocess.run([go, "build", "./..."], cwd=path,
                           capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"超时（{timeout}s）", "tool": "go"}
    out = (r.stderr or b"").decode(errors="replace")
    diags = _parse_go_build(out, path)
    return {"tool": "go", "exit": r.returncode, "ok": r.returncode == 0,
            "total": len(diags), "errors": diags, "raw_tail": out[-2000:]}

def _build_python(path, timeout):
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
