# -*- coding: utf-8 -*-
"""tools/scan.py —— 扫描域（5 工具）：bug_scan / std_check / ui_check / bug_locate / project_scan

收敛自旧版 vuln_scan/scan_all/scan_now/scan_delta → project_scan 组合。
P3 增强（2026-08-24）：Rust 生产规则、ui_check 三引擎。
P4 增强（2026-08-24）：Bevy 专项规则（用户：引擎重点优化 Bevy）。
P5 修复（2026-08-25）：Python AST 作用域感知（参数/方法/属性/魔法方法不算未定义）——
  消除 undefined_name 假阳性（592→0 级）；bug_locate 提取 traceback 文件:行号。
S82（2026-09-05）：std_check / ui_check / bug_locate Rust 原生化（rx-scan.exe，
  见 rust/src/scan.rs）——Python 侧只留薄壳转调，exe 缺失报清晰错误不静默降级。
S83（2026-09-05）：bug_scan 全量原生化（rust/src/bug.rs + 手写迷你解析器 pyast.rs，
  rx-scan bugscan 子命令）——scan.py 至此四工具皆薄壳；ast_scan 仍 Python（后续轮）。
"""
import os
import re
import json
import subprocess
from collections import Counter, defaultdict

from tools.fs import _resolve as _fs_resolve

_SKIP_DIRS = ('.git', 'node_modules', 'target', '__pycache__', 'dist', 'build',
              '.unified-rx-index', 'backups')

import registry  # S55 同类修复：code_review 的 bug_scan 透镜用 registry.call 却没导入，
                 # NameError 被 except 吞掉——S44 起该透镜从未真正运行过
from registry import tool
# tools/bevy.py 自 S83 起为规则档案：bevy_rules 的正则唯一实现在 rust/src/bug.rs

MAX_FILES = 100

# ---------- 语言探测 ----------
_LANG_BY_EXT = {
    ".py": "python", ".rs": "rust", ".go": "go", ".ts": "typescript",
    ".tsx": "typescript", ".js": "javascript", ".jsx": "javascript",
    ".gd": "gdscript", ".c": "c", ".cpp": "cpp", ".h": "c", ".hpp": "cpp",
    ".cs": "csharp", ".dart": "dart", ".lua": "lua", ".sh": "bash",
    ".java": "java", ".kt": "kotlin", ".php": "php", ".rb": "ruby",
    ".swift": "swift",
}

_PLACEHOLDER_WORDS = ("TODO", "FIXME", "placeholder", "占位", "待实现", "未实现",
                      "lorem", "example.com", "your_name", "xxx", "foo", "bar")


def _iter_files(path, max_files):
    """遍历文件（目录或单文件）。max_files 只计代码文件（有语言映射的）。"""
    if os.path.isfile(path):
        yield path
        return
    if not os.path.isdir(path):
        return
    count = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "target",
                                                "__pycache__", "dist", "build",
                                                ".codegraph", "backups", "assets",
                                                "screenshots", "images", "fonts")]
        for fn in files:
            # 只对代码文件计数（非代码文件直接跳过，不占额度）
            if not _LANG_BY_EXT.get(os.path.splitext(fn)[1].lower(), ""):
                continue
            if count >= max_files:
                return
            yield os.path.join(root, fn)
            count += 1


def _lang_of(path):
    return _LANG_BY_EXT.get(os.path.splitext(path)[1].lower(), "")


# ---------- Rust 薄壳（S82 起）：std_check/ui_check/bug_locate 原生实现在 rx-scan.exe ----------
# 遍历契约（名额只计代码文件/每层文件先行/upcase 序）与手写正则语义见 rust/src/scan.rs。

_RX_SCAN_EXE_NAME = "rx-scan.exe"

# 大文本不走 argv：Windows CreateProcess 命令行上限 32767 UTF-16 码元（代理对
# 最坏翻倍），10000 字符留足余量；argv 传 "-" 时 exe 侧改读 stdin 全文。
_QUERY_ARGV_CAP = 10000


