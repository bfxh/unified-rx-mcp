# -*- coding: utf-8 -*-
"""tools/scan.py —— 扫描域（5 工具）：bug_scan / std_check / ui_check / bug_locate / project_scan

收敛自旧版 vuln_scan/scan_all/scan_now/scan_delta → project_scan 组合。
P3 增强（2026-08-24）：Rust 生产规则、ui_check 三引擎。
P4 增强（2026-08-24）：Bevy 专项规则（用户：引擎重点优化 Bevy）。
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
    """遍历文件（目录或单文件）。"""
    if os.path.isfile(path):
        yield path
        return
    if not os.path.isdir(path):
        return
    count = 0
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "target",
                                                "__pycache__", "dist", "build",
                                                ".codegraph", "backups")]
        for fn in files:
            if count >= max_files:
                return
            yield os.path.join(root, fn)
            count += 1


def _lang_of(path):
    return _LANG_BY_EXT.get(os.path.splitext(path)[1].lower(), "")


# ---------- bug_scan：Python AST 规则 ----------
def _scan_python(src, path):
    issues = []
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return [{"line": e.lineno or 0, "rule": "syntax_error",
                 "msg": f"语法错误: {e.msg}", "file": path}]
    defined = set()
    imported = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                imported[a.asname or a.name.split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                imported[a.asname or a.name] = node.lineno
    defined |= set(imported.keys())
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in defined and node.id not in dir(__builtins__):
                issues.append({"line": node.lineno, "rule": "undefined_name",
                               "msg": f"未定义变量 '{node.id}'", "file": path})
        elif isinstance(node, ast.ExceptHandler) and node.type is None:
            issues.append({"line": getattr(node, "lineno", 0), "rule": "bare_except",
                           "msg": "裸 except（吞掉所有异常）", "file": path})
    for name, lineno in imported.items():
        if name in dir(__builtins__):
            issues.append({"line": lineno, "rule": "redefined_import",
                           "msg": f"导入 '{name}' 遮蔽内建名", "file": path})
    return issues


# ---------- bug_scan：Rust 生产规则 ----------
_RUST_RULES = [
    ("unwrap", r"\.unwrap\(\)", "unwrap()——None/Err 时直接 panic（生产代码应改为 ? / expect / match）", "high"),
    ("expect", r"\.expect\(\s*\"", "expect()——带消息 panic（仍会崩溃；生产应返回 Result）", "medium"),
    ("panic", r"\bpanic!\(", "panic!()——直接崩溃（生产代码应避免）", "high"),
    ("unreachable", r"\bunreachable!\(", "unreachable!()——到达即 bug（生产代码应避免）", "high"),
    ("todo_unimplemented", r"\b(todo!|unimplemented!)\(", "todo!/unimplemented!()——未实现即崩溃", "high"),
    ("as_cast", r"\bas\s+(i64|i32|u64|u32|f64|f32|usize|isize)\b", "as 类型转换——截断/精度丢失风险（建议 try_from）", "medium"),
    ("indexing", r"\[[a-zA-Z_][a-zA-Z0-9_]*\]", "索引访问——越界即 panic（建议 .get()）", "medium"),
]


def _scan_rust(src, path):
    issues = []
    for rule, pat, msg, sev in _RUST_RULES:
        for m in re.finditer(pat, src):
            line = src.count("\n", 0, m.start()) + 1
            line_text = src.split("\n")[line - 1].strip()
            if line_text.startswith("//"):
                continue
            issues.append({"line": line, "rule": rule, "msg": msg, "file": path,
                           "severity": sev})
    # Bevy 代码规则（Rust 文件里也扫）
    for rule, pat, msg, sev in bevy.bevy_rules():
        for m in re.finditer(pat, src):
            line = src.count("\n", 0, m.start()) + 1
            issues.append({"line": line, "rule": rule, "msg": msg, "file": path,
                           "severity": sev})
    # 测试目录降级
    norm = path.replace("\\", "/")
    norm_clean = norm.replace("_tmp/", "")
    is_test = norm_clean.endswith("_test.rs") or re.search(r"/tests(?:/|$)", norm_clean)
    if is_test:
        for i in issues:
            if i["rule"] in ("unwrap", "expect"):
                i["severity"] = "low"
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
            issues.append({"line": line, "rule": rule, "msg": msg, "file": path})
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
    return {"files": files_scanned, "total": len(issues),
            "by_rule": by_rule, "by_severity": by_sev,
            "issues": issues[:200]}


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
    return {"files": files_scanned, "total": len(findings), "findings": findings[:200]}


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
    return {"files": files_scanned, "total": len(issues), "issues": issues[:200]}


# ---------- bug_locate ----------
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
    for m in re.finditer(r"([A-Za-z_][A-Za-z0-9_.]*\.py)", error_text):
        candidates.append(m.group(1))
    for m in re.finditer(r"(?:NameError|AttributeError|KeyError|ImportError).*?['\"]([^'\"]+)['\"]", error_text):
        candidates.append(m.group(1))
    direct = []
    for c in candidates:
        if c.endswith(".py"):
            for fp in _iter_files(root, MAX_FILES):
                if fp.endswith(c):
                    direct.extend(_find_in_file(fp, ""))
                    if direct:
                        direct[-1]["msg"] = f"候选文件 {c}"
        else:
            for fp in _iter_files(root, MAX_FILES):
                if fp.endswith(".py"):
                    hits = _find_in_file(fp, c, max_hits=2)
                    if hits:
                        for h in hits:
                            h["msg"] = f"符号 '{c}'"
                        direct.extend(hits)
    seen = set()
    out = []
    for h in direct:
        k = (h["file"], h["line"])
        if k in seen:
            continue
        seen.add(k)
        out.append(h)
        if len(out) >= 10:
            break
    return {"candidates": len(out), "hits": out}


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
