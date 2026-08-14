# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
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
    r"待实现|待完成|待办|这里写|此处写|暂未实现|尚未实现|未实现|"  # 2026-08-14 词表补齐（中文常用占位）
    r"your[-_ ]?(name|email|url|project|org)|example\.(com|org)",
    re.IGNORECASE,
)

# TODO/FIXME 是开发中正常标记，仅统计不判违规（summary 计数）
_TODO_RE = re.compile(r"TODO\s*[:：]|FIXME\s*[:：]|XXX\s*[:：]", re.IGNORECASE)

# 常见 UI 硬编码（前端/游戏 UI）：颜色与典型魔法尺寸
_UI_HARDCODE_RE = re.compile(
    r"(#[0-9a-fA-F]{3,8}\b)|(rgba?\(\s*\d+)|(Color::rgb)|(Color::rgba)|(Color::hex)|"
    r"(width\s*[:=]\s*\d{3,})|(height\s*[:=]\s*\d{3,})|(font_size\s*[:=]\s*\d{2,})|"
    r"(padding\s*[:=]\s*\d{2,})|(margin\s*[:=]\s*\d{2,})|"
    r"(Val::Px\(\s*\d{2,})|"  # 2026-08-14 补齐：Bevy 最常见写法（原只认裸数字）
    # IDE 增强 310：Flutter 硬编码（Color(0xFF...)/width:/height:/fontSize:）
    r"(Color\(0x[0-9a-fA-F]{6,8}\))|(width:\s*\d{3,})|(height:\s*\d{3,})|"
    r"(fontSize:\s*\d{2,})|(padding:\s*(EdgeInsets\.)?\w+)|(margin:\s*\d{2,})",
)

# IDE 增强 117：支持带下划线数字（Rust/Python 风格 100_000——防漏检）
_MAGIC_NUMBER_RE = re.compile(r"\b(?:[3-9][\d_]{2,}|[1-9][\d_]{3,})\b")

# 依赖泄露（secret）检测：常见凭据/令牌模式（对标 gitleaks 子集，零依赖）。
# 命中即 Critical——提交到仓库的凭据是真实泄露风险。
# 强格式密钥（ghp_/AKIA/sk- 等）在任何文件（含测试）都报——测试夹具也不该用真实格式；
# 弱赋值模式（password=xxx）跳过测试文件（夹具常用）。
_SECRET_RE = re.compile(
    r"(?i)\b(ghp_[A-Za-z0-9]{36}|gho_[A-Za-z0-9]{36}|github_pat_[A-Za-z0-9_]{22,}|"
    r"AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|sk-[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"AIza[0-9A-Za-z_-]{35}|-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----|"
    r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,})",
)
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)\b(password|passwd|secret|api[_-]?key|token)\s*[=:]\s*['\"][A-Za-z0-9_./+=-]{12,}['\"]",
)


def _iter_py_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith((".", "__")) and d not in ("node_modules", "target", "bin", "dist", "build")]
        for fn in filenames:
            if not fn.endswith((".py", ".rs", ".ts", ".tsx", ".js", ".jsx", ".go", ".gd",
                                ".gdshader", ".c", ".h", ".cpp", ".hpp", ".cc",
                                ".cs", ".lua", ".sh", ".bash",
                                ".java", ".kt", ".kts", ".swift", ".php", ".rb", ".ps1",
                                ".dart")):
                continue
            yield os.path.join(dirpath, fn)


# 扫描器自身文件豁免：这些文件的 docstring 描述规则关键词（"占位/假数据/魔法数字"等），
# text_placeholder/magic_number 规则会自报噪声——自身文件跳过这两条规则
_SELF_EXEMPT_BASENAMES = {"std_core.py", "server.py", "locate_core.py", "cb_index_core.py", "ds_core.py", "ui_check_core.py"}


def _is_self_exempt(path: str) -> bool:
    return os.path.basename(path) in _SELF_EXEMPT_BASENAMES