def _rx_scan_exe():
    """定位 rx-scan.exe：UNIFIED_RX_RS_EXE 覆盖 → cargo 目标目录惯例路径。

    与 tools/search.py::_rx_search_exe 同纪律：候选必须是已存在且文件名恰为
    rx-scan.exe 的常规文件（argv 固定前缀、list 形式、无 shell，
    env 覆盖不构成任意命令执行面）。
    """
    cand = []
    override = os.environ.get("UNIFIED_RX_RS_EXE")
    if override:
        cand.append(override)
    tmp = os.environ.get("TEMP", r"C:\Temp")
    cand += [os.path.join(tmp, "rx-rs-target", kind, _RX_SCAN_EXE_NAME)
             for kind in ("release", "debug")]
    for c in cand:
        if os.path.isfile(c) and os.path.basename(c) == _RX_SCAN_EXE_NAME:
            return c
    return None


def _rx_scan_call(argv, stdin_data=""):
    """薄壳转调 rx-scan.exe，返回结果 dict；用法级拒绝 raise ValueError。

    stdin 恒接管（空串即 EOF），子进程绝不继承宿主的协议管道。
    """
    exe = _rx_scan_exe()
    if not exe:
        raise ValueError("rx-scan.exe 不存在——先在 rust/ 下 cargo build --release "
                         "（或设 UNIFIED_RX_RS_EXE 指向现有 exe）")
    try:
        cp = subprocess.run([exe] + argv, capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=120, input=stdin_data or "")
    except subprocess.TimeoutExpired:
        raise ValueError("rx-scan 超时（120s）")
    tail = (cp.stderr or "").strip()[-300:]
    lines = (cp.stdout or "").strip().splitlines()
    if not lines:
        raise ValueError(f"rx-scan 无输出（exit={cp.returncode}）: {tail}")
    try:
        out = json.loads(lines[-1])
    except ValueError:
        raise ValueError(f"rx-scan 输出非 JSON: {lines[-1][:200]}")
    if cp.returncode == 2:
        # 用法级拒绝（缺参数）→ 与 fs/search 壳同走 ValueError 包络
        raise ValueError(out.get("error") if isinstance(out, dict) else lines[-1])
    if cp.returncode != 0:
        raise ValueError(f"rx-scan 执行失败（exit={cp.returncode}）: {tail}")
    return out


# ---------- bug_scan：S83 起全量原生 ----------
# Python AST 规则（P5 作用域感知）/ Rust 生产规则 / 通用正则 / S5-C2 指纹缓存
# 已整体退役——唯一实现在 rust/src/bug.rs（rx-scan bugscan 子命令，含手写迷你
# 解析器 pyast.rs）。语义等价由 S83 对照实验证明：7 场景（语料三配额/单文件/
# 非代码/不存在路径/全仓 169 文件 909 条）与旧实现逐字节一致。
# bevy.py 保留为规则档案（bevy_rules 的正则原文在 Rust 侧 bug.rs 手写匹配器）。


@tool("bug_scan", "静态扫描 bug 模式（未定义变量/裸 except/浮点比较/eval/Rust/Bevy 等）", "scan",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "文件或目录"},
           "max_files": {"type": "integer", "description": "扫描上限（默认 100）"},
       },
       "required": ["path"]})
def bug_scan(path, max_files=MAX_FILES):
    if not os.path.exists(path):
        return {"error": f"路径不存在: {path}"}
    return _rx_scan_call(["bugscan", path, str(int(max_files))])


# ---------- std_check ----------
@tool("std_check", "工程标准检查（占位文字/魔法数字/未使用导入）", "scan",
      {"type": "object",
       "properties": {
           "path": {"type": "string"},
           "max_files": {"type": "integer", "description": "扫描上限（默认 100）"},
       },
       "required": ["path"]})
def std_check(path, max_files=MAX_FILES):
    if not os.path.exists(path):
        return {"error": f"路径不存在: {path}"}
    # S83 起全域不走缓存：exe 每调独立进程，无跨调缓存面（旧 _SCAN_CACHE 已退役）
    return _rx_scan_call(["stdcheck", path, str(int(max_files))])


