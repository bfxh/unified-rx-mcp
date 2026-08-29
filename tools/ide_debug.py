# -*- coding: utf-8 -*-
"""tools/ide_debug.py —— 调试/断点面（S48 拆分）。"""
import os
import re
import json
import tempfile
import subprocess
import shutil

from registry import tool
from tools.ide_common import _RE_PY_FRAME, _RE_PY_LAST
from tools.fs import _resolve as _fs_resolve
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

_RE_JAVA_FRAME = re.compile(r"^\s+at\s+([\w$.]+)\(([\w./]+\.java):(\d+)\)", re.M)

_RE_JAVA_LAST = re.compile(
    r'(?:Exception in thread "[^"]*"\s+)?'
    r'([\w.$]+(?:Exception|Error|Throwable))\s*:?\s*(.*)')

_RE_GO_PANIC = re.compile(r"^panic:\s+(.*)$")

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
       "required": ["path", "cmd"]},
      requires_auth=True)
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
            if b.get("cond"):
                try:
                    if not eval(b["cond"], {}, frame.f_locals):
                        continue
                except Exception:
                    continue          # 条件求值失败 = 不命中（宁缺毋假）
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
sys.path.insert(0, os.getcwd())      # S45：用户模块以 cwd 导入（-m 模式必需）
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
       "required": ["path", "cmd", "breakpoints"]},
      requires_auth=True)
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

def _norm_mode_target(cmd, path):
    """S44 拍平：cmd → (mode, target, rest)，-m 模块/path 文件两式。"""
    if len(cmd) > 1 and cmd[1] == "-m":
        target = cmd[2] if len(cmd) > 2 else ""
        if not target:
            return None, None, []
        return "module", target, list(cmd[3:])
    target = cmd[1] if len(cmd) > 1 else ""
    if mode_is_file_ok(path, target):
        return "path", target, list(cmd[2:])
    return None, None, []

def mode_is_file_ok(path, target):
    t_abs = os.path.abspath(
        os.path.join(path, target) if not os.path.isabs(target) else target)
    return os.path.isfile(t_abs)

def _norm_bps(path, breakpoints):
    out = []
    for b in breakpoints:
        bf = b["file"]
        babs = os.path.abspath(os.path.join(path, bf) if not os.path.isabs(bf) else bf)
        entry = {"file": babs, "line": int(b["line"])}
        if b.get("cond"):
            entry["cond"] = str(b["cond"])   # S50：帧 locals 内求值的条件
        out.append(entry)
    return out

def _break_python(path, cmd, breakpoints, max_hits):
    """python settrace 记录器：cmd = [python, target.py, ...] 或 [python, -m, 模块, ...]。"""
    mode, target, rest = _norm_mode_target(cmd, path)
    if mode is None:
        return {"error": f"python 目标不存在或 -m 缺模块名: {cmd[1:3]}"}
    bps = _norm_bps(path, breakpoints)
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