def _is_test_file(path: str) -> bool:
    """测试文件（夹具凭据是故意数据，secret 扫描跳过；其他规则仍扫）。"""
    base = os.path.basename(path)
    return base.startswith("test_") or base.endswith("_test.py") or base.endswith(".spec.ts")


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
    lines = src.splitlines()
    for m in _TEXT_PLACEHOLDER_RE.finditer(src):
        line = src.count("\n", 0, m.start()) + 1
        # 修复（自扫第三轮抓出）：整行注释里的占位词不报
        # （示例 URL 说明/文档注释——非占位文字）
        try:
            line_txt = lines[line - 1]
        except IndexError:
            line_txt = ""
        _cp = "#" if path.endswith(".py") else ("//" if path.endswith((".rs", ".go", ".ts", ".tsx", ".js", ".jsx", ".c", ".cpp", ".h", ".hpp")) else "")
        if _cp and line_txt.lstrip().startswith(_cp):
            continue
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
        # IDE 增强 108/118/166：ts/js/tsx/jsx + go + gd 文本启发——模块级重复声明
        # （function/class/const/let/var/func 同名 → 重复定义；gd 的 func 与 go 同构）
        # IDE 增强 258：c/cpp 并入（函数声明同名 → 重复定义；c 的 static 函数重名是错误）
        if path.endswith((".ts", ".tsx", ".js", ".jsx", ".go", ".gd")):
            count = 0
            seen: dict = {}
            for i, line in enumerate(src.splitlines(), 1):
                if line.lstrip().startswith(("//", "#")):
                    continue
                # IDE 增强 118：go 的 func name( 声明并入检测
                m = re.match(
                    r"\s*(?:export\s+default\s+|export\s+)?"
                    r"(?:function\s+([A-Za-z_$][\w$]*)|class\s+([A-Za-z_$][\w$]*)|"
                    r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:function|class|\(|\{|[A-Za-z_$])|"
                    r"func\s+(?:\([^)]*\)\s+)?([A-Za-z_$][\w$]*)\s*\()",
                    line)
                if not m:
                    continue
                name = next((g for g in m.groups() if g), "")
                if not name:
                    continue
                if name in seen:
                    issues.append({
                        "file": path, "line": i, "rule": "name_conflict",
                        "severity": "Warning",
                        "msg": f"重复定义 {name}（首次在行 {seen[name]}）",
                    })
                    count += 1
                    if count >= limit:
                        return
                else:
                    seen[name] = i
            return
    if path.endswith((".c", ".cpp", ".h", ".hpp")):
        # IDE 增强 258：c/cpp 函数声明重复（行首声明——排除 return/if 等
        # 关键字开头防调用/语句误匹配）
        count = 0
        seen: dict = {}
        for i, line in enumerate(src.splitlines(), 1):
            if line.lstrip().startswith(("//", "#", "*")):
                continue
            m = re.match(
                r"\s*(?:static\s+|inline\s+|extern\s+|virtual\s+|explicit\s+)*"
                r"(?!return\b|if\b|while\b|for\b|switch\b|else\b|case\b|sizeof\b|"
                r"new\b|delete\b|throw\b|goto\b)"
                r"[A-Za-z_]\w*\s+([A-Za-z_]\w*)\s*\([^)]*\)\s*(?:\{|;)", line)
            if not m:
                continue
            name = m.group(1)
            if name in seen:
                issues.append({
                    "file": path, "line": i, "rule": "name_conflict",
                    "severity": "Warning",
                    "msg": f"重复定义 {name}（首次在行 {seen[name]}）",
                })
                count += 1
                if count >= limit:
                    return
            else:
                seen[name] = i
        return
    if path.endswith((".dart",)):
        # IDE 增强 294：dart 重复类/函数检测（class/func 同名——
        # 对齐 c 分支；排除 Flutter 控件名）
        count = 0
        seen: dict = {}
        for i, line in enumerate(src.splitlines(), 1):
            if line.lstrip().startswith(("//", "#")):
                continue
            m = re.match(
                r"\s*(?:class|abstract class|mixin|enum)\s+(\w+)|"
                r"\s*(?:Future\s*<[^>]*>\s*|Widget\s+|void\s+|int\s+|String\s+|"
                r"bool\s+|double\s+)?"
                r"(?!TextButton\b|ElevatedButton\b|OutlinedButton\b|IconButton\b|"
                r"FilledButton\b|Column\b|Row\b|Container\b|Text\b|SizedBox\b)"
                r"(\w+)\s*\(", line)
            if not m:
                continue
            name = next((g for g in m.groups() if g), "")
            if not name:
                continue
            if name in seen:
                issues.append({
                    "file": path, "line": i, "rule": "name_conflict",
                    "severity": "Warning",
                    "msg": f"重复定义 {name}（首次在行 {seen[name]}）",
                })
                count += 1
                if count >= limit:
                    return
            else:
                seen[name] = i
        return
    if path.endswith((".java",)):
        # IDE 增强 313：java 重复类/方法检测（class/方法声明同名——
        # 对齐 dart/c 分支；排除构造器与 main 重载）
        count = 0
        seen: dict = {}
        for i, line in enumerate(src.splitlines(), 1):
            if line.lstrip().startswith(("//", "*")):
                continue
            m = re.match(
                r"\s*(?:public\s+|private\s+|protected\s+)*(?:static\s+|final\s+)*"
                r"(?:class|interface|enum)\s+(\w+)|"
                r"\s*(?:public\s+|private\s+|protected\s+)*(?:static\s+|final\s+)*"
                r"[A-Za-z_<>\[\],\s]*\s+([A-Za-z_]\w*)\s*\([^)]*\)\s*(?:\{|$)", line)
            if not m:
                continue
            name = next((g for g in m.groups() if g), "")
            if not name:
                continue
            if name in seen:
                issues.append({
                    "file": path, "line": i, "rule": "name_conflict",
                    "severity": "Warning",
                    "msg": f"重复定义 {name}（首次在行 {seen[name]}）",
                })
                count += 1
                if count >= limit:
                    return
            else:
                seen[name] = i
        return
    if path.endswith((".kt", ".kts")):
        # IDE 增强 314：kotlin 重复类/函数检测（class/fun 同名——
        # 对齐 java/dart 分支）
        count = 0
        seen: dict = {}
        for i, line in enumerate(src.splitlines(), 1):
            if line.lstrip().startswith(("//", "*")):
                continue
            m = re.match(
                r"\s*(?:data\s+|sealed\s+|abstract\s+|open\s+)?class\s+(\w+)|"
                r"\s*(?:fun\s+)?(?!TextButton|ElevatedButton)(\w+)\s*\(", line)
            if not m:
                continue
            name = next((g for g in m.groups() if g), "")
            if not name:
                continue
            if name in seen:
                issues.append({
                    "file": path, "line": i, "rule": "name_conflict",
                    "severity": "Warning",
                    "msg": f"重复定义 {name}（首次在行 {seen[name]}）",
                })
                count += 1
                if count >= limit:
                    return
            else:
                seen[name] = i
        return
    if path.endswith((".swift",)):
        # IDE 增强 316：swift 重复类/函数检测（class/struct/func 同名——
        # 对齐 kt/java 分支）
        count = 0
        seen: dict = {}
        for i, line in enumerate(src.splitlines(), 1):
            if line.lstrip().startswith(("//", "*")):
                continue
            m = re.match(
                r"\s*(?:public\s+|private\s+|internal\s+|final\s+)*(?:class|struct|enum)\s+(\w+)|"
                r"\s*(?:public\s+|private\s+|internal\s+|final\s+)*func\s+(\w+)\s*\(", line)
            if not m:
                continue
            name = next((g for g in m.groups() if g), "")
            if not name:
                continue
            if name in seen:
                issues.append({
                    "file": path, "line": i, "rule": "name_conflict",
                    "severity": "Warning",
                    "msg": f"重复定义 {name}（首次在行 {seen[name]}）",
                })
                count += 1
                if count >= limit:
                    return
            else:
                seen[name] = i
        return
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return
    count = 0

    def _check_scope(scope_name: str, defs: list):
        """作用域内重复定义检测（模块级或单个 class 内）。"""
        nonlocal count
        seen: dict = {}
        for node in defs:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            name = node.name
            if name in seen:
                issues.append({
                    "file": path, "line": node.lineno, "rule": "name_conflict",
                    "severity": "Warning",
                    "msg": f"重复定义 {name}（{scope_name} 内，首次在行 {seen[name]}）",
                })
                count += 1
                if count >= limit:
                    return
            else:
                seen[name] = node.lineno

    # 模块级
    _check_scope("模块", [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))])
    if count >= limit:
        return
    # 每个 class 内（同类方法不算重复——修复 __init__ 跨类误报）
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            _check_scope(f"类 {node.name}", node.body)
            if count >= limit:
                return