# ---------- ui_check：多引擎（Bevy 重点/Godot/Unity 死按钮/空容器模式） ----------
@tool("ui_check", "UI 静态检查（Bevy 重点/Godot/Unity 死按钮/空容器模式）", "scan",
      {"type": "object",
       "properties": {"path": {"type": "string"}, "max_files": {"type": "integer"}},
       "required": ["path"]})
def ui_check(path, max_files=MAX_FILES):
    if not os.path.exists(path):
        return {"error": f"路径不存在: {path}"}
    return _rx_scan_call(["uicheck", path, str(int(max_files))])


# ---------- bug_locate：报错 → file:line（P5：提取 traceback 文件名:行号） ----------
@tool("bug_locate", "报错文本 → 定位 file:line（含上下文片段）", "scan",
      {"type": "object",
       "properties": {
           "error_text": {"type": "string", "description": "报错/traceback 文本"},
           "root": {"type": "string", "description": "可选：搜索根目录（默认当前目录）"},
       },
       "required": ["error_text"]})
def bug_locate(error_text, root=None):
    root = root or os.getcwd()
    argv = ["buglocate", root, error_text]
    stdin_data = ""
    if len(error_text) > _QUERY_ARGV_CAP:
        argv[2] = "-"
        stdin_data = error_text
    return _rx_scan_call(argv, stdin_data)


# ---------- project_scan：组合 ----------
@tool("project_scan", "项目级扫描组合：bug_scan + std_check + ui_check 三路", "scan",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "项目根目录"},
           "max_files": {"type": "integer", "description": "扫描上限（默认 100）"},
           "ui": {"type": "boolean", "description": "是否扫 UI（默认 true）"},
       },
       "required": ["path"]})
def project_scan(path, max_files=MAX_FILES, ui=True):
    if not os.path.isdir(path):
        return {"error": f"不是目录: {path}"}
    bug = bug_scan(path, max_files)
    std = std_check(path, max_files)
    ui_r = ui_check(path, max_files) if ui else {"files": 0, "total": 0, "issues": []}
    return {
        "path": path,
        "bug_scan": {"total": bug.get("total", 0), "by_rule": bug.get("by_rule", {}),
                     "by_severity": bug.get("by_severity", {})},
        "std_check": {"total": std.get("total", 0)},
        "ui_check": {"total": ui_r.get("total", 0)},
        "summary": f"bug {bug.get('total', 0)} + std {std.get('total', 0)} + ui {ui_r.get('total', 0)}",
    }
# -*- coding: utf-8 -*-
"""S44：code_review —— 多透镜代码评审（找问题不再单一）+ diff 模式（只报改动）。"""

_RE_SECRET = re.compile(
    r"(?i)(password|passwd|api_?key|secret|token|access_key)\s*[=:]\s*[\"'][^\"']{6,}")
_RE_DANGER = [
    (re.compile(r"\beval\s*\("), "eval 动态执行"),
    (re.compile(r"\bexec\s*\("), "exec 动态执行"),
    (re.compile(r"\bos\.system\s*\("), "os.system shell 调用"),
    (re.compile(r"subprocess\.[a-z_]+\([^)]*shell\s*=\s*True"), "subprocess shell=True"),
    (re.compile(r"\.innerHTML\s*="), "innerHTML 直接赋值（XSS 面）"),
    (re.compile(r"execute\s*\([^)]*[%+]"), "SQL 拼接执行"),
]
_RE_TODO = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")
_RE_FUNC_START = {
    "python": re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)"),
    # S64：pub(crate)/pub(super)/pub(in path) 可见性修饰也要认——此前不认，
    # 函数跨度被错误归给上一个 fn（VF3 sync_wheel_axles 108 行算给了
    # face_from_index，报 134 行假热点）
    "rust": re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)"),
    "go": re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)"),
    "javascript": re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)"),
}
_FUNC_LONG = 80
# 24 空格（6 层）对 try/except 密集的基建代码是常规密度；28（7 层）才是真离群
_NEST_SPACES = 28
_PARAMS_MAX = 6


