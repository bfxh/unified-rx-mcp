# -*- coding: utf-8 -*-
"""tools/scan.py —— 扫描域（5 工具）：bug_scan / std_check / ui_check / bug_locate / project_scan

收敛自旧版 vuln_scan/scan_all/scan_now/scan_delta → project_scan 组合。
P3 增强（2026-08-24）：Rust 生产规则、ui_check 三引擎。
P4 增强（2026-08-24）：Bevy 专项规则（用户：引擎重点优化 Bevy）。
P5 修复（2026-08-25）：Python AST 作用域感知（参数/方法/属性/魔法方法不算未定义）——
  消除 undefined_name 假阳性（592→0 级）；bug_locate 提取 traceback 文件:行号。
"""
import os
import re
import ast
import subprocess
from collections import Counter, defaultdict

from tools.fs import _resolve as _fs_resolve

_SKIP_DIRS = ('.git', 'node_modules', 'target', '__pycache__', 'dist', 'build',
              '.unified-rx-index', 'backups')

import registry  # S55 同类修复：code_review 的 bug_scan 透镜用 registry.call 却没导入，
                 # NameError 被 except 吞掉——S44 起该透镜从未真正运行过
from registry import tool
from . import bevy  # Bevy 专项规则

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


# ---------- bug_scan：Python AST 规则（P5：作用域感知） ----------
import builtins as _builtins
# 常见内建名（用 builtins 模块，__builtins__ 在模块/__main__ 表现不同）
_BUILTINS = set(dir(_builtins))
# 方法属性访问/魔法方法/参数名不是"未定义变量"
_SPECIAL = {"self", "cls", "super", "_", "__file__", "__name__", "__doc__",
           "__package__", "__loader__", "__spec__", "__builtins__", "__cached__",
           "__annotations__", "__all__", "__path__", "__main__"}


