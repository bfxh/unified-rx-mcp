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
import json

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
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
            # 参数也算定义
            args = node.args
            for a in list(args.args) + list(args.kwonlyargs) + list(args.posonlyargs):
                defined.add(a.arg)
            if args.vararg:
                defined.add(args.vararg.arg)
            if args.kwarg:
                defined.add(args.kwarg.arg)
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
    return issues


# ---------- bug_scan：通用正则规则 ----------
_RE_RULES = [
    ("assert_always_true", r"assert\s+True\b", "恒真断言（永远通过，无意义）"),
    ("equal_float", r"==\s*\d+\.\d+", "浮点相等比较（精度风险）"),
    ("eval_exec", r"\b(eval|exec)\s*\(", "eval/exec 动态执行（安全风险）"),
]


def _scan_generic(src, path, lang):
    issues = []
    for rule, pat, msg in _RE_RULES:
        for m in re.finditer(pat, src):
            line = src.count("\n", 0, m.start()) + 1
            issues.append({"line": line, "rule": rule, "msg": msg, "file": path,
                           "severity": "medium", "kind": "clue"})
    return issues


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
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            continue
        if lang == "python":
            issues.extend(_scan_python(src, fp))
        elif lang == "rust":
            issues.extend(_scan_rust(src, fp))
        issues.extend(_scan_generic(src, fp, lang))
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
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                src = f.read()
        except OSError:
            continue
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
    return {"files": files_scanned, "total": len(findings), "findings": findings}


# ---------- ui_check：多引擎（Bevy 重点）----------
_UI_PATTERNS = {
    "bevy": bevy.BEVY_UI_PATTERNS,
    "godot": [
        (r"Button\b[^:]*:\s*$", "Button 信号未连接（疑似死按钮）"),
    ],
    "unity": [
        (r"new\s+Button\s*\([^)]*\)", "运行时 new Button（应引用场景中的）"),
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