def _lang_of_file(fp):
    ext = os.path.splitext(fp)[1].lower().lstrip(".")
    return {"py": "python", "rs": "rust", "go": "go", "js": "javascript",
            "ts": "javascript", "jsx": "javascript", "tsx": "javascript"}.get(ext)


def _symbol_spans(lines, lang):
    """[(name, start_idx, end_idx, kind)]——顶层+嵌套符号清单（S70）。

    kind：fn / type（rust struct/enum/trait/impl、python class）。
    end_idx 为 1-based 末行（brace=闭合行；python=body 最后一行）。
    brace 语言（rust/go/js）顶层符号用括号深度回 0 定真尾（S64 沿用）；
    python 用缩进回归定 body 末行；嵌套符号（缩进 def/class）也列出。
    """
    if lang == "python":
        pat = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)|^(\s*)class\s+(\w+)")
    elif lang == "rust":
        pat = re.compile(r"^(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)"
                         r"|^(?:pub(?:\([^)]*\))?\s+)?"
                         r"(?:struct|enum|trait|impl)\s+(\w+)")
    elif lang == "go":
        pat = re.compile(r"^func\s+(?:\([^)]*\)\s*)?(\w+)|^type\s+(\w+)\s*")
    elif lang == "javascript":
        pat = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)"
                         r"|^\s*class\s+(\w+)")
    else:
        return []
    brace_lang = lang in ("rust", "go", "javascript")
    starts = []
    for i, line in enumerate(lines):
        m = pat.match(line)
        if not m:
            continue
        name = m.group(m.lastindex) if m.lastindex else \
            next((g for g in m.groups() if g), None)
        if not name:
            continue
        kind = "fn"
        if (lang == "python" and line.lstrip().startswith("class")) or \
                (lang != "python" and re.search(r"\b(struct|enum|trait|impl)\s+\w", line)):
            kind = "type"
        starts.append((name, i, kind))
    spans = []
    for j, (name, i, kind) in enumerate(starts):
        end = starts[j + 1][1] if j + 1 < len(starts) else \
            min(len(lines), i + _FUNC_LONG * 4)
        if brace_lang and lines[i][:1] not in (" ", "\t"):
            cap = min(len(lines), i + _FUNC_LONG * 6)
            depth = 0
            opened = False
            for k in range(i, cap):
                depth += lines[k].count("{") - lines[k].count("}")
                if "{" in lines[k]:
                    opened = True
                if opened and depth <= 0:
                    end = k + 1
                    break
        elif lang == "python":
            ind = len(lines[i]) - len(lines[i].lstrip())
            last_body = i
            for k in range(i + 1, len(lines)):
                if lines[k].strip():
                    cur = len(lines[k]) - len(lines[k].lstrip())
                    if cur <= ind:
                        break
                    last_body = k
            end = last_body + 1
        spans.append((name, i, end, kind))
    return spans