def _scan_python(src, path):
    issues = []
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [{"line": e.lineno or 0, "rule": "syntax_error",
                 "msg": f"语法错误: {e.msg}", "file": path}]
    # 收集所有定义：函数/类名、赋值、导入、参数、推导式变量、with-as、except-as、for 变量
    defined = set()
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defined.add(node.name)
            # 参数也算定义
            args = node.args
            for a in list(args.args) + list(args.kwonlyargs) + list(args.posonlyargs):
                defined.add(a.arg)
            if args.vararg:
                defined.add(args.vararg.arg)
            if args.kwarg:
                defined.add(args.kwarg.arg)
        elif isinstance(node, ast.ClassDef):
            # S5 修复：ClassDef 没有 .args——此前带装饰器/类的 Python 文件直接 AttributeError 全扫崩
            defined.add(node.name)
            for base in node.bases:
                for t in ast.walk(base):
                    if isinstance(t, ast.Name):
                        defined.add(t.id)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                imported[a.asname or a.name.split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                imported[a.asname or a.name] = node.lineno
        elif isinstance(node, ast.comprehension):
            # 推导式变量（支持元组解包 (a, b) for ...）
            for t in ast.walk(node.target):
                if isinstance(t, ast.Name):
                    defined.add(t.id)
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if isinstance(item.optional_vars, ast.Name):
                    defined.add(item.optional_vars.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            # Python 3.11+ ExceptHandler.name 是 str（不是 Name 节点）
            defined.add(node.name)
        elif isinstance(node, ast.Lambda):
            # lambda 参数（如 sort 的 key=lambda x: ...）
            for a in list(node.args.args) + list(node.args.kwonlyargs):
                defined.add(a.arg)
        elif isinstance(node, ast.Global):
            defined.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            defined.update(node.names)
    defined |= set(imported.keys())
    defined |= _BUILTINS
    defined |= _SPECIAL

    for node in ast.walk(tree):
        # 裸 except
        if isinstance(node, ast.ExceptHandler) and node.type is None:
            issues.append({"line": getattr(node, "lineno", 0), "rule": "bare_except",
                           "msg": "裸 except（吞掉所有异常）", "file": path})
        # S61 动态执行（AST 级）：只查裸 Name 调用——re.compile 等 Attribute 成员
        # 调用天然排除（S44 的 dsml FP 教训在 AST 层的结构化解法）
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id in ("eval", "exec", "compile"):
                hot = fn.id in ("eval", "exec")
                issues.append({
                    "line": node.lineno, "rule": "eval_exec",
                    "msg": f"python 动态执行 {fn.id}()——注入面（裸调用）",
                    "file": path,
                    "severity": "high" if hot else "medium",
                    "kind": "definite" if hot else "clue"})
        # 未定义变量：只查 Load 上下文的 Name，且：
        #   - 是属性访问的一部分（node.xxx 的 xxx 不是 Name 节点，天然排除）
        #   - 方法调用 self.xxx 的 xxx 是 Attribute，排除
        #   - 函数调用 foo(...) 的 foo 若是 Name 且未定义 → 报（真未定义函数）
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            # 排除: 函数定义名、类名（已在 defined）
            # 排除: 作为 Attribute 的 value（a.b 的 a 要定义，b 不是 Name）
            # 排除: 关键字参数名（kwarg.arg 是 str）
            if node.id not in defined:
                issues.append({"line": node.lineno, "rule": "undefined_name",
                               "msg": f"未定义变量 '{node.id}'", "file": path})
    # 导入遮蔽内建
    for name, lineno in imported.items():
        if name in _BUILTINS:
            issues.append({"line": lineno, "rule": "redefined_import",
                           "msg": f"导入 '{name}' 遮蔽内建名", "file": path})
    return issues


# ---------- bug_scan：Rust 生产规则 ----------
# S4-D1 分级重构（L2 实测依据：文本密度与真缺陷修复无正相关）：
#   - 文本线索类（unwrap/expect/as_cast/indexing）降为 info/clue——只当"可能有雷的位置"
#   - 跨函数可判定/确定崩溃类保留 high（todo!/unimplemented!/panic!/unreachable!）
#   - 新增 kind 字段：clue=线索流（不当质量分数用），definite=确定性风险
_RUST_RULES = [
    ("unwrap", r"\.unwrap\(\)", "unwrap()——None/Err 时 panic（线索：确认有 ?/match 兜底即可忽略）", "info", "clue"),
    ("expect", r"\.expect\(\s*\"", "expect()——带消息 panic（线索）", "info", "clue"),
    ("panic", r"\bpanic!\(", "panic!()——直接崩溃", "high", "definite"),
    ("unreachable", r"\bunreachable!\(", "unreachable!()——到达即 bug", "high", "definite"),
    ("todo_unimplemented", r"\b(todo!|unimplemented!)\(", "todo!/unimplemented!()——未实现即崩溃", "high", "definite"),
    ("as_cast", r"\bas\s+(i64|i32|u64|u32|f64|f32|usize|isize)\b", "as 类型转换——截断/精度丢失（线索：建议 try_from）", "info", "clue"),
    ("indexing", r"\[[a-zA-Z_][a-zA-Z0-9_]*\]", "索引访问——越界即 panic（线索：建议 .get()）", "info", "clue"),
    # S27 人工标注审计发现：[expr.field as usize] 成员+转换索引此前全部漏报
    ("indexing", r"\[[^\]\[\n]{0,80}\bas\s+(usize|isize|i64|i32|u64|u32)\s*\]", "索引访问（含 as 转换）——越界即 panic（线索：建议 .get()）", "info", "clue"),
]


def _scan_rust(src, path):
    issues = []
    for rule, pat, msg, sev, kind in _RUST_RULES:
        for m in re.finditer(pat, src):
            line = src.count("\n", 0, m.start()) + 1
            line_text = src.split("\n")[line - 1].strip()
            if line_text.startswith("//"):
                continue
            issues.append({"line": line, "rule": rule, "msg": msg, "file": path,
                           "severity": sev, "kind": kind})
    # Bevy 代码规则（Rust 文件里也扫；kind 统一补 clue——迁移类文本规则不当质量分数）
    for rule, pat, msg, sev in bevy.bevy_rules():
        for m in re.finditer(pat, src):
            line = src.count("\n", 0, m.start()) + 1
            issues.append({"line": line, "rule": rule, "msg": msg, "file": path,
                           "severity": sev, "kind": "clue"})
    # S4-D1 测试代码降级：tests 目录/文件名 *_test.rs 整个降；
    # 文件内 #[cfg(test)] mod 之后（tests mod 起）的 clue 类命中按行号逐条降级
    norm = path.replace("\\", "/").replace("_tmp/", "")
    is_test_file = norm.endswith("_test.rs") or re.search(r"/tests(?:/|$)", norm)
    m_test_mod = re.search(r"^#\[\s*cfg\s*\(\s*test\s*\)\s*\]", src, re.MULTILINE)
    test_start_line = None
    if not is_test_file and m_test_mod is not None:
        # cfg(test) 属性行起（mod tests 紧随其后）视为测试区起点
        test_start_line = src.count("\n", 0, m_test_mod.start()) + 1
    for i in issues:
        in_test = is_test_file or (test_start_line is not None and i["line"] >= test_start_line)
        if in_test and i["rule"] in ("unwrap", "expect", "as_cast", "indexing"):
            i["severity"] = "low"
            i["kind"] = "clue"
            i["msg"] += "（测试代码，降级）"
        elif in_test and i["rule"] == "panic":
            # S12 语境诚实化：panic! 在测试内通常就是断言/should_panic 用途。
            # VF3 实证：21/21 个 high panic 全在测试区——高危分被测试打爆即失真。
            i["severity"] = "low"
            i["kind"] = "clue"
            i["msg"] += "（测试上下文，通常为断言用途，降级）"
    return issues


# ---------- bug_scan：通用正则规则 ----------
_RE_RULES = [
    ("assert_always_true", r"assert\s+True\b", "恒真断言（永远通过，无意义）"),
    ("equal_float", r"==\s*\d+\.\d+", "浮点相等比较（精度风险）"),
    # (?<![.\w]) 排除成员调用：RegExp.prototype.exec(/x/) 是正则方法不是动态执行
    # （源码审计实测误报：lib/dsml-tool-call.js 的 10 处全是 regex.exec）
    ("eval_exec", r"(?<![.\w])(eval|exec|execSync)\s*\(", "eval/exec 动态执行（安全风险）"),
]


def _scan_generic(src, path, lang):
    issues = []
    for rule, pat, msg in _RE_RULES:
        for m in re.finditer(pat, src):
            line = src.count("\n", 0, m.start()) + 1
            issues.append({"line": line, "rule": rule, "msg": msg, "file": path,
                           "severity": "medium", "kind": "clue"})
    return issues


# ---------- S5-C2 内容指纹缓存 ----------
# 文件级指纹（mtime_ns + size）→ 该文件上次扫描 issues。
# 未变文件直接复用，免重复 open+parse；指纹含 mtime_ns+size 两要素，
# 修改后任一变化即失效（NTFS mtime 精度 100ns，无 stale 风险）。
import threading as _threading

_SCAN_CACHE = {}
_CACHE_LOCK = _threading.Lock()
_CACHE_MAX = 8192


def _file_fingerprint(fp):
    try:
        st = os.stat(fp)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _cached_scan(func, fp, src):
    """单文件扫描的缓存包装。func(src, fp) → issues list。"""
    fp_norm = os.path.normcase(os.path.abspath(fp))
    key = (fp_norm, func.__name__)
    with _CACHE_LOCK:
        hit = _SCAN_CACHE.get(key)
    if hit is not None and hit[0] == _file_fingerprint(fp):
        return hit[1]
    result = func(src, fp)
    with _CACHE_LOCK:
        if len(_SCAN_CACHE) >= _CACHE_MAX:
            _SCAN_CACHE.clear()  # 粗暴防膨胀：工具生命周期内够用
        _SCAN_CACHE[key] = (_file_fingerprint(fp), result)
    return result


def scan_cache_clear():
    """写操作后由调用方清缓存（外部改文件走 fs_write 时 registry 不感知内容）。"""
    with _CACHE_LOCK:
        _SCAN_CACHE.clear()


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
    issues = []
    files_scanned = 0
    for fp in _iter_files(path, max_files):
        lang = _lang_of(fp)
        if not lang:
            continue
        files_scanned += 1
        # S5-C2：按 (文件指纹, 扫描函数) 查缓存——命中则跳过读取与解析
        if lang == "python":
            scan_fn, cache_name = _scan_python, "_scan_python"
        elif lang == "rust":
            scan_fn, cache_name = _scan_rust, "_scan_rust"
        else:
            scan_fn, cache_name = None, "_generic"
        fp_norm = os.path.normcase(os.path.abspath(fp))
        with _CACHE_LOCK:
            cached = _SCAN_CACHE.get((fp_norm, cache_name))
        if cached is not None and cached[0] == _file_fingerprint(fp):
            issues.extend(cached[1])
            continue
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            continue
        if scan_fn is not None:
            file_issues = _cached_scan(scan_fn, fp, src)
        else:
            file_issues = _scan_generic(src, fp, lang)
            with _CACHE_LOCK:
                if len(_SCAN_CACHE) >= _CACHE_MAX:
                    _SCAN_CACHE.clear()
                _SCAN_CACHE[(fp_norm, cache_name)] = (_file_fingerprint(fp), file_issues)
        issues.extend(file_issues)
    by_rule = {}
    by_sev = {}
    for i in issues:
        by_rule[i["rule"]] = by_rule.get(i["rule"], 0) + 1
        sev = i.get("severity", "info")
        by_sev[sev] = by_sev.get(sev, 0) + 1
    # UPGRADE-C1：全量保留交给 registry._clamp 统一分页（tool 内不再私自截断丢信息）
    return {"files": files_scanned, "total": len(issues),
            "by_rule": by_rule, "by_severity": by_sev,
            "issues": issues}


# ---------- std_check ----------
def _std_check_file(src, fp, lang):
    """单文件 std 检查（S5 缓存单元）。"""
    findings = []
    lines = src.split("\n")
    for idx, line in enumerate(lines, 1):
        low = line.lower()
        for w in _PLACEHOLDER_WORDS:
            if w.lower() in low and not line.strip().startswith(("#", "//", "/*", "*")):
                findings.append({"file": fp, "line": idx, "rule": "placeholder",
                                 "msg": f"占位/假数据文字: {w}", "text": line.strip()[:80]})
                break
        m = re.search(r"=\s*(-?\d{3,}|[2-9]\d{2,})\b", line)
        if m and lang in ("rust", "python", "go", "typescript", "javascript", "gdscript"):
            findings.append({"file": fp, "line": idx, "rule": "magic_number",
                             "msg": f"魔法数字: {m.group(1)}", "text": line.strip()[:80]})
    return findings


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
    findings = []
    files_scanned = 0
    for fp in _iter_files(path, max_files):
        lang = _lang_of(fp)
        if not lang:
            continue
        files_scanned += 1
        key = (os.path.normcase(os.path.abspath(fp)), "_std")
        with _CACHE_LOCK:
            cached = _SCAN_CACHE.get(key)
        if cached is not None and cached[0] == _file_fingerprint(fp):
            findings.extend(cached[1])
            continue
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            continue
        file_findings = _std_check_file(src, fp, lang)
        with _CACHE_LOCK:
            _SCAN_CACHE[key] = (_file_fingerprint(fp), file_findings)
        findings.extend(file_findings)
    return {"files": files_scanned, "total": len(findings), "findings": findings}


# ---------- ui_check：多引擎（Bevy 重点/Godot/Unity 死按钮/空容器模式） ----------
_UI_PATTERNS = {
    "bevy": bevy.BEVY_UI_PATTERNS,
    "godot": [
        (r"Button\b[^:]*:\s*$", "Button 信号未连接（疑似死按钮）"),
    ],
    "unity": [
        (r"new\s+Button\s*\([^)]*\)", "运行时 new Button（应引用场景中的实例）"),
    ],
}


@tool("ui_check", "UI 静态检查（Bevy 重点/Godot/Unity 死按钮/空容器模式）", "scan",
      {"type": "object",
       "properties": {"path": {"type": "string"}, "max_files": {"type": "integer"}},
       "required": ["path"]})
def ui_check(path, max_files=MAX_FILES):
    if not os.path.exists(path):
        return {"error": f"路径不存在: {path}"}
    issues = []
    files_scanned = 0
    for fp in _iter_files(path, max_files):
        ext = os.path.splitext(fp)[1].lower()
        if ext == ".rs":
            engine = "bevy"
        elif ext == ".gd":
            engine = "godot"
        elif ext == ".cs":
            engine = "unity"
        else:
            continue
        files_scanned += 1
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            continue
        for pat, msg in _UI_PATTERNS[engine]:
            for m in re.finditer(pat, src, re.MULTILINE):
                line = src.count("\n", 0, m.start()) + 1
                issues.append({"file": fp, "line": line, "rule": "ui_pattern",
                               "msg": msg, "engine": engine})
        # S6：Bevy 死按钮用结构化检测（Marker-Query 跨 system 验证，非同域正则）
        if engine == "bevy":
            from .bevy import find_dead_buttons
            for ln, marker in find_dead_buttons(src):
                issues.append({"file": fp, "line": ln, "rule": "ui_pattern",
                               "msg": f"死按钮：{marker} spawn 后无任何 Query 交互处理",
                               "engine": engine})
    return {"files": files_scanned, "total": len(issues), "issues": issues}


# ---------- bug_locate：报错 → file:line（P5：提取 traceback 文件名:行号） ----------
def _find_in_file(fp, needle, max_hits=3):
    hits = []
    try:
        with open(fp, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return hits
    for idx, line in enumerate(lines, 1):
        if needle in line:
            ctx_start = max(0, idx - 2)
            ctx_end = min(len(lines), idx + 3)
            hits.append({
                "file": fp, "line": idx,
                "snippet": "".join(lines[ctx_start:ctx_end]).strip(),
            })
            if len(hits) >= max_hits:
                break
    return hits


@tool("bug_locate", "报错文本 → 定位 file:line（含上下文片段）", "scan",
      {"type": "object",
       "properties": {
           "error_text": {"type": "string", "description": "报错/traceback 文本"},
           "root": {"type": "string", "description": "可选：搜索根目录（默认当前目录）"},
       },
       "required": ["error_text"]})
def bug_locate(error_text, root=None):
    root = root or os.getcwd()
    if not os.path.isdir(root):
        return {"error": f"root 不是目录: {root}"}
    candidates = []
    # P5：直接提取 traceback 的 File "...x.py", line N（支持多语言）
    for m in re.finditer(r'File\s+"([^"]+\.(?:py|rs|go|ts|js|tsx|jsx|gd|cs|java|kt|rb|php))",\s*line\s+(\d+)', error_text):
        candidates.append((m.group(1), int(m.group(2)), "traceback 精确"))
    # 兜底：普通代码文件名
    if not candidates:
        for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_.]*\.(?:py|rs|go|ts|js|tsx|jsx|gd|cs|java|kt|rb|php))", error_text):
            candidates.append((m.group(1), None, "文件名"))
    # 兜底：报错符号（NameError: 'xxx'）
    if not candidates:
        for m in re.finditer(r"(?:NameError|AttributeError|KeyError|ImportError|Error).*?['\"]([^'\"]+)['\"]", error_text):
            candidates.append((m.group(1), None, "符号"))
    direct = []
    for c, lineno, how in candidates:
        is_code_file = c.endswith((".py", ".rs", ".go", ".ts", ".js", ".gd", ".cs",
                                   ".java", ".kt", ".rb", ".php"))
        if is_code_file:
            # 找该文件名（相对 root 或绝对）
            fpath = c if os.path.isabs(c) else None
            if fpath is None or not os.path.exists(fpath):
                for fp in _iter_files(root, MAX_FILES):
                    if fp.endswith(c):
                        fpath = fp
                        break
            if fpath and os.path.exists(fpath):
                if lineno:
                    direct.append({"file": fpath, "line": lineno, "how": how,
                                   "snippet": _line_ctx(fpath, lineno)})
                else:
                    direct.extend(_find_in_file(fpath, ""))
                    if direct:
                        direct[-1]["how"] = how
        else:
            for fp in _iter_files(root, MAX_FILES):
                hits = _find_in_file(fp, c, max_hits=2)
                for h in hits:
                    h["how"] = f"符号 '{c}'"
                direct.extend(hits)
    seen = set()
    out = []
    for h in direct:
        k = (h["file"], h.get("line"))
        if k in seen:
            continue
        seen.add(k)
        out.append(h)
        if len(out) >= 10:
            break
    return {"candidates": len(out), "hits": out}


def _line_ctx(fpath, lineno, radius=2):
    """取某文件某行的上下文。"""
    try:
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except OSError:
        return ""
    start = max(0, lineno - 1 - radius)
    end = min(len(lines), lineno + radius)
    return "".join(lines[start:end]).strip()


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


def _func_spans(lines, lang):
    """[(name, start_idx, end_idx, params)]——函数跨度与参数数（廉价比 AST 稳）。"""
    pat = _RE_FUNC_START.get(lang)
    if not pat:
        return []
    starts = []
    for i, line in enumerate(lines):
        m = pat.match(line)
        if not m:
            continue
        name = next((g for g in m.groups() if g), None)
        if not name:
            continue
        params = line.count(",") + 1 if "(" in line and ")" in line else 0
        starts.append((name, i, params))
    spans = []
    for j, (name, i, params) in enumerate(starts):
        end = starts[j + 1][1] if j + 1 < len(starts) else min(len(lines), i + _FUNC_LONG * 3)
        # S64：brace 语言的真函数尾 = 函数后第一个列 0 的 "}"——此前跨度
        # 一律到下一个 fn 起点，中间的 struct/常量/注释把行数吹大
        # （VF3 vehicle_compound_parts 真身 59 行被报 82）
        # 仅顶层 fn 适用；嵌套在 mod/class 里的 fn（行首缩进）走原回退
        if lang in ("rust", "go", "javascript") and lines[i][:1] not in (" ", "\t"):
            cap = min(len(lines), i + _FUNC_LONG * 4)
            for k in range(i + 1, cap):
                if lines[k].rstrip() == "}":
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
        for ln, msg in _complexity_findings(lines, lang):
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
      "热点 + TODO；mode=diff 只报改动行（评审补丁）", "scan",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "文件/目录/git 仓库根"},
           "mode": {"type": "string", "enum": ["file", "diff"],
                    "description": "diff=只评审 git 改动行（默认 file）"},
           "base": {"type": "string",
                    "description": "diff 基线（默认 HEAD；可传分支名评审整个 branch）"},
           "max_files": {"type": "integer", "description": "文件上限（默认 60）"},
       },
       "required": ["path"]})
def code_review(path, mode="file", max_files=60, base="HEAD"):
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
    except Exception:
        pass                             # bug_scan 不可用 → 其余透镜照常
    # S49 卫生透镜：重复文件 + 无测试源文件（仅目录模式；diff 评审改动不掺卫生面）
    if mode == "file" and os.path.isdir(path):
        findings.extend(_dup_file_findings(path))
        findings.extend(_untested_findings(path, files))
    by_lens = Counter(f["lens"] for f in findings)
    hot = Counter(f["file"] for f in findings if f["severity"] in ("high", "med"))
    return {"mode": mode, "files": len(files), "total": len(findings),
            "by_lens": dict(by_lens),
            "top_hotspots": [{"file": f, "findings": n} for f, n in
                             hot.most_common(5)],
            "findings": sorted(findings, key=lambda x: (
                {"high": 0, "med": 1, "low": 2, "info": 3}[x["severity"]],
                x["file"], x["line"]))[:200]}