def _scan_ui_hardcode(path: str, src: str, issues: list, limit: int):
    if not (path.endswith((".rs", ".ts", ".tsx", ".js", ".jsx", ".gd", ".dart"))):
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
    # IDE 增强 106：支持 .ts/.js（前端代码魔法数字同样要查）
    if not (path.endswith((".py", ".rs", ".go", ".ts", ".tsx", ".js", ".jsx", ".gd",
                            ".c", ".h", ".cpp", ".hpp", ".cc", ".cs", ".lua",
                            ".sh", ".bash", ".java", ".kt", ".kts", ".swift",
                            ".php", ".rb", ".ps1", ".dart"))):
        return
    count = 0
    lines = src.splitlines()
    for m in _MAGIC_NUMBER_RE.finditer(src):
        line_no = src.count("\n", 0, m.start()) + 1
        # 双报去重（2026-08-14）：Val::Px/Val::Percent 内的数字已由
        # ui_hardcode 报（语义更准）——magic_number 跳过 UI 维度上下文
        try:
            line_txt = lines[line_no - 1]
        except IndexError:
            line_txt = ""
        # 修复（自扫抓出 2026-08-14）：整行注释里的数字不报
        # （SPDX 版权年份/版本注释——非魔法数字）
        _cp = "--" if path.endswith((".lua",)) else (
            "#" if path.endswith((".py", ".gd", ".sh", ".bash")) else (
            "//" if path.endswith((".rs", ".go", ".ts", ".tsx", ".js", ".jsx",
                                   ".c", ".cpp", ".h", ".hpp", ".cs", ".java",
                                   ".kt", ".kts", ".swift", ".php", ".dart")) else
            "#" if path.endswith((".rb", ".ps1")) else ""))
        if _cp and line_txt.lstrip().startswith(_cp):
            continue
        if "Val::Px" in line_txt or "Val::Percent" in line_txt \
                or "Val::Vw" in line_txt or "Val::Vh" in line_txt:
            continue
        issues.append({
            "file": path, "line": line_no, "rule": "magic_number",
            "severity": "Suggestion",
            "msg": f"未命名魔法数字: {m.group(0)}（建议提取命名常量）",
        })
        count += 1
        if count >= limit:
            return