def _func_spans(lines, lang):
    """[(name, start_idx, end_idx, params)]——函数跨度与参数数（廉价比 AST 稳）。"""
    starts = []
    for i, line in enumerate(lines):
        m = re.match(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+(\w+)",
                     line) if lang == "rust" else None
        if lang == "python":
            m = re.match(r"^(\s*)(?:async\s+)?def\s+(\w+)", line)
        elif lang == "go":
            m = re.match(r"^func\s+(?:\([^)]*\)\s*)?(\w+)", line)
        elif lang == "javascript":
            m = re.match(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(\w+)", line)
        if not m:
            continue
        # S66：python 模式组 1 是缩进、组 2 才是名字——取最后一个参与组
        # （此前缩进 def 的"名字"是缩进串，complexity 报名全错）
        name = m.group(m.lastindex) if m.lastindex else \
            next((g for g in m.groups() if g), None)
        if not name:
            continue
        params = line.count(",") + 1 if "(" in line and ")" in line else 0
        starts.append((name, i, params))
    spans = []
    for j, (name, i, params) in enumerate(starts):
        end = starts[j + 1][1] if j + 1 < len(starts) else min(len(lines), i + _FUNC_LONG * 3)
        # S64：brace 语言的真函数尾 = 括号深度回到 0——此前跨度
        # 一律到下一个 fn 起点，中间的 struct/常量/注释把行数吹大
        # （VF3 vehicle_compound_parts 真身 59 行被报 82）
        # 仅顶层 fn 适用；嵌套在 mod/class 里的 fn（行首缩进）走原回退
        if lang in ("rust", "go", "javascript") and lines[i][:1] not in (" ", "\t"):
            cap = min(len(lines), i + _FUNC_LONG * 4)
            depth = 0
            opened = False
            for k in range(i, cap):
                depth += lines[k].count("{") - lines[k].count("}")
                if "{" in lines[k]:
                    opened = True
                if opened and depth <= 0:
                    end = k + 1
                    break
        spans.append((name, i, end, params))
    return spans


def _complexity_findings(lines, lang):
    out = []
    for name, i, end, params in _func_spans(lines, lang):
        span = end - i
        if span > _FUNC_LONG:
            out.append((i + 1, f"函数 {name} 长 {span} 行（>{_FUNC_LONG}）——复杂度热点"))
        if params > _PARAMS_MAX:
            out.append((i + 1, f"函数 {name} 参数 {params} 个（>{_PARAMS_MAX}）"))
        # S44 括号深度感知：多行调用的续行缩进不是逻辑嵌套（假阳性修正）
        depth = 0
        bracket = 0
        for ln in lines[i:end]:
            if bracket == 0:
                stripped = len(ln) - len(ln.lstrip())
                if ln.strip():
                    depth = max(depth, stripped)
            bracket += ln.count("(") + ln.count("[") + ln.count("{") \
                - ln.count(")") - ln.count("]") - ln.count("}")
            if bracket < 0:
                bracket = 0
        if depth >= _NEST_SPACES:
            out.append((i + 1, f"函数 {name} 嵌套深 {depth} 空格（≥{_NEST_SPACES}）"))
    return out


def _test_mod_regions(lines):
    """rust #[cfg(test)] + mod X { 的区间（到列 0 的 '}'）——测试夹具的
    长函数不是产品复杂度（S65：VF3 测试夹具污染热点清单）。"""
    regions = []
    i = 0
    while i < len(lines) - 1:
        if lines[i].strip() == "#[cfg(test)]" and \
                re.match(r"^\s*mod\s+\w+\s*\{", lines[i + 1]):
            for k in range(i + 1, len(lines)):
                if lines[k].rstrip() == "}":
                    regions.append((i, k))
                    i = k
                    break
        i += 1
    return regions


def _review_file(fp, changed=None):
    """单文件全透镜。changed=改动行区间列表时只报区间内的发现。"""
    lang = _lang_of_file(fp)
    try:
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            src = f.read()
    except OSError:
        return []
    lines = src.split("\n")

    def in_changed(ln):
        return changed is None or any(a <= ln <= b for a, b in changed)

    out = []
    for i, line in enumerate(lines, 1):
        if not in_changed(i):
            continue
        m = _RE_SECRET.search(line)
        if m:
            out.append({"lens": "security", "severity": "high", "file": fp,
                        "line": i, "msg": f"疑似硬编码凭据: {m.group(1)}"})
            continue
        for pat, msg in _RE_DANGER:
            if pat.search(line):
                out.append({"lens": "security", "severity": "high", "file": fp,
                            "line": i, "msg": msg})
                break
        m = _RE_TODO.search(line)
        if m:
            out.append({"lens": "todo", "severity": "info", "file": fp,
                        "line": i, "msg": f"{m.group(1)} 标记"})
    if lang:
        # S65：复杂度透镜跳过测试区——rust #[cfg(test)] mod 与 python 测试/
        # conftest 文件；测试夹具的长函数（def/mount 表构造）不是产品复杂度
        stem = os.path.splitext(os.path.basename(fp))[0]
        is_py_test = lang == "python" and (
            stem.startswith("test_") or stem.endswith("_test") or stem == "conftest")
        skip_regions = _test_mod_regions(lines) if lang == "rust" else []
        for ln, msg in _complexity_findings(lines, lang):
            if is_py_test or any(a <= ln <= b for a, b in skip_regions):
                continue
            if in_changed(ln):
                out.append({"lens": "complexity", "severity": "low", "file": fp,
                            "line": ln, "msg": msg})
    return out


def _dup_file_findings(root):
    """S49 卫生透镜：内容完全相同的重复文件（md5 分组，≥2 成员即报）。
    抓 data/ vs dist/data 式构建残留副本——**全文件类型**（.ron/.json 等数据
    文件正是高发区，不能只看代码文件）。"""
    import hashlib
    groups = defaultdict(list)
    # S49 修正：dist/build 不能跳——构建残留副本正是高发区（用户实测：
    # data/modules.ron 与 dist/data/modules.ron 字节级相同）
    keep_skip = ('.git', 'node_modules', '__pycache__', '.unified-rx-index')
    for r, dirs, fs in os.walk(root):
        dirs[:] = [d for d in dirs if d not in keep_skip]
        for fn in fs:
            fp = os.path.join(r, fn)
            try:
                if os.path.getsize(fp) > 1024 * 1024:
                    continue
                with open(fp, "rb") as f:
                    h = hashlib.md5(f.read()).hexdigest()
                groups[h].append(fp)
            except OSError:
                continue
    out = []
    for h, members in groups.items():
        if len(members) < 2:
            continue
        out.append({"lens": "duplication", "severity": "med",
                    "file": members[0], "line": 0,
                    "msg": "重复文件 ×{}: {}".format(
                        len(members), ", ".join(members[1:])[:160])})
    return out


_TEST_LANG_EXTS = (".py", ".java", ".go")


def _untested_findings(root, files):
    """S49 卫生透镜：有测试约定的语言（py/java/go）源文件无对应测试文件。
    rust 走内联 #[cfg(test)]，文件级约定不适用 → 如实排除。"""
    repo_files = set()
    for r, dirs, fs in os.walk(root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fn in fs:
            repo_files.add(fn.lower())
    out, seen = [], set()
    for fp in files:
        ext = os.path.splitext(fp)[1].lower()
        if ext not in _TEST_LANG_EXTS:
            continue
        stem = os.path.splitext(os.path.basename(fp))[0]
        if stem.startswith("test_") or stem.endswith("_test") or stem == "conftest":
            continue
        cands = [f"test_{stem}{ext}", f"{stem}_test{ext}",
                 f"test_{stem.replace('test_', '')}{ext}"]
        if any(c in repo_files for c in cands):
            continue
        if stem in seen:
            continue
        seen.add(stem)
        out.append({"lens": "coverage", "severity": "med", "file": fp,
                    "line": 0, "msg": f"源文件 {stem} 无对应测试文件"})
    return out[:30]


def _git_changed_ranges(repo, base="HEAD"):
    """git diff <base> 的改动行区间 {abspath: [(start,end)]}。base=HEAD（默认）/分支名。含未跟踪文件。"""
    changed = {}
    untracked = []
    try:
        r = subprocess.run(["git", "-C", repo, "diff", "-U0", "--no-color", base],
                           capture_output=True, timeout=120)
        out = (r.stdout or b"").decode(errors="replace")
        cur = None
        for line in out.splitlines():
            m = re.match(r"^\+\+\+ b/(\S+)", line)
            if m:
                cur = os.path.abspath(os.path.join(repo, m.group(1)))
                continue
            m = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if m and cur:
                start = int(m.group(1))
                n = int(m.group(2) or 1)
                if n:
                    changed.setdefault(cur, []).append((start, start + n - 1))
        r2 = subprocess.run(["git", "-C", repo, "ls-files", "--others",
                             "--exclude-standard"], capture_output=True, timeout=60)
        for f in (r2.stdout or b"").decode(errors="replace").splitlines():
            if f.strip():
                untracked.append(os.path.abspath(os.path.join(repo, f.strip())))
    except (OSError, subprocess.TimeoutExpired):
        pass
    return changed, untracked


@tool("code_review", "多维代码评审：bug 模式 + 安全（硬编码凭据/危险调用）+ 复杂度"
      "热点 + TODO；mode=diff 只报改动行（评审补丁）；lens 只留指定透镜", "scan",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "文件/目录/git 仓库根"},
           "mode": {"type": "string", "enum": ["file", "diff"],
                    "description": "diff=只评审 git 改动行（默认 file）"},
           "base": {"type": "string",
                    "description": "diff 基线（默认 HEAD；可传分支名评审整个 branch）"},
           "max_files": {"type": "integer", "description": "文件上限（默认 60）"},
           "lens": {"type": "string",
                    "description": "只保留指定透镜（bug_scan/security/complexity/"
                                   "todo/duplication/coverage）——大仓评审免截断"},
       },
       "required": ["path"]})
