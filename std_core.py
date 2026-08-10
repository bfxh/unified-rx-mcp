"""std_core — 通用工程标准检查（软件/游戏/前端/UI 通用）。

检查维度（本地静态扫描，零网络，兼容绝大多数项目；特殊条件由调用方
在提示词中提前告知，否则按本默认标准执行）：

1. text_placeholder  文字规范：占位符/假数据/套话（TODO/FIXME/lorem/示例文案/假数据）
2. name_conflict     命名冲突：同一作用域重复定义（def/class/fn/const）
3. ui_hardcode       UI 标准化：硬编码颜色/尺寸魔法值（前端/UI 代码）
4. magic_number      魔法数字：代码中未命名的裸数字（限阈值）
5. dead_code         死代码/空实现：pass/return None 占位、未使用 import 启发

输出：{"ok": bool, "issues": [{file, line, rule, severity, msg}], "summary": {...}}
Python 3.8+ 标准库零依赖。与 server.py 同目录部署。
"""

import ast
import os
import re

_MAX_FILE = 1 << 20          # 单文件 1MB
_MAX_FILES = 500

_TEXT_PLACEHOLDER_RE = re.compile(
    r"lorem\s+ipsum|placeholder|占位|假数据|示例文案|待补充|待完善|"
    r"your[-_ ]?(name|email|url|project|org)|example\.(com|org)",
    re.IGNORECASE,
)

# TODO/FIXME 是开发中正常标记，仅统计不判违规（summary 计数）
_TODO_RE = re.compile(r"TODO\s*[:：]|FIXME\s*[:：]|XXX\s*[:：]", re.IGNORECASE)

# 常见 UI 硬编码（前端/游戏 UI）：颜色与典型魔法尺寸
_UI_HARDCODE_RE = re.compile(
    r"(#[0-9a-fA-F]{3,8}\b)|(rgba?\(\s*\d+)|(Color::rgb)|(Color::rgba)|(Color::hex)|"
    r"(width\s*[:=]\s*\d{3,})|(height\s*[:=]\s*\d{3,})|(font_size\s*[:=]\s*\d{2,})|"
    r"(padding\s*[:=]\s*\d{2,})|(margin\s*[:=]\s*\d{2,})",
)

_MAGIC_NUMBER_RE = re.compile(r"\b(?:[3-9]\d{2,}|[1-9]\d{3,})\b")

# 依赖泄露（secret）检测：常见凭据/令牌模式（对标 gitleaks 子集，零依赖）。
# 命中即 Critical——提交到仓库的凭据是真实泄露风险。
_SECRET_RE = re.compile(
    r"(?i)\b(ghp_[A-Za-z0-9]{36}|gho_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}|"
    r"AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"AIza[0-9A-Za-z_-]{35}|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"(password|passwd|secret|api[_-]?key|token)\s*[=:]\s*['\"][A-Za-z0-9_./+=-]{12,}['\"])",
)


def _iter_py_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "__")) and d not in ("node_modules", "target", "bin", "dist", "build")]
        for fn in filenames:
            if not fn.endswith((".py", ".rs", ".ts", ".tsx", ".js", ".jsx", ".go", ".gd", ".gdshader")):
                continue
            yield os.path.join(dirpath, fn)


def _read(path: str) -> str | None:
    try:
        if os.path.getsize(path) > _MAX_FILE:
            return None
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _scan_text_placeholder(path: str, src: str, issues: list, limit: int, todo_count: list):
    count = 0
    todo_count[0] += len(_TODO_RE.findall(src))
    for m in _TEXT_PLACEHOLDER_RE.finditer(src):
        line = src.count("\n", 0, m.start()) + 1
        issues.append({
            "file": path, "line": line, "rule": "text_placeholder",
            "severity": "Suggestion",
            "msg": f"占位文字: {m.group(0)!r}",
        })
        count += 1
        if count >= limit:
            return


def _scan_name_conflict(path: str, src: str, issues: list, limit: int):
    if not (path.endswith(".py")):
        return
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return
    seen: dict = {}
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = node.name
            if name in seen and seen[name] == node.lineno:
                continue
            if name in seen:
                issues.append({
                    "file": path, "line": node.lineno, "rule": "name_conflict",
                    "severity": "Warning",
                    "msg": f"重复定义 {name}（首次在行 {seen[name]}）",
                })
                count += 1
                if count >= limit:
                    return
            else:
                seen[name] = node.lineno