def _scan_dead_code(path: str, src: str, issues: list, limit: int):
    """死代码/空实现（2026-08-14 补实现——文档宣称但缺失）：
    1. 空实现：函数体仅 pass（占位未实现）→ warning
    2. 未使用 import（AST 启发：import 名在文件中零引用）→ warning
    仅 .py（AST 精确分析）；防误报：pass 函数带 docstring 或 raise 不算空实现。
    """
    if not path.endswith(".py"):
        # IDE 增强 107/119：ts/js/tsx/jsx + go 文本启发——未使用 import
        # （import { A, B } from / import A from / go import "pkg"；零引用 → 未使用）
        if path.endswith((".ts", ".tsx", ".js", ".jsx")):
            count = 0
            for i, line in enumerate(src.splitlines(), 1):
                if line.lstrip().startswith("//"):
                    continue
                m = re.search(
                    r"\bimport\s+(?:type\s+)?(?:\{([^}]*)\}|([A-Za-z_$][\w$]*))"
                    r"\s+from\s+", line)
                if not m:
                    continue
                names = [n.strip() for n in
                         (m.group(1) or m.group(2) or "").split(",") if n.strip()]
                for name in names:
                    name = name.split(" as ")[-1].strip()
                    if not name:
                        continue
                    if len(re.findall(rf"\b{re.escape(name)}\b", src)) <= 1:
                        issues.append({
                            "rule": "dead_code", "severity": "Warning",
                            "line": i, "msg": f"未使用 import：`{name}`（文件内零引用）",
                            "file": path})
                        count += 1
                        if count >= limit:
                            return
        elif path.endswith(".go"):
            # IDE 增强 119：go 未使用 import（`import "fmt"` / 别名 `import f "fmt"`
            # 零引用 → 未使用；`_ "pkg"` 副作用导入豁免）
            count = 0
            for i, line in enumerate(src.splitlines(), 1):
                if line.lstrip().startswith("//"):
                    continue
                if re.search(r'\b_\s+"', line):
                    continue  # 副作用导入豁免
                m = re.search(
                    r'\bimport\s+(?:([A-Za-z_][\w]*)\s+)?"([^"]+)"'
                    r'|^\s*"([^"]+)"\s*$', line)
                if not m:
                    continue
                name = (m.group(1) or m.group(2) or m.group(3) or "")
                name = name.split("/")[-1].split("-")[-1]
                if not name:
                    continue
                if len(re.findall(rf"\b{re.escape(name)}\b", src)) <= 1:
                    issues.append({
                        "rule": "dead_code", "severity": "Warning",
                        "line": i, "msg": f"未使用 import：`{name}`（文件内零引用）",
                        "file": path})
                    count += 1
                    if count >= limit:
                        return
        # IDE 增强 307：java/kt 未使用 import（import java.util.List /
        # import android.os.Bundle / import kotlinx.coroutines.*；零引用 → 未使用）
        if path.endswith((".java", ".kt", ".kts")):
            count = 0
            for i, line in enumerate(src.splitlines(), 1):
                if line.lstrip().startswith("//"):
                    continue
                m = re.match(r"\s*import\s+(?:static\s+)?([A-Za-z_][\w.]*)", line)
                if not m:
                    continue
                name = m.group(1).split(".")[-1].strip("*")
                if not name:
                    continue
                if name == "*" or name.endswith(".*"):
                    continue  # 通配导入豁免（可能使用任意子符号）
                if len(re.findall(rf"\b{re.escape(name)}\b", src)) <= 1:
                    issues.append({
                        "rule": "dead_code", "severity": "Warning",
                        "line": i, "msg": f"未使用 import：`{name}`（文件内零引用）",
                        "file": path})
                    count += 1
                    if count >= limit:
                        return
        return
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return
    count = 0

    def _add(line: int, msg: str) -> None:
        nonlocal count
        if count >= limit:
            return
        issues.append({"rule": "dead_code", "severity": "Warning",
                       "line": line, "msg": msg, "file": path})  # 2026-08-14：大写与 summary 计数一致
        count += 1

    # 1. 空实现：函数体仅 pass（有 docstring 的不算——占位带说明可接受）
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        has_doc = any(isinstance(s, ast.Expr)
                      and isinstance(s.value, ast.Constant)
                      and isinstance(s.value.value, str)
                      for s in node.body)
        body = [s for s in node.body
                if not (isinstance(s, ast.Expr)
                        and isinstance(s.value, ast.Constant)
                        and isinstance(s.value.value, str))]
        if not has_doc and len(body) == 1 and isinstance(body[0], ast.Pass):
            _add(node.lineno, f"空实现占位：`{node.name}` 仅 pass——未实现")

    # 2. 未使用 import（启发式：名字除 import 语句外零引用）
    imported: dict[str, int] = {}
    used: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                imported[a.asname or a.name.split(".")[0]] = node.lineno
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                if a.name != "*":
                    imported[a.asname or a.name] = node.lineno
        elif isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            used.add(node.value.id)
    # 2026-08-14 修复误报边界：__all__ 再导出（__init__ 常见模式——
    # import 的名字在 __all__ 字符串列表里视为使用）
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.List, ast.Tuple)):
            if any(isinstance(t, ast.Name) and t.id == "__all__"
                   for t in node.targets):
                for elt in node.value.elts:
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                        used.add(elt.value)
    for name, lineno in imported.items():
        if name not in used and name not in ("_", "annotations"):
            _add(lineno, f"未使用 import：`{name}`（文件内零引用）")