def code_review(path, mode="file", max_files=60, base="HEAD", lens=None):
    try:
        path = _fs_resolve(path)
    except ValueError as e:
        return {"error": str(e)}
    if os.path.isfile(path):
        files, changed, untracked = [os.path.abspath(path)], None, []
    elif os.path.isdir(path):
        files = [os.path.abspath(p) for p in _iter_files(path, max_files)]
        if mode == "diff" and os.path.isdir(os.path.join(path, ".git")):
            changed, untracked = _git_changed_ranges(path, base)
        else:
            changed, untracked = None, []
    else:
        return {"error": f"路径不存在: {path}"}

    findings = []
    for fp in files:
        ch = changed.get(fp) if changed is not None else None
        if changed is not None and ch is None and fp not in untracked:
            continue                     # diff 模式：只评审改动/新增文件
        fch = changed.get(fp) if changed else None
        findings.extend(_review_file(fp, fch))
    # bug_scan 透镜（真扫描器复用）
    target = files[0] if len(files) == 1 else path
    try:
        r = registry.call("bug_scan", {"path": target, "max_files": max_files})
        res = r.get("result") or {}
        for d in res.get("issues") or []:
            fp = os.path.abspath(d["file"])
            ch = changed.get(fp) if changed is not None else None
            if changed is not None and (ch is None or not any(
                    a <= d["line"] <= b for a, b in ch)):
                continue
            findings.append({"lens": "bug_scan", "severity":
                             "high" if d.get("kind") == "definite" else "med",
                             "file": fp, "line": d["line"], "msg": d["msg"][:160]})
    except Exception as e:
        # S72：静默吞异常违反 workflow.md 自己立的规矩——至少留一条协议日志
        registry.notify("warning",
                        f"code_review 的 bug_scan 透镜失败（其余透镜照常）: {type(e).__name__}: {e}")
    # S49 卫生透镜：重复文件 + 无测试源文件（仅目录模式；diff 评审改动不掺卫生面）
    if mode == "file" and os.path.isdir(path):
        findings.extend(_dup_file_findings(path))
        findings.extend(_untested_findings(path, files))
    # S65：lens 过滤——大仓评审免截断（findings 出口有 200 项钳制，
    # 全透镜 468 项时 duplication/coverage 会被挤出）
    if lens:
        findings = [f for f in findings if f["lens"] == lens]
    by_lens = Counter(f["lens"] for f in findings)
    hot = Counter(f["file"] for f in findings if f["severity"] in ("high", "med"))
    if not hot and findings:
        # S65：lens 过滤后可能全是 low/info（如 complexity）——回退按文件计数
        hot = Counter(f["file"] for f in findings)
    return {"mode": mode, "files": len(files), "total": len(findings),
            "by_lens": dict(by_lens), "lens": lens or None,
            "top_hotspots": [{"file": f, "findings": n} for f, n in
                             hot.most_common(5)],
            "findings": sorted(findings, key=lambda x: (
                {"high": 0, "med": 1, "low": 2, "info": 3}[x["severity"]],
                x["file"], x["line"]))[:200]}