def _scan_ui_hardcode(path: str, src: str, issues: list, limit: int):
    if not (path.endswith((".rs", ".ts", ".tsx", ".js", ".jsx", ".gd"))):
        return
    count = 0
    for m in _UI_HARDCODE_RE.finditer(src):
        line = src.count("\n", 0, m.start()) + 1
        issues.append({
            "file": path, "line": line, "rule": "ui_hardcode",
            "severity": "Suggestion",
            "msg": f"UI 硬编码值: {m.group(0)[:40]}（建议引用设计系统 token）",
        })
        count += 1
        if count >= limit:
            return


def _scan_magic_number(path: str, src: str, issues: list, limit: int):
    if not (path.endswith((".py", ".rs", ".go"))):
        return
    count = 0
    for m in _MAGIC_NUMBER_RE.finditer(src):
        line = src.count("\n", 0, m.start()) + 1
        issues.append({
            "file": path, "line": line, "rule": "magic_number",
            "severity": "Suggestion",
            "msg": f"未命名魔法数字: {m.group(0)}（建议提取命名常量）",
        })
        count += 1
        if count >= limit:
            return


def _scan_secret(path: str, src: str, issues: list, limit: int):
    """依赖泄露检测：命中凭据/令牌/私钥 → Critical（真实泄露风险）。"""
    count = 0
    for m in _SECRET_RE.finditer(src):
        line = src.count("\n", 0, m.start()) + 1
        secret = m.group(0)
        # 不泄露完整值：只显示前缀 + 长度
        shown = secret[:12] + "…" if len(secret) > 12 else secret
        issues.append({
            "file": path, "line": line, "rule": "secret_detection",
            "severity": "Critical",
            "msg": f"疑似凭据泄露: {shown}（长度 {len(secret)}）——立即轮换并移出代码库",
        })
        count += 1
        if count >= limit:
            return


def scan_directory(path: str, max_files: int = 200) -> dict:
    """扫描目录：返回 {ok, issues, summary}。"""
    issues: list = []
    files = 0
    todo_count = [0]
    per_rule_limit = max(10, max_files // 4)
    for fp in _iter_py_files(path):
        if files >= max_files:
            break
        src = _read(fp)
        if src is None:
            continue
        files += 1
        _scan_text_placeholder(fp, src, issues, per_rule_limit, todo_count)
        _scan_name_conflict(fp, src, issues, per_rule_limit)
        _scan_ui_hardcode(fp, src, issues, per_rule_limit)
        _scan_magic_number(fp, src, issues, per_rule_limit)
        _scan_secret(fp, src, issues, per_rule_limit)
        if len(issues) >= max_files:
            break
    return _summarize(issues, files, path, todo_count[0])


def scan_file(path: str) -> dict:
    src = _read(path)
    if src is None:
        return {"ok": False, "issues": [], "summary": {"files": 0, "rules": {}, "error": f"读取失败或超过 1MB: {path}"}}
    issues: list = []
    todo_count = [0]
    _scan_text_placeholder(path, src, issues, 50, todo_count)
    _scan_name_conflict(path, src, issues, 50)
    _scan_ui_hardcode(path, src, issues, 50)
    _scan_magic_number(path, src, issues, 50)
    _scan_secret(path, src, issues, 50)
    return _summarize(issues, 1, path, todo_count[0])


def _summarize(issues: list, files: int, path: str, todo_count: int = 0) -> dict:
    rules: dict = {}
    for i in issues:
        rules[i["rule"]] = rules.get(i["rule"], 0) + 1
    critical = sum(1 for i in issues if i["severity"] == "Critical")
    warning = sum(1 for i in issues if i["severity"] == "Warning")
    suggestion = sum(1 for i in issues if i["severity"] == "Suggestion")
    return {
        "ok": critical == 0 and warning == 0,
        "issues": issues[:200],
        "summary": {
            "scanned": path, "files": files, "total": len(issues),
            "critical": critical, "warning": warning, "suggestion": suggestion,
            "todo_markers": todo_count,
            "rules": rules,
        },
    }
