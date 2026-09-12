# -*- coding: utf-8 -*-
"""tools/ide_deadcode.py —— 死符号可达性（S123）：ide_dead_code。

动机（SCAN-POLICY：上帝对象拆分大于测试）：拆分前先知道**谁没人用**。全库
可达性近似——把"定义了但整个代码库零引用"的顶层函数/类/私有方法找出来，给
拆分候选清单一个客观下界。

口径（保守优先，误报比漏报伤人）：
- 引用 = 所有 `.py` 里出现的 ast.Name / ast.Attribute 名字（跨模块引用算活）；
  `def foo():` 的定义处本身不产生 Name 节点，所以"零 Name/Attribute 出现"是
  可靠的死信号；
- **字符串引用升级**：名字出现在任何字符串字面量里（getattr 分发、注册表、
  __all__）→ 不判死，单独列 suspect_dynamic——动态分发静态分析看不见；
- **带装饰器的定义默认豁免**：@tool/@app.route 之类是框架注册点，函数对象
  被框架拿走，静态零引用不等于死（本仓库 66 个 MCP 工具全是这类）。要查
  就传 include_decorated=true；
- 方法只查**私有**（单下划线、非双下划线）——公有方法可能是鸭子类型/覆写
  目标；嵌套函数/闭包不查（按设计就常零引用）；dunder 一律不查；
- 测试文件引用与生产代码同权——测试引用到的就是活的（测试即入口）；
- **pytest 命名约定豁免**：`test_*` / `pytest_*` / `setup_*` / `teardown_*`
  前缀与 `conftest.py` 整文件——pytest 按约定收集，从不按名字引用，不豁免
  就是整片误报（自测实录：conftest 的 pytest_configure 被报死）。

诚实边界：这是**全库静态可达性近似，不是精确调用图**——不解 import 语义
（名字重用跨模块会误活）、不追 exec/eval/globals() 字符串拼接、别名引用
（as 重命名后用新名）算不到原名。零引用 ≠ 可安全删除，删前人工确认。
"""
import ast
import os
import time

from registry import tool
from tools.ide_common import _SKIP_DIRS

_DUNDERS = frozenset()
_MAX_FILE_KB = 1024
# pytest 约定入口：按名字收集、从不按名字引用（不豁免就是整片误报）
_PYTEST_PREFIXES = ("test_", "pytest_", "setup_", "teardown_")


def _is_pytest_entry(name, kind, basename):
    return (kind in ("function", "method")
            and (name.startswith(_PYTEST_PREFIXES) or basename == "conftest.py"))


def _walk_py(root, max_files):
    """遍历 .py 文件（跳过 _SKIP_DIRS 与 venv 常见目录），超过 max_files 停。"""
    skip = set(_SKIP_DIRS) | {"venv", ".venv", "site-packages", ".tox",
                              ".mypy_cache", ".pytest_cache", ".ruff_cache"}
    count = 0
    for r, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in skip]
        for fn in files:
            if not fn.endswith(".py"):
                continue
            if count >= max_files:
                return
            count += 1
            yield os.path.join(r, fn)


def _collect_defs(tree):
    """(顶层函数/类, 私有方法) —— 返回 [(file, line, name, kind)] 由调用方补 file。"""
    top, methods = [], []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            decorated = bool(node.decorator_list)
            top.append((node.name, node.lineno, "function", decorated))
        elif isinstance(node, ast.ClassDef):
            decorated = bool(node.decorator_list)
            top.append((node.name, node.lineno, "class", decorated))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if (sub.name.startswith("_") and not sub.name.startswith("__")
                            and not sub.decorator_list):
                        methods.append((sub.name, sub.lineno, "method", False))
    return top, methods


def _shorthand_refs(tree):
    """一次遍历收集 Name/Attribute 引用与字符串字面量。"""
    names, attrs, strings = set(), set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            attrs.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.append(node.value)
    return names, attrs, strings


@tool("ide_dead_code",
      "Python 死符号可达性：全库 ast 扫描，报零引用的顶层函数/类/私有方法"
      "（上帝对象拆分候选的客观下界）。带装饰器的定义默认豁免（框架注册点）；"
      "名字出现在字符串字面量 → 列入 suspect_dynamic 不判死。零引用≠可安全删除，"
      "删前人工确认", "ide",
      {"type": "object",
       "properties": {
           "path": {"type": "string", "description": "项目根目录（默认当前目录）"},
           "max_files": {"type": "integer", "description": "最多扫描的 .py 文件数（默认 2000）"},
           "include_decorated": {"type": "boolean",
                                 "description": "把带装饰器的定义也纳入死代码判定（默认 false=豁免）"},
           "max_results": {"type": "integer", "description": "dead 列表上限（默认 200）"}},
       "required": []})
def ide_dead_code(path=None, max_files=2000, include_decorated=False,
                  max_results=200):
    t0 = time.time()
    root = os.path.abspath(path or os.getcwd())
    if not os.path.isdir(root):
        return {"ok": False, "error": f"目录不存在: {root}"}

    ref_names, ref_attrs, all_strings = set(), set(), []
    defs = []            # (file, line, name, kind, decorated)
    files_scanned = 0
    parse_errors = []

    for fp in _walk_py(root, int(max_files)):
        try:
            if os.path.getsize(fp) > _MAX_FILE_KB * 1024:
                continue
            with open(fp, encoding="utf-8", errors="replace") as f:
                src = f.read()
            tree = ast.parse(src)
        except SyntaxError as e:
            parse_errors.append({"file": fp, "error": str(e)})
            continue
        except OSError:
            continue
        files_scanned += 1
        rel = os.path.relpath(fp, root)
        top, methods = _collect_defs(tree)
        for name, line, kind, decorated in top:
            defs.append((rel, line, name, kind, decorated))
        for name, line, kind, decorated in methods:
            defs.append((rel, line, name, kind, decorated))
        names, attrs, strings = _shorthand_refs(tree)
        ref_names |= names
        ref_attrs |= attrs
        all_strings.extend(strings)

    blob = "\n".join(all_strings)
    dead, suspect = [], []
    exempted_decorated = 0
    exempted_pytest = 0
    for rel, line, name, kind, decorated in defs:
        if name.startswith("__") and name.endswith("__"):
            continue
        if decorated and not include_decorated:
            exempted_decorated += 1
            continue
        if _is_pytest_entry(name, kind, os.path.basename(rel)):
            exempted_pytest += 1
            continue
        referenced = name in ref_names or name in ref_attrs
        if referenced:
            continue
        entry = {"file": rel, "line": line, "name": name, "kind": kind}
        if name in blob:                    # 字符串里有名字 → 动态分发嫌疑
            entry["hint"] = "名字出现在字符串字面量（getattr/注册表/__all__），静态无法定论"
            suspect.append(entry)
        else:
            dead.append(entry)

    dead.sort(key=lambda e: (e["file"], e["line"]))
    suspect.sort(key=lambda e: (e["file"], e["line"]))
    truncated = len(dead) > int(max_results)
    return {"root": root, "files_scanned": files_scanned,
            "parse_errors": parse_errors[:10], "defs_total": len(defs),
            "dead_count": len(dead), "dead": dead[:int(max_results)],
            "suspect_dynamic": suspect[:50],
            "exempted_decorated": exempted_decorated,
            "exempted_pytest_entry": exempted_pytest,
            "truncated": truncated,
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
            "note": ("口径：ast.Name/Attribute 零引用 + 字符串引用降级为嫌疑 + "
                     "装饰器定义默认豁免。别名/import as、exec/eval、globals() "
                     "拼接追不到——零引用≠可安全删除，删前人工确认")}