def _scan_secret(path: str, src: str, issues: list, limit: int):
    """依赖泄露检测：命中凭据/令牌/私钥 → Critical（真实泄露风险）。

    强格式密钥（ghp_/AKIA/sk- 等）任何文件都报（测试夹具也不该用真实格式）；
    弱赋值模式（password=xxx）跳过测试文件（夹具常用，防误报）。
    """
    is_test = _is_test_file(path)
    patterns = [_SECRET_RE] if is_test else [_SECRET_RE, _SECRET_ASSIGN_RE]
    count = 0
    seen_lines: set = set()
    for pat in patterns:
        for m in pat.finditer(src):
            line = src.count("\n", 0, m.start()) + 1
            if line in seen_lines:  # 同一条行被强格式+赋值双命中时只报一次
                continue
            seen_lines.add(line)
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
    # IDE 增强 140：文件类型分布（语言构成——AI 了解项目用什么语言）
    ext_counts: dict[str, int] = {}
    per_rule_limit = max(10, max_files // 4)
    for fp in _iter_py_files(path):
        if files >= max_files:
            break
        src = _read(fp)
        if src is None:
            continue
        files += 1
        _ext = os.path.splitext(fp)[1].lower() or "(none)"
        ext_counts[_ext] = ext_counts.get(_ext, 0) + 1
        if _is_self_exempt(fp):
            # 自身文件豁免：docstring 描述词不报（text_placeholder/magic_number 噪声）
            _scan_name_conflict(fp, src, issues, per_rule_limit)
            _scan_ui_hardcode(fp, src, issues, per_rule_limit)
            _scan_secret(fp, src, issues, per_rule_limit)
        else:
            _scan_text_placeholder(fp, src, issues, per_rule_limit, todo_count)
            _scan_name_conflict(fp, src, issues, per_rule_limit)
            _scan_ui_hardcode(fp, src, issues, per_rule_limit)
            _scan_magic_number(fp, src, issues, per_rule_limit)
            _scan_secret(fp, src, issues, per_rule_limit)
            _scan_dead_code(fp, src, issues, per_rule_limit)  # 2026-08-14：补实现
        if len(issues) >= max_files:
            break
    return _summarize(issues, files, path, todo_count[0], ext_counts)


def scan_file(path: str) -> dict:
    src = _read(path)
    if src is None:
        return {"ok": False, "issues": [], "summary": {"files": 0, "rules": {}, "error": f"读取失败或超过 1MB: {path}"}}
    issues: list = []
    todo_count = [0]
    if _is_self_exempt(path):
        # 自身文件豁免：docstring 描述词不报（text_placeholder/magic_number 噪声）
        _scan_name_conflict(path, src, issues, 50)
        _scan_ui_hardcode(path, src, issues, 50)
        _scan_secret(path, src, issues, 50)
    else:
        _scan_text_placeholder(path, src, issues, 50, todo_count)
        _scan_name_conflict(path, src, issues, 50)
        _scan_ui_hardcode(path, src, issues, 50)
        _scan_magic_number(path, src, issues, 50)
        _scan_secret(path, src, issues, 50)
        _scan_dead_code(path, src, issues, 50)  # 2026-08-14：补实现
    return _summarize(issues, 1, path, todo_count[0])


def _summarize(issues: list, files: int, path: str, todo_count: int = 0,
               ext_counts: dict | None = None) -> dict:
    rules: dict = {}
    for i in issues:
        rules[i["rule"]] = rules.get(i["rule"], 0) + 1
    critical = sum(1 for i in issues if i["severity"] == "Critical")
    warning = sum(1 for i in issues if i["severity"] == "Warning")
    suggestion = sum(1 for i in issues if i["severity"] == "Suggestion")
    # LSE 自适应权重（P1）：从 lse-engine 读每条规则权重（无则 1.0）
    rule_weights: dict = {}
    try:
        from lse_client import state_get
        st = state_get()
        if st.get("ok"):
            for rname, rdata in st.get("result", {}).get("rules", {}).items():
                rule_weights[rname] = rdata.get("weight", 1.0)
    except Exception:  # 尽力而为
        pass
    # 低权重规则（<0.3）视为已被反馈降权——suggestion 降级为 info（不阻断 ok）
    low_weight_rules = {r for r, w in rule_weights.items() if w < 0.3}
    effective_critical = critical
    effective_warning = warning
    for i in issues:
        if i["rule"] in low_weight_rules and i["severity"] == "Suggestion":
            i["severity"] = "Info"
    return {
        "ok": effective_critical == 0 and effective_warning == 0,
        "issues": issues[:200],
        "summary": {
            "scanned": path, "files": files, "total": len(issues),
            "critical": effective_critical, "warning": effective_warning, "suggestion": suggestion,
            "todo_markers": todo_count,
            "rules": rules,
            # IDE 增强 140：文件类型分布（语言构成）
            "ext_counts": dict(sorted((ext_counts or {}).items(),
                                      key=lambda kv: -kv[1])),
            # IDE 增强 286：languages 别名（与 bug_scan/cb_scan 统一——
            # vuln/project 聚合入口认 languages 字段；key 去点对齐）
            "languages": dict(sorted(
                {k.lstrip("."): v for k, v in (ext_counts or {}).items()}.items(),
                key=lambda kv: -kv[1])),
            "rule_weights": rule_weights,  # LSE 自适应权重（采纳/忽略反馈进化）
            # IDE 增强 144：空仓库/空目录明确提示（files=0 时不产生歧义）
            "hint": ("未扫描到代码文件（空目录/无支持后缀）——检查路径或语言支持"
                     if files == 0 else ""),
        },
    }
