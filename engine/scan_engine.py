from __future__ import annotations
import sys as _sys
for _m in ['bug_scan_core', 'std_core', 'ui_check_core', 'cov_scan', 'cross_taint', 'rust_scan', 'sage_scan']:
    _sys.modules.setdefault(_m, _sys.modules[__name__])



"""scan_engine.py — 扫描引擎（合并自 7 个扫描模块）。

结构：bug_scan（漏洞扫描）/ std_check（标准检查）/ ui_check（UI 检查）/
cov_scan（覆盖）/ cross_taint（污点）/ rust_scan / sage_scan。
新技术 = 往本模块增量加函数（不新建零散文件）。
"""

# ══════════════ bug_scan_core（合并） ══════════════
# -*- coding: utf-8 -*-
"""bug_scan_core —— Python 缺陷扫描引擎（2026-08-15 从 server.py 拆出）。

架构整改 R2-R3（拆上帝文件）：bug 扫描族（_bug_scan_file CC=60/_bug_scope_scan
CC=48/_bug_resource_leak CC=35/_scan_body CC=31）独立成模块——工具行为零变化。
"""
import ast
import builtins
import json
import os

# 引擎根（合并后 __file__ 在 engine/ 下——数据文件在仓库根）
_ENGINE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import re
import threading
from pathlib import Path


_MAX_READ = 1 << 20  # 单文件读取上限（1MB——防 OOM）

_BUG_BUILTINS = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__",
    "__annotations__", "__class__", "__dict__", "__main__", "self", "cls",
    "True", "False", "None", "NotImplemented", "Ellipsis",
}


# 报错定位正则：traceback 风格 `File "x.py", line 42, in foo` + 简洁风格 `x.py:42`
_TRACEBACK_RE = re.compile(r'File "([^"]+)", line (\d+)(?:, in ([^\n]+))?')
# IDE 增强 159：支持 .rs/.go/.ts/.js（原仅 .py——Rust panic 位置
# src/main.rs:2:5 无法定位，探针验证抓出）
# IDE 增强 272：对齐 22 语言（Java 栈帧 at App.run(App.java:12) 等）
_SIMPLE_POS_RE = re.compile(
    r'((?:[A-Za-z]:[\\/])?[^\s:()"]+\.(?:py|rs|go|ts|tsx|js|jsx|gd|c|cpp|h|hpp|'
    r'cs|lua|sh|bash|java|kt|kts|swift|php|rb|ps1|dart)):(\d+)(?::(\d+))?')


def _bug_is_open(node) -> bool:
    """识别 open(...) 调用（含 io.open / builtins.open 形式；排除 os.open——它配 os.close，review nit 修复）。"""
    if not (isinstance(node, ast.Call)):
        return False
    if isinstance(node.func, ast.Name) and node.func.id == "open":
        return True
    if isinstance(node.func, ast.Attribute) and node.func.attr == "open":
        # 排除 os.open（模块级 os）——其返回 fd 配 os.close，不是文件句柄泄漏
        return not (isinstance(node.func.value, ast.Name) and node.func.value.id == "os")
    return False


def _bug_const_zero(node) -> bool:
    """字面量 0（含 -0）检测。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and node.value == 0:
        return True
    return isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and _bug_const_zero(node.operand)


def _bug_seq_len(node) -> int | None:
    """字面量容器长度：List/Tuple/str Constant；非字面量返回 None（ast 不折叠，需分别识别）。"""
    if isinstance(node, (ast.List, ast.Tuple)):
        return len(node.elts)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return len(node.value)
    return None


def _bug_issue(path, node, rule, severity, msg, lines) -> dict:
    """构造 issue 记录（file/line/col 对齐 ast 节点）。"""
    line = getattr(node, "lineno", 0)
    return {"file": path, "line": line, "col": getattr(node, "col_offset", 0),
            "rule": rule, "severity": severity, "msg": msg,
            "snippet": lines[line - 1].strip() if 0 < line <= len(lines) else ""}


def _bug_direct_defs(node) -> set:
    """node 直接定义的名称（不深入嵌套函数/类内部；函数/类只算自身名字）。"""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return {node.name}
    if isinstance(node, ast.Lambda):
        return set()  # 参数由调用方并入
    if isinstance(node, ast.Import):
        return {a.asname or a.name.split(".")[0] for a in node.names}
    if isinstance(node, ast.ImportFrom):
        return {a.asname or a.name for a in node.names}
    defs = set()
    if isinstance(node, ast.ExceptHandler) and node.name:
        defs.add(node.name)
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
            defs.add(child.id)
        else:
            defs |= _bug_direct_defs(child)
    return defs


def _bug_func_args(node) -> set:
    """函数/方法/lambda 的形参名集合。"""
    args = {a.arg for a in node.args.args + node.args.kwonlyargs}
    if node.args.vararg:
        args.add(node.args.vararg.arg)
    if node.args.kwarg:
        args.add(node.args.kwarg.arg)
    return args


def _bug_is_none_guarded(child, none_vars, ancestors=None) -> bool:
    """短路保护检测：X is None or X.field / X is None and X.field 模式不报。

    沿祖先链（显式列表，IDE 增强 153——替代 _p 临时属性）向上找 BoolOp：
    若 X 解引用在 BoolOp 右支且左支有 'X is None'，则受保护。
    """
    chain = list(ancestors or [])
    while chain:
        cur = chain.pop(0)
        if isinstance(cur, ast.BoolOp):
            pos = None
            for i, v in enumerate(cur.values):
                # 递归包含（IDE 增强 153 二修）：`X.poll() is not None` 是
                # Compare 不是 Call——poll 深一层（Compare→Call→poll），
                # 直接子节点判断（child in _ast_children(v)）找不到 → 漏豁免
                if v is child or child in ast.walk(v):
                    pos = i
                    break
            if pos is None:
                return False
            for v in cur.values[:pos]:
                if isinstance(v, ast.Compare) and v.ops and isinstance(v.ops[0], ast.Is):
                    if isinstance(v.left, ast.Name) and v.left.id in none_vars:
                        return True
            return False
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module,
                            ast.ClassDef, ast.If, ast.While, ast.For)):
            return False
    return False


def _ast_children(n):
    return list(ast.iter_child_nodes(n))


def _bug_check_deref(node, none_vars, path, lines, issues, parents=None):
    """None 变量被解引用（属性/下标/调用）检测；线性近似，可能漏报/误报。

    IDE 增强 153（自扫抓出）：用显式祖先链（parents 列表）替代 _p 临时
    属性——_p 在多路径遍历下会被覆盖，短路保护检测（X is None or
    X.method()）时灵时不灵；显式链确定可靠。
    """
    ancestors = [node] + list(parents or [])
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.Attribute, ast.Subscript)) and isinstance(child.value, ast.Name) \
                and child.value.id in none_vars:
            if not _bug_is_none_guarded(child, none_vars, ancestors):
                issues.append(_bug_issue(path, child, "none_deref", "warning",
                                         f"'{child.value.id}' 可能为 None，此处解引用会抛异常", lines))
        elif isinstance(child, ast.Call) and isinstance(child.func, ast.Name) \
                and child.func.id in none_vars:
            if not _bug_is_none_guarded(child, none_vars, ancestors):
                issues.append(_bug_issue(path, child, "none_deref", "warning",
                                         f"'{child.func.id}' 可能为 None，调用会抛 TypeError", lines))
        _bug_check_deref(child, none_vars, path, lines, issues, parents=ancestors)


def _bug_check_seq(node, seq_vars, path, lines, issues):
    """字面量容器变量被字面量索引越界检测；线性近似。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.Subscript) and isinstance(child.value, ast.Name) \
                and child.value.id in seq_vars and isinstance(child.slice, ast.Constant) \
                and isinstance(child.slice.value, int):
            size, _ = seq_vars[child.value.id]
            idx = child.slice.value
            if idx >= size or idx < -size:
                issues.append(_bug_issue(path, child, "index_out_of_range", "error",
                                         f"索引 {idx} 越界（容器长度 {size}）", lines))
        else:
            _bug_check_seq(child, seq_vars, path, lines, issues)


def _bug_scope_scan(stmts, outer: set, path, lines, issues) -> set:
    """遍历一个作用域：未定义变量 + None 解引用。返回本作用域定义名集合。"""
    defs = set()
    for stmt in stmts:
        defs |= _bug_direct_defs(stmt)
    known = defs | outer | _BUG_BUILTINS

    # 设置 _p 父指针（短路保护检测需要父链）
    def _link(node, parent):
        node._p = parent  # type: ignore[attr-defined]
        for c in ast.iter_child_nodes(node):
            _link(c, node)
    for stmt in stmts:
        _link(stmt, None)

    # ── 未定义变量（跳过嵌套函数/类，由递归处理；lambda 参数并入）──
    def walk_names(node, extra):
        # 入口为函数/类时：只查装饰器与参数默认值，函数体交给作用域递归
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in list(node.decorator_list) + list(node.args.defaults) + list(node.args.kw_defaults):
                if d is not None:
                    walk_names(d, extra)
            return
        if isinstance(node, ast.ClassDef):
            for d in node.decorator_list:
                walk_names(d, extra)
            return
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(child, ast.Lambda):
                walk_names(child.body, extra | _bug_func_args(child))
                continue
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) \
                    and child.id not in known and child.id not in extra:
                issues.append(_bug_issue(path, child, "undefined_name", "warning",
                                         f"名称 '{child.id}' 可能未定义（未赋值/导入/参数）", lines))
            walk_names(child, extra)

    for stmt in stmts:
        walk_names(stmt, frozenset())

    # ── None 解引用 + 常量容器越界（线性跟踪当前作用域直接语句，分支近似）──
    none_vars = {}
    seq_vars = {}  # name -> (长度, 行号)：字面量容器变量
    for stmt in stmts:
        if isinstance(stmt, ast.Assign):
            val_none = isinstance(stmt.value, ast.Constant) and stmt.value.value is None
            # 别名传播只认"真别名"（X = Y，Y 当前为 None 变量）；
            # 函数/构造调用赋值（X = Foo()）绝不视为 None——修复误报（review 实测）
            val_alias = isinstance(stmt.value, ast.Name) and stmt.value.id in none_vars
            is_call_assign = isinstance(stmt.value, ast.Call)
            seq_len = _bug_seq_len(stmt.value)
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    if (val_none or val_alias) and not is_call_assign:
                        none_vars[t.id] = stmt.lineno
                    else:
                        none_vars.pop(t.id, None)
                    if seq_len is not None:
                        seq_vars[t.id] = (seq_len, stmt.lineno)
                    else:
                        seq_vars.pop(t.id, None)
        # 容器变异（append/extend/+=）后长度未知 → 清空该条目，防越界误报（review should-fix）
        if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call) \
                and isinstance(stmt.value.func, ast.Attribute) \
                and stmt.value.func.attr in ("append", "extend") \
                and isinstance(stmt.value.func.value, ast.Name):
            seq_vars.pop(stmt.value.func.value.id, None)
        if isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name) \
                and isinstance(stmt.op, ast.Add):
            seq_vars.pop(stmt.target.id, None)
        # try 块内联：body/handlers/orelse 的赋值流并入当前作用域——必须先于
        # deref 检查（修复 'X=None 后 X=Foo() 构造赋值仍报 none_deref' 误报：
        # try 内语句原本不参与外层线性跟踪，构造赋值无法清除 None 标记）
        if isinstance(stmt, ast.Try):
            for inner in stmt.body + stmt.handlers + stmt.orelse:
                inner_list = inner.body if isinstance(inner, ast.ExceptHandler) else [inner]
                for is2 in inner_list:
                    if isinstance(is2, ast.Assign):
                        iv = is2.value
                        icall = isinstance(iv, ast.Call)
                        for t in is2.targets:
                            if isinstance(t, ast.Name):
                                # 构造/函数调用赋值（X = Foo()）绝不视为 None；只有 X = None 才算
                                if isinstance(iv, ast.Constant) and iv.value is None:
                                    none_vars[t.id] = is2.lineno
                                else:
                                    none_vars.pop(t.id, None)
        if none_vars:
            _bug_check_deref(stmt, none_vars, path, lines, issues)
        if seq_vars:
            _bug_check_seq(stmt, seq_vars, path, lines, issues)

    # ── 递归嵌套函数/类（闭包可见外层定义）──
    for stmt in stmts:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            _bug_scope_scan(stmt.body, known | _bug_func_args(stmt), path, lines, issues)
        elif isinstance(stmt, ast.ClassDef):
            _bug_scope_scan(stmt.body, known, path, lines, issues)
    return defs


def _bug_resource_leak(tree, path, lines, issues):
    """open() 未用 with 且同作用域未见 .close() → resource_leak（按函数作用域隔离）。"""
    with_opens = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.With):
            for item in n.items:
                if _bug_is_open(item.context_expr):
                    with_opens.add(id(item.context_expr))

    def _scan_body(body):
        """扫描一个作用域体：仅函数/类分割 closed；块（if/for/while/try/with）共享同一集合，
        使 f=open(x) + try/finally: f.close() 教科书模式不误报（review 修复）。"""
        closed = {}  # name -> lineno（块共享，跨块配对）
        open_assigned = {}  # id(open 调用) -> (变量名, 赋值行号)

        def _collect(statements):
            for n in statements:
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and _bug_is_open(n.value):
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            open_assigned[id(n.value)] = (t.id, getattr(n, "lineno", 0))
                if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) \
                        and isinstance(n.value.func, ast.Attribute) \
                        and n.value.func.attr == "close" and isinstance(n.value.func.value, ast.Name):
                    closed[n.value.func.value.id] = getattr(n, "lineno", 0)

        def _check(statements):
            for n in statements:
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call) and _bug_is_open(n.value):
                    if id(n.value) in with_opens:
                        continue
                    tolerated = False
                    for t in n.targets:
                        if isinstance(t, ast.Name):
                            if closed.get(t.id, -1) > getattr(n, "lineno", 0):
                                tolerated = True  # 同作用域 close 在赋值后 → 容忍
                    if not tolerated:
                        issues.append(_bug_issue(path, n.value, "resource_leak", "warning",
                                                 "open() 未使用 with 语句，异常路径会泄漏文件句柄", lines))

        def _flatten_blocks(statements) -> list:
            """递归展开块语句为语句列表；函数/类不展开（保持递归锚点）。
            递归展开保证嵌套块（if 套 for、try 套 if 等）内语句被扫描（review 修复）。"""
            flat = []
            for n in statements:
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    flat.append(n)  # 保持为递归锚点
                elif isinstance(n, ast.With):
                    flat.extend(_flatten_blocks(n.body))
                elif isinstance(n, ast.For):
                    flat.extend(_flatten_blocks(list(n.body) + list(n.orelse)))
                elif isinstance(n, ast.While):
                    flat.extend(_flatten_blocks(list(n.body) + list(n.orelse)))
                elif isinstance(n, ast.If):
                    flat.extend(_flatten_blocks(list(n.body) + list(n.orelse)))
                elif isinstance(n, ast.Try):
                    flat.extend(_flatten_blocks(list(n.body) + list(n.orelse) + list(n.finalbody)))
                    for h in n.handlers:
                        flat.extend(_flatten_blocks(h.body))  # except 展开为 handler.body
                else:
                    flat.append(n)
            return flat

        flat = _flatten_blocks(body)
        _collect(flat)
        _check(flat)
        # 递归子作用域（仅函数/类）
        for n in flat:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                _scan_body(n.body)

    _scan_body(tree.body)


def _bug_scan_file(path: str) -> tuple[list, int]:
    """单文件 bug 扫描（模块级，可被 ProcessPoolExecutor pickle 进子进程）。

    返回 (issues, line_count)。大文件/读错/语法错误都转为结构化 issues 而非抛异常
    （子进程异常无法回传，全部就地消化）。
    """
    issues: list = []
    f = Path(path)
    try:
        size = f.stat().st_size
        if size > _MAX_READ:
            return ([{"file": str(f), "line": 0, "col": 0, "rule": "file_too_large",
                      "severity": "warning", "msg": f"文件过大（{size} 字节），跳过", "snippet": ""}], 0)
        src = f.read_text(encoding="utf-8", errors="replace")
        # 读后复核（TOCTOU：读取期间文件被替换增长，与 fs_read 一致，review nit 修复）
        if f.stat().st_size > _MAX_READ:
            return ([{"file": str(f), "line": 0, "col": 0, "rule": "file_too_large",
                      "severity": "warning", "msg": "文件读取后超限（>1MB），跳过", "snippet": ""}], 0)
    except OSError as exc:
        return ([{"file": str(f), "line": 0, "col": 0, "rule": "read_error",
                  "severity": "warning", "msg": f"读取失败: {exc}", "snippet": ""}], 0)
    lines = src.splitlines()
    try:
        tree = ast.parse(src, filename=str(f))
    except SyntaxError as exc:
        return ([{"file": str(f), "line": exc.lineno or 0, "col": exc.offset or 0,
                  "rule": "syntax_error", "severity": "error",
                  "msg": f"语法错误: {exc.msg}",
                  "snippet": lines[exc.lineno - 1].strip() if exc.lineno and exc.lineno <= len(lines) else ""}],
                len(lines))
    _bug_scope_scan(tree.body, set(), str(f), lines, issues)
    _bug_resource_leak(tree, str(f), lines, issues)
    # 挖漏洞增强（2026-08-15）：污点分析（Python 数据流——面对复杂漏洞；
    # 复用主解析的 tree，不再重复 ast.parse——纯收益无行为变化）
    issues.extend(py_taint_scan(src, str(f), lines, tree))
    try:
        from cross_taint import cross_taint_scan
        cross_taint_scan(tree, str(f), lines, issues)
    except ImportError:
        pass  # 跨函数模块缺失降级（函数内 taint 已覆盖）

    # 挖漏洞增强：模板规则 DSL（Nuclei 概念——外部 vuln_rules.json）
    ext_rules_scan(src, str(f), lines, issues)
    for n in ast.walk(tree):
        # 除零：字面量 0 分母（确定性）
        if isinstance(n, ast.BinOp) and isinstance(n.op, (ast.Div, ast.FloorDiv, ast.Mod)) \
                and _bug_const_zero(n.right):
            issues.append(_bug_issue(str(f), n, "divide_by_zero", "error",
                                     "除数为字面量 0，运行期必抛 ZeroDivisionError", lines))
        if isinstance(n, ast.AugAssign) and isinstance(n.op, (ast.Div, ast.FloorDiv, ast.Mod)) \
                and _bug_const_zero(n.value):
            issues.append(_bug_issue(str(f), n, "divide_by_zero", "error",
                                     "除数为字面量 0，运行期必抛 ZeroDivisionError", lines))
        # IDE 增强 102：空 except 吞错（静默吞异常——Python 常见隐患）
        if isinstance(n, ast.ExceptHandler):
            _body = [s for s in n.body
                     if not (isinstance(s, ast.Expr)
                             and isinstance(s.value, ast.Constant)
                             and isinstance(s.value.value, str))]
            if len(_body) == 1 and isinstance(_body[0], ast.Pass):
                # IDE 增强 105（自扫第二轮抓出）：except 行/上一行/pass 行任一
                # 带注释说明 → 设计性静默（日志失败/扫描失败尽力而为），不算吞错
                _pl = _body[0].lineno
                _exc_txt = lines[n.lineno - 1] if 0 < n.lineno <= len(lines) else ""
                _prev_txt = lines[n.lineno - 2] if n.lineno >= 2 else ""
                _pass_txt = lines[_pl - 1] if 0 < _pl <= len(lines) else ""
                if "#" not in _exc_txt and "#" not in _prev_txt \
                        and "#" not in _pass_txt:
                    issues.append(_bug_issue(
                        str(f), n, "swallowed_exception", "warning",
                        "空 except 吞错（pass）——静默吞异常，建议记录或显式处理", lines))
            # IDE 增强 103：except BaseException 过宽捕获（吞 KeyboardInterrupt/
            # SystemExit——程序无法 Ctrl-C 退出，运行期隐患）
            _exc_names: set[str] = set()
            if isinstance(n.type, ast.Name):
                _exc_names.add(n.type.id)
            elif isinstance(n.type, ast.Tuple):
                for _e in n.type.elts:
                    if isinstance(_e, ast.Name):
                        _exc_names.add(_e.id)
            if "BaseException" in _exc_names:
                issues.append(_bug_issue(
                    str(f), n, "overwide_except", "warning",
                    "except BaseException 过宽——吞 KeyboardInterrupt/SystemExit，"
                    "建议收窄到 Exception 或具体异常", lines))
            # IDE 增强 299：裸 except（无异常类型——吞所有异常含 KeyboardInterrupt/
            # SystemExit；即使有处理体也应写明异常类型）
            if n.type is None:
                issues.append(_bug_issue(
                    str(f), n, "bare_except", "warning",
                    "裸 except:（无异常类型）——吞所有异常，建议写明异常类型"
                    "（except (ValueError, TypeError):）", lines))
        # 越界：字面量容器 + 字面量索引（确定性）
        seq_len = _bug_seq_len(n.value) if isinstance(n, ast.Subscript) else None
        if seq_len is not None and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, int):
            idx = n.slice.value
            if idx >= seq_len or idx < -seq_len:
                issues.append(_bug_issue(str(f), n, "index_out_of_range", "error",
                                         f"索引 {idx} 越界（容器长度 {seq_len}）", lines))
        # IDE 增强 123：eval/exec 动态执行（任意代码注入风险——安全敏感）
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id in ("eval", "exec"):
            issues.append(_bug_issue(
                str(f), n, "dynamic_exec", "warning",
                f"{n.func.id}() 动态执行——输入不可信时任意代码注入，建议安全替代", lines))
        # IDE 增强 263：可变默认参数（def f(x=[])——跨调用共享同一对象——
        # 经典 Python bug——默认值应为 None+内部初始化）
        if isinstance(n, ast.FunctionDef) and n.args.defaults:
            for d, arg in zip(n.args.defaults, n.args.args[-len(n.args.defaults):]):
                if isinstance(d, (ast.List, ast.Dict, ast.Set)):
                    issues.append(_bug_issue(
                        str(f), n, "mutable_default_arg", "warning",
                        f"可变默认参数 `{arg.arg}=[.../{{}}/set`——跨调用共享同一"
                        f"对象（经典 bug）——建议 None+内部初始化", lines))
                    break
        # IDE 增强 124：shell 命令注入（os.system / subprocess shell=True）
        if isinstance(n, ast.Call):
            _fn = ""
            _obj = ""
            if isinstance(n.func, ast.Name):
                _fn = n.func.id
            elif isinstance(n.func, ast.Attribute):
                _fn = n.func.attr
                _obj = getattr(n.func.value, "id", "") if isinstance(n.func.value, ast.Name) else ""
            if _fn == "system" and _obj == "os":
                issues.append(_bug_issue(
                    str(f), n, "shell_injection", "warning",
                    "os.system() 执行 shell 命令——参数含用户输入时命令注入风险，"
                    "建议 subprocess 列表参数", lines))
            elif _fn in ("run", "Popen", "call", "check_output", "check_call") \
                    and any(isinstance(k, ast.keyword) and k.arg == "shell"
                            and isinstance(k.value, ast.Constant)
                            and k.value.value is True for k in n.keywords):
                issues.append(_bug_issue(
                    str(f), n, "shell_injection", "warning",
                    "subprocess shell=True——参数含用户输入时命令注入风险，"
                    "建议列表参数（无 shell）", lines))
            # IDE 增强 125：pickle 不可信反序列化（任意代码执行——安全敏感）
            elif _fn in ("loads", "load") and _obj == "pickle":
                issues.append(_bug_issue(
                    str(f), n, "unsafe_pickle", "warning",
                    f"pickle.{_fn}() 反序列化——数据不可信时任意代码执行，"
                    "建议 JSON/替代格式", lines))
            # IDE 增强 126：yaml.load 未指定 Loader（默认全功能 Loader——
            # 任意代码执行；应 yaml.safe_load）
            elif _fn == "load" and _obj == "yaml" \
                    and not any(k.arg == "Loader" for k in n.keywords):
                issues.append(_bug_issue(
                    str(f), n, "unsafe_yaml", "warning",
                    "yaml.load() 未指定 Loader——默认全功能 Loader 任意代码执行，"
                    "建议 yaml.safe_load()", lines))
            # IDE 增强 127：tarfile.extractall 路径穿越（CWE-22——恶意 tar
            # 成员可写到解压目录外，建议成员路径过滤；extractall 方法名独有，
            # 链式调用 tarfile.open(...).extractall() 也能命中）
            elif _fn == "extractall":
                issues.append(_bug_issue(
                    str(f), n, "tar_path_traversal", "warning",
                    "tarfile.extractall() 路径穿越风险——恶意 tar 成员可写到"
                    "解压目录外，建议过滤成员路径（../../ 拒绝）", lines))
        # 挖漏洞增强（2026-08-15——面对日益复杂的漏洞）：
        # 路径遍历：路径拼接含 ../（open/Path/os.path.join 等——CWE-22）
        if isinstance(n, ast.Call):
            _pfn = n.func.attr if isinstance(n.func, ast.Attribute) else (
                n.func.id if isinstance(n.func, ast.Name) else "")
            if _pfn in ("join", "open", "Path", "read_text", "write_text",
                        "unlink", "rename", "copy", "move"):
                for a in n.args:
                    if isinstance(a, ast.Constant) and isinstance(a.value, str) \
                            and ("../" in a.value or "..\\" in a.value
                                 or a.value.startswith("..")):
                        issues.append(_bug_issue(
                            str(f), n, "path_traversal", "warning",
                            f"路径拼接含 '../'（{_pfn}()）——CWE-22 路径穿越，"
                            "建议路径规范化/沙盒校验", lines))
                        break
        # assert 用于验证（security-review MEDIUM：全量 assert 报=噪音——
        # 只在 assert 行附近 3 行内有外部输入获取（args.get/input/getenv）
        # 时报——真实"用户输入校验被 -O 移除"场景；测试文件豁免）
        if isinstance(n, ast.Assert) \
                and "test_" not in str(f).replace("\\", "/").split("/")[-1]:
            _ctx = " ".join(lines[max(0, n.lineno - 4):n.lineno])
            if any(k in _ctx for k in ("args.get", "input(", "getenv",
                                       "environ", "request.", ".get(")):
                issues.append(_bug_issue(
                    str(f), n, "assert_validation", "hint",
                    "assert 用于用户输入校验——python -O 运行时 assert 被移除"
                    "（验证失效），关键校验建议显式 if+raise", lines))
        # 安全场景用 random（token/密码/密钥——应 secrets 模块）
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and getattr(n.func.value, "id", "") == "random" \
                and n.func.attr in ("random", "randint", "choice", "choices",
                                    "shuffle", "sample"):
            _ctx = " ".join(lines[max(0, n.lineno - 4):n.lineno]).lower()
            if any(k in _ctx for k in ("token", "password", "secret", "key",
                                       "密钥", "密码", "令牌")):
                issues.append(_bug_issue(
                    str(f), n, "insecure_random", "warning",
                    "random.* 用于安全场景（token/密码/密钥）——random 可预测，"
                    "建议 secrets 模块", lines))
        # 请求无超时（requests/urlopen——慢速攻击/挂起 DoS）
        if isinstance(n, ast.Call):
            _rfn = n.func.attr if isinstance(n.func, ast.Attribute) else (
                n.func.id if isinstance(n.func, ast.Name) else "")
            _robj = getattr(getattr(n.func, "value", None), "id", "") \
                if isinstance(n.func, ast.Attribute) else ""
            if _rfn in ("get", "post", "put", "delete", "patch") \
                    and _robj == "requests" \
                    and not any(k.arg == "timeout" for k in n.keywords):
                issues.append(_bug_issue(
                    str(f), n, "request_no_timeout", "warning",
                    "requests.* 无 timeout——慢速攻击/挂起（DoS），"
                    "建议 timeout=(3, 10)", lines))
            if _rfn == "urlopen" \
                    and not any(k.arg == "timeout" for k in n.keywords):
                issues.append(_bug_issue(
                    str(f), n, "request_no_timeout", "warning",
                    "urlopen() 无 timeout——挂起风险，建议 timeout=10", lines))
        # ReDoS：嵌套量词（(a+)+ / (a*)* / (a|a)*——指数回溯）
        if isinstance(n, ast.Constant) and isinstance(n.value, str) \
                and len(n.value) < 200:
            _p = n.value
            if re.search(r"\([^)]*[+*][^)]*\)[+*]", _p) \
                    or re.search(r"\([^)]*\|[^)]*\)\*", _p) \
                    or re.search(r"\([^)]*[+*?{][^)]*\)[+*{]", _p) \
                    or re.search(r"\([^)]*\|[^)]*\)\s*\{", _p) \
                    or re.search(r"\([^)]*\|[^)]*\)[+*{]", _p):
                _parent = lines[n.lineno - 1] if n.lineno <= len(lines) else ""
                if "re." in _parent or "compile" in _parent or "match" in _parent \
                        or "search" in _parent or "findall" in _parent:
                    issues.append(_bug_issue(
                        str(f), n, "regex_dos", "warning",
                        "正则嵌套量词（(a+)+ 类）——指数回溯 ReDoS，"
                        "建议原子分组/无嵌套量词写法", lines))
    return issues, len(lines)


# ── 挖漏洞增强（2026-08-15）：污点分析（Python 函数级数据流）────
_SINK_FUNCS = {
    "open": "文件读写", "eval": "动态执行", "exec": "动态执行",
    "system": "shell 命令", "run": "子进程", "Popen": "子进程",
    "loads": "反序列化", "load": "反序列化", "extractall": "解压",
    "remove": "文件删除", "unlink": "文件删除",
}  # 收窄（security-review MEDIUM：get/post/join/urlopen 太泛——合法业务
    # 模式误报爆炸）——网络类用独立规则（request_no_timeout）
_SOURCE_FUNCS = {
    "get": "外部参数", "getenv": "环境变量", "environ": "环境变量",
    "input": "用户输入", "argv": "命令行参数", "read": "文件内容",
    "recv": "网络接收", "body": "请求体", "query": "查询参数",
    "form": "表单数据", "headers": "请求头", "cookie": "Cookie",
    "json": "JSON 解析", "load": "配置加载",
}


def py_taint_scan(src: str, path: str, lines: list,
                  tree: "ast.AST | None" = None) -> list:
    """Python 函数级污点分析（面对日益复杂的漏洞——数据流而非单点规则）。

    函数内：外部源（args.get/input/getenv/request 参数）→ 危险 sink
    （open/eval/subprocess/反序列化/网络）——变量级跟踪 + 函数参数
    传播（跨函数 1 层）。确定性（AST 数据流——非启发）。

    tree: 复用调用方已解析的 AST（2026-08-15 速度优化——消除重复 parse）。
    """
    issues = []
    if tree is None:
        try:
            tree = ast.parse(src, filename=str(path))
        except SyntaxError:
            return issues

    # 函数参数收集（跨函数传播：main(args) → 函数内 args.get 使用）
    fn_params: dict[str, set] = {}
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            fn_params[n.name] = {a.arg for a in n.args.args}

    # parent 映射预建（security-review HIGH：每 Call 全树 walk 是 O(n²)——
    # 大文件分钟级卡死；单趟 iter_child_nodes O(n)）
    parent_map: dict[object, object] = {}
    for _pn in ast.walk(tree):
        for _ch in ast.iter_child_nodes(_pn):
            parent_map[_ch] = _pn

    # 每个函数体内污点流（source 变量 → sink 调用）
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        tainted: dict[str, int] = {}  # 变量 → 污染行
        # 1) 形参不无条件污染（security-review MEDIUM：全参数污染=误报
        #    爆炸；跨函数调用边传播标注为未来方向——函数内 source 已覆盖
        #    MCP 工具模式 handler(args)→args.get→sink）
        # 2) 遍历函数体语句
        for st in ast.walk(fn):
            # source：外部输入获取
            if isinstance(st, ast.Call):
                _fn = st.func.attr if isinstance(st.func, ast.Attribute) else (
                    st.func.id if isinstance(st.func, ast.Name) else "")
                _obj = getattr(getattr(st.func, "value", None), "id", "") \
                    if isinstance(st.func, ast.Attribute) else ""
                src_name = _fn if _obj in ("", "os", "sys", "flask",
                                           "request", "fastapi") else ""
                if src_name in _SOURCE_FUNCS or (_fn == "get" and _obj == "args"):
                    # 赋值目标：x = args.get(...)——parent 映射 O(1) 查找
                    parent = parent_map.get(st)
                    if isinstance(parent, ast.Assign):
                        for t in parent.targets:
                            if isinstance(t, ast.Name):
                                tainted[t.id] = st.lineno
            # sink：危险调用 + 参数含污点变量
            if isinstance(st, ast.Call):
                _fn2 = st.func.attr if isinstance(st.func, ast.Attribute) else (
                    st.func.id if isinstance(st.func, ast.Name) else "")
                if _fn2 in _SINK_FUNCS:
                    arg_names = set()
                    for a in st.args:
                        if isinstance(a, ast.Name):
                            arg_names.add(a.id)
                        elif isinstance(a, ast.JoinedStr):  # f-string 含污点
                            for v in ast.walk(a):
                                if isinstance(v, ast.Name) and v.id in tainted:
                                    arg_names.add(v.id)
                    hits = arg_names & set(tainted)
                    if hits:
                        issues.append({
                            "file": str(path), "line": st.lineno, "col": 0,
                            "rule": "taint_flow",
                            "severity": "warning",
                            "msg": (f"污点流：外部输入 {sorted(hits)[:3]} "
                                    f"(源 {sorted(set(tainted[h] for h in hits))[:2]}) "
                                    f"→ {_fn2}()（{_SINK_FUNCS[_fn2]}）——"
                                    f"输入不可信时注入/越权风险，建议校验/白名单"),
                            "snippet": (lines[st.lineno - 1].strip()[:80]
                                        if st.lineno <= len(lines) else ""),
                        })
    return issues


# ── 挖漏洞增强（2026-08-15）：模板规则 DSL（Nuclei 概念——确定性规则
#    规模化——不改代码加规则）─────────────────────────────
_RULES_CACHE: dict = {"mtime": 0.0, "rules": []}
_RULES_LOCK = threading.Lock()  # 2026-08-15：并发安全——多线程（bug_scan
# 并行/vuln_scan 三路）同时 load_ext_rules 读改缓存——dict 竞态防损


def _rules_path() -> str:
    """外部规则文件路径（env 可配——测试隔离/自定义规则集）。"""
    override = os.environ.get("UNIFIED_RX_VULN_RULES", "")
    if override.strip():
        return override
    base = _ENGINE_ROOT
    return os.path.join(base, "vuln_rules.json")


def load_ext_rules(force: bool = False) -> list:
    """加载外部模板规则（vuln_rules.json——{id, pattern, language, severity,
    msg} 列表——正则模式确定性检测；mtime 缓存防重复读）。"""
    p = _rules_path()
    with _RULES_LOCK:  # 2026-08-15：并发安全（多线程 load 竞态防损）
        try:
            mt = os.path.getmtime(p)
            if not force and mt == _RULES_CACHE["mtime"]:
                return _RULES_CACHE["rules"]
            data = json.loads(open(p, encoding="utf-8").read())
            rules = []
            for r in data.get("rules", []):
                if isinstance(r, dict) and r.get("id") and r.get("pattern"):
                    # 安全（security-review MEDIUM：嵌套量词 ReDoS 拒绝——
                    # (a+)+/(a|aa)+$/(a{1,3}){2,}$ 类——指数回溯卡死；
                    # 长度上限防超大 pattern）
                    pat = str(r["pattern"])
                    if len(pat) > 200:
                        continue
                    if re.search(r"\([^)]*[+*][^)]*\)[+*]", pat) \
                            or re.search(r"\([^)]*\|[^)]*\)\*", pat) \
                            or re.search(r"\(\s*[^)]*[+*][^)]*\s*\)\s*\{[^}]+\}", pat) \
                            or re.search(r"\([^)]*\|[^)]*\)\+\$", pat) \
                            or re.search(r"\([^)]*[+*?{][^)]*\)[+*{]", pat) \
                            or re.search(r"\([^)]*\|[^)]*\)\s*\{", pat) \
                            or re.search(r"\([^)]*\|[^)]*\)[+*{]", pat) \
                            or re.search(r"\(\([^)]*\)\)[+*{]", pat) \
                            or re.search(r"[^()]\{[^}]+\}\s*\{[^}]+\}", pat):
                        continue
                    try:
                        re.compile(pat)  # 试编译——非法模式跳过（防运行时 re.error）
                    except re.error:
                        continue
                    rules.append({
                        "id": str(r["id"])[:40],
                        "pattern": pat,
                        "language": str(r.get("language", "all")),
                        "severity": str(r.get("severity", "warning")),
                        "msg": str(r.get("msg", "外部规则命中"))[:120],
                        # 预编译（2026-08-15 速度优化）：行级扫描不再每次
                        # re.compile——rules 缓存按 mtime 失效，编译一次复用
                        "compiled": re.compile(pat),
                    })
            _RULES_CACHE["mtime"] = mt
            _RULES_CACHE["rules"] = rules
            return rules
        except (OSError, ValueError, TypeError, re.error):
            return []


def ext_rules_scan(src: str, path: str, lines: list, issues: list) -> None:
    """模板规则扫描：行级正则匹配（确定性——Nuclei 模板概念）。"""
    rules = load_ext_rules()
    if not rules:
        return
    ext = os.path.splitext(str(path))[1].lower()
    for r in rules:
        lang = r["language"]
        if lang != "all" and ext != lang:
            continue
        rx = r.get("compiled") or re.compile(r["pattern"])
        for i, line in enumerate(lines, 1):
            if rx.search(line):
                issues.append(_bug_issue(
                    str(path), None, r["id"], r["severity"],
                    r["msg"], lines))
                # 模板规则无 AST 节点——行号手动补（_bug_issue 的 node=None
                # 行号 0——这里直接改 issue 的行号）
                issues[-1]["line"] = i
                issues[-1]["snippet"] = line.strip()[:80]
                break  # 每规则每文件 1 条（防噪音）




# ══════════════ std_core（合并） ══════════════
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
        # 注释前缀按语言完整映射（IDE 增强 461：java/kt/swift/cs/dart 的 //、
        # gd/sh/bash/rb/ps1 的 #、lua 的 --——注释里的占位词不报）
        if path.endswith((".py", ".gd", ".sh", ".bash", ".rb", ".ps1")):
            _cp = "#"
        elif path.endswith((".lua",)):
            _cp = "--"
        else:
            _cp = "//"  # rs/go/ts/js/c/cpp/h/hpp/java/kt/swift/cs/dart 等
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
    # IDE 增强 466/467（security review 复检修复）：别名归一**只用于分支判断**——
    # 不改写 path 参数（issue 的 file 字段必须保留磁盘真实路径）
    _norm_ext = os.path.splitext(path)[1].lower()
    _norm_ext = {".cc": ".cpp", ".cxx": ".cpp", ".hh": ".hpp", ".hxx": ".hpp",
                 ".bash": ".sh", ".zsh": ".sh"}.get(_norm_ext, _norm_ext)
    _branch = os.path.splitext(path)[0] + _norm_ext  # 仅 endswith 判断用
    if not (_branch.endswith(".py")):
        # IDE 增强 108/118/166：ts/js/tsx/jsx + go + gd 文本启发——模块级重复声明
        # （function/class/const/let/var/func 同名 → 重复定义；gd 的 func 与 go 同构）
        # IDE 增强 258：c/cpp 并入（函数声明同名 → 重复定义；c 的 static 函数重名是错误）
        # IDE 增强 462：php/sh/bash 并入（php 的 function/class、sh 的 name() 重复声明）
        if _branch.endswith((".ts", ".tsx", ".js", ".jsx", ".go", ".gd",
                          ".php", ".sh", ".bash")):
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
                    # IDE 增强 462/466：php（function/class）与 sh/bash（name()）重复声明。
                    # security review 修复：php 只匹配**裸** `function name(`（类方法带
                    # public/private/protected 修饰——多类同名方法合法，不报）
                    m2 = re.match(
                        r"\s*function\s+([A-Za-z_][\w]*)\s*\("
                        r"|^\s*(?:final\s+|abstract\s+)*class\s+([A-Za-z_][\w]*)\b"
                        r"|^\s*([A-Za-z_][\w]*)\s*\(\s*\)\s*(?:\{|$)",
                        line)
                    if not m2:
                        continue
                    name = next((g for g in m2.groups() if g), "")
                else:
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
    if _branch.endswith((".c", ".cpp", ".h", ".hpp")):
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
    if _branch.endswith((".dart",)):
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
    if _branch.endswith((".java",)):
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
    if _branch.endswith((".kt", ".kts")):
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
    if _branch.endswith((".swift",)):
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
    if _branch.endswith((".rb", ".lua", ".ps1", ".cs")):
        # IDE 增强 317：rb/lua/ps1/cs 重复定义（def/function/func 同名——
        # name_conflict 全语言收官）
        count = 0
        seen: dict = {}
        _cs = _branch.endswith(".cs")
        for i, line in enumerate(src.splitlines(), 1):
            if line.lstrip().startswith(("//", "#", "--", "*")):
                continue
            if _cs:
                # cs 方法声明（返回类型+名——对齐 java）
                m = re.match(
                    r"\s*(?:public\s+|private\s+|internal\s+|protected\s+)*"
                    r"(?:static\s+|virtual\s+|override\s+|async\s+)*"
                    r"[A-Za-z_<>\[\],\s]*\s+([A-Za-z_]\w*)\s*\([^)]*\)\s*\{", line)
            else:
                m = re.match(
                    r"\s*(?:def|function|func)\s+([A-Za-z_][\w-]*)", line)
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
    # IDE 增强 466/467/468：别名归一只用于分支判断（第三轮 review：
    # 不改写 path——issue.file 保留磁盘真实路径；dead_code 目前无 c/cpp/sh
    # 分支（消费者为空）——归一保留为未来分支预留）
    _ext = os.path.splitext(path)[1].lower()
    if _ext in (".cc", ".cxx", ".hh", ".hxx", ".bash", ".zsh"):
        _root, _ = os.path.splitext(path)
        path = _root + {".cc": ".cpp", ".cxx": ".cpp", ".hh": ".hpp", ".hxx": ".hpp",
                        ".bash": ".sh", ".zsh": ".sh"}[_ext]
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
        # IDE 增强 463/466：cs/dart/php 未使用 import——security review 修复：
        # cs 的 `using System;`（命名空间）短名引用无法静态判定（Console 不带 System.
        # 前缀）——只查**含点** using（`using System.IO;`→`IO.` 前缀引用）；
        # dart 查 `base.` 前缀引用；swift 无前缀直接用（UIView 不带 UIKit.）——
        # 无法判定 → 去掉 swift 分支（宁可不报不误报）
        if path.endswith((".cs", ".dart", ".php")):
            count = 0
            for i, line in enumerate(src.splitlines(), 1):
                if line.lstrip().startswith(("//", "#", "/*", "*", "--")):
                    continue
                m = None
                if path.endswith(".cs"):
                    m = re.match(r"\s*using\s+([A-Za-z_][\w.]*)\s*;", line)
                    if m and "." not in m.group(1):
                        continue  # using System; 类命名空间——短名引用无法判定
                elif path.endswith(".dart"):
                    m = re.match(r"\s*import\s+['\"]([^'\"]+)['\"]", line)
                    if m:
                        base = m.group(1).split("/")[-1].split(".")[0]
                        if not base:
                            continue
                        # 引用 = `base.` 前缀——**只搜 import 行之后**（import 行
                        # 自身 `bar.dart` 命中 `bar.`——security review 复检修复）
                        rest = "\n".join(src.splitlines()[i:])
                        if not re.search(rf"\b{re.escape(base)}\s*\.", rest):
                            issues.append({
                                "rule": "dead_code", "severity": "Warning",
                                "line": i, "msg": f"未使用 import：`{base}`（文件内零引用）",
                                "file": path})
                            count += 1
                            if count >= limit:
                                return
                        continue
                elif path.endswith(".php"):
                    # 第四轮 review LOW：剥离 use function/use const 前缀（否则
                    # 捕获 function/const 作 name）；as 别名取别名（用 B 时误报）
                    m = re.match(
                        r"\s*use\s+(?:function\s+|const\s+)?"
                        r"([A-Za-z_\\][\w\\]*)\s*(?:as\s+([A-Za-z_][\w]*))?\s*;",
                        line)
                if not m:
                    continue
                # IDE 增强 469：php 正则才有组 2（as 别名）——cs/dart 正则只有
                # 组 1——m.group(2) 对 cs/dart 抛 no such group（探针 9 文件抓出）
                if path.endswith(".php"):
                    name = m.group(2) or m.group(1).split(".")[-1].split("\\")[-1].strip()
                else:
                    name = m.group(1).split(".")[-1].split("\\")[-1].strip()
                if not name or name == "*" or name.endswith(".*"):
                    continue  # 通配导入豁免
                if path.endswith(".cs"):
                    # cs：含点 using 的短名（System.IO→IO）——引用是 `IO.` 前缀；
                    # 阈值 ==0（using 行自身无 `IO.`——security review 复检修复：
                    # 恰好一次完全限定引用 System.IO.File 不误报）
                    pat = rf"\b{re.escape(name)}\s*\."
                    if len(re.findall(pat, src)) == 0:
                        issues.append({
                            "rule": "dead_code", "severity": "Warning",
                            "line": i, "msg": f"未使用 import：`{name}`（文件内零引用）",
                            "file": path})
                        count += 1
                        if count >= limit:
                            return
                    continue
                # php：use 行自身命中 1 次——引用 >1 才不报（第三轮 review 回归修复）
                if len(re.findall(rf"\b{re.escape(name)}\b", src)) == 1:
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


# ══════════════ ui_check_core（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""Bevy UI 静态检查器（程序驱动，非 skill）——ui_check 工具核心。

检查 Bevy ECS UI 代码的常见崩溃/不可见模式：
  - ui_root_missing   : spawn UI 但无 Node 组件
  - camera_missing    : 有 UI spawn 但无 Camera（3D/2D）
  - mode_isolation    : 编辑模式/演示模式 UI 未隔离（模式切换时未隐藏）
  - focus_pass        : 全屏 Node 无 FocusPolicy::Pass（点击被吞）
  - font_missing      : Text 组件无字体兜底（中文字体缺失白屏）
  - z_ordering        : 绝对定位重叠且无层级（z_index）

纯文本扫描（正则 + 简单状态跟踪），零依赖，适配 unified-rx 契约。
"""


_RULES = {
    "ui_root_missing": ("error", "spawn UI 节点但无 Node 组件（UI 不会渲染）"),
    "camera_missing": ("error", "存在 UI 但无相机（UI 不可见）"),
    "mode_isolation": ("warning", "编辑模式/演示模式 UI 未隔离（模式切换未隐藏）"),
    "focus_pass": ("warning", "全屏 Node 无 FocusPolicy::Pass（点击被 UI 吞掉）"),
    "font_missing": ("warning", "Text 组件无字体兜底（CJK 字体缺失会白屏/方框）"),
    "z_ordering": ("warning", "绝对定位重叠且无 z_index 层级（遮挡/闪烁）"),
}

# Node spawn 且带 PositionType::Absolute 的节点
_ABSOLUTE_RE = re.compile(r"PositionType::Absolute")
# FocusPolicy 设置
_FOCUS_PASS_RE = re.compile(r"FocusPolicy::Pass")
# Text 组件
_TEXT_RE = re.compile(r"Text(::|Bundle|\b)")
# 字体资源
_FONT_RE = re.compile(r"Font\(|font:|UiCjkFont|Font::default|asset_server\.load\(.*font|\.insert\(font|insert\(.*font\)|font,|font\)")
# 相机
_CAMERA_RE = re.compile(r"Camera3d|Camera2d|Camera \{\}|UiCamera")
# 编辑模式标记（编辑模式隔离的常见命名）
_EDIT_MARK_RE = re.compile(r"editor_|EditorMode|edit_mode|is_editing")
# spawn UI 节点
_SPAWN_UI_RE = re.compile(r"spawn\((?:Node|Text|Button|Image|Panel|Bar|Slot|Sprite|NodeBundle)")
# z_index
_Z_RE = re.compile(r"z_index|ZIndex")

# Bevy UI 组件标记（UI 特有的 Component 派生）
_UI_COMPONENT_RE = re.compile(r"#\[derive\(Component[^\]]*\)\]\s*\n\s*pub struct (HudRoot|.*Panel.*|.*Button.*|.*Text|.*Bar|.*Slot|.*Inventory|.*Menu)")


def scan_ui_source(src: str, path: str = "", dir_mode: bool = False) -> list[dict]:
    """扫描单个 Rust 文件，返回 issue 列表（unified-rx 契约：{rule,severity,line,msg}）。
    dir_mode=True 时跳过文件级 camera 提示（目录模式由 scan_ui_dir 聚合检查）。"""
    issues = []
    lines = src.splitlines()
    _last_z_rep = [0]  # z_ordering 去重状态：上次报告行号（for 循环内可变，用列表容器）

    # 相机存在性（文件级）
    has_camera = bool(_CAMERA_RE.search(src))
    # UI spawn 存在性
    has_ui = bool(_SPAWN_UI_RE.search(src)) or bool(_UI_COMPONENT_RE.search(src))

    for i, line in enumerate(lines, 1):
        # ui_root_missing: spawn 块里只有 Component 标记没有 Node
        if re.search(r"spawn\([^)]*\)", line) and "Node" not in line:
            # 检查接下来几行是否有 Node（spawn 多行形式）或 Node 样式函数（panel_node_style() 等）
            block = "\n".join(lines[max(0, i - 1) : i + 6])
            style_fn = re.search(r"spawn\(([a-z_]+)\(", line)
            has_node_style = bool(style_fn and re.search(r"node|style|panel|root|container", style_fn.group(1)))
            if has_node_style:
                continue  # spawn(node_style_fn()) 视为有 Node（误报修复）
            if "Node" not in block and re.search(r"\.insert\([^)]*[A-Z][a-zA-Z]+\)", block):
                # 排除纯逻辑 spawn（无 UI 标记组件）
                if _UI_COMPONENT_RE.search(block) or re.search(r"Text|Button|Panel|Bar|Slot", block):
                    issues.append({"rule": "ui_root_missing", "severity": "error",
                                   "line": i, "msg": "spawn UI 但未见 Node 组件"})

        # focus_pass: 全屏绝对定位 Node 无 FocusPolicy::Pass（块检测，兼容多行 Node 写法）
        if "PositionType::Absolute" in line:
            # 2026-08-14 补齐：等号写法（style.width = Val::Percent）+ 窗口前移
            # 3 行（width/height 常写在 position 前——Bevy style 赋值顺序任意）
            # + FocusPolicy 检查限当前节点（原实现块跨节点——第二个覆盖层的
            # FocusPolicy 会豁免第一个，同文件双覆盖层漏检）
            block = "\n".join(lines[max(0, i - 3) : i + 10])
            nxt = next((j for j in range(i + 1, min(len(lines), i + 30))
                        if "PositionType::Absolute" in lines[j]), len(lines))
            node_block = "\n".join(lines[i:nxt])
            if re.search(r"width\s*[:=]\s*Val::Percent\(100", block) \
                    and "FocusPolicy" not in node_block:
                issues.append({"rule": "focus_pass", "severity": "warning",
                               "line": i, "msg": "全屏绝对定位 Node 无 FocusPolicy::Pass（点击穿透）"})

        # z_ordering: 多个绝对定位且无 z_index（last_reported 行号去重，防重复且不丢边缘场景）
        if "PositionType::Absolute" in line:
            block = "\n".join(lines[max(0, i - 30) : i + 30])
            abs_count = len(_ABSOLUTE_RE.findall(block))
            if abs_count >= 3 and not _Z_RE.search(block):
                # 去重：30 行内已报过则跳过（相邻节点只报 1 条；跨度 31-59 行不丢失）
                if i - _last_z_rep[0] >= 30 or _last_z_rep[0] == 0:
                    _last_z_rep[0] = i
                    issues.append({"rule": "z_ordering", "severity": "warning",
                                   "line": i, "msg": "多个绝对定位节点无 z_index 层级"})

        # font_missing: Text 使用但无字体资源（行级）
        if "Text::new" in line or "Text(" in line or ".insert(Text" in line:
            block = "\n".join(lines[max(0, i - 5) : i + 10])
            if not _FONT_RE.search(block):
                issues.append({"rule": "font_missing", "severity": "warning",
                               "line": i, "msg": "Text 无字体兜底（CJK 缺失会方框/白屏）"})

        # IDE 增强 121/164：交互缺失——Button 系 spawn 无交互处理
        # （死按钮；164：支持 UiButton/TextButton/ImageButton/IconButton 变体；
        # 只查 spawn 行本身——块检查会误吃相邻按钮的 Interaction）
        if re.search(r"\bspawn\([^)]*(?:Button|Btn)", line) \
                or re.search(r"\bUi(?:Button|TextButton|ImageButton|IconButton)\b", line):
            if not re.search(r"Interaction|on_press|on_click|Pressed|Clicked|listener|"
                             r"\.clicked|pressed\s*\(|Released", line):
                issues.append({"rule": "no_interaction", "severity": "warning",
                               "line": i, "msg": "Button 无交互处理（Interaction/点击事件）——死按钮"})

    # 文件级 camera 检查：camera 在别的文件 → 单文件模式降级提示（目录模式在 scan_ui_dir 聚合）
    if not dir_mode and has_ui and not has_camera:
        issues.append({"rule": "camera_missing", "severity": "warning",
                       "line": 0, "msg": "文件含 UI 但未见相机（单文件模式——建议用目录扫描确认）"})

    # mode_isolation: 编辑模式标记存在但 UI 组件未在模式检查中隐藏（加 has_ui 门控防纯逻辑文件误报）
    if has_ui and _EDIT_MARK_RE.search(src) and not re.search(r"Hidden|Visible|visibility|despawn|toggle", src):
        issues.append({"rule": "mode_isolation", "severity": "warning",
                       "line": 0, "msg": "编辑模式标记存在但未见 UI 显隐逻辑（模式隔离缺失）"})

    return issues


def _scan_dart_ui(src: str, path: str) -> list[dict]:
    """Flutter（.dart）UI 检查（IDE 增强 274：Button 系无 onPressed——
    死按钮；四引擎 Bevy/Godot/Unity/Flutter）。窗口 = 创建行 + 后 2 行。"""
    import re as _re
    issues = []
    _btn = _re.compile(r"\b(?:TextButton|ElevatedButton|OutlinedButton|"
                       r"IconButton|FilledButton)\s*\(")
    _press = _re.compile(r"onPressed|onPressed\s*:|onLongPress")
    lines = src.splitlines()
    for i, line in enumerate(lines, 1):
        if not _btn.search(line):
            continue
        block = "\n".join(lines[i - 1:i + 3])
        if not _press.search(block):
            issues.append({
                "rule": "no_interaction", "severity": "warning", "line": i,
                "msg": "Button 无 onPressed 处理（onPressed 缺失）——死按钮",
                "file": path})
    return issues


def _scan_cs_ui(src: str, path: str) -> list[dict]:
    """Unity（.cs）UI 检查（IDE 增强 267：Button 创建无 onClick 连接——
    死按钮；对齐 Bevy/Godot）。窗口 = 创建行 + 后 2 行。"""
    import re as _re
    issues = []
    _btn = _re.compile(r"\bButton\b")
    _click = _re.compile(r"onClick|on_click|AddListener|\.onClick\.AddListener|"
                         r"clicked\s*=|Click\s*\+=")
    lines = src.splitlines()
    for i, line in enumerate(lines, 1):
        if not _btn.search(line):
            continue
        if "using " in line and "Button" not in line.split("using ")[-1]:
            continue  # import 行非按钮创建
        block = "\n".join(lines[i - 1:i + 2])
        if not _click.search(block):
            issues.append({
                "rule": "no_interaction", "severity": "warning", "line": i,
                "msg": "Button 无点击处理（onClick.AddListener 缺失）——死按钮",
                "file": path})
    return issues


def _scan_gd_ui(src: str, path: str) -> list[dict]:
    """Godot（.gd）UI 检查（IDE 增强 257：用户点名"没有多语言处理 包括扫描"——
    Bevy 之外的游戏 UI 同样检查）。

    规则：Button/TextureButton 创建后无 pressed 连接（死按钮——
    对齐 Bevy no_interaction）。块窗口 8 行（spawn 后找连接）。"""
    import re as _re
    issues = []
    _btn = _re.compile(r"\b(?:Button|TextureButton)\.new\(\)|"
                       r"add_child\([^)]*[Bb]utton\)")
    _pressed = _re.compile(r"pressed\.connect|_pressed\b|\.pressed\s*=|"
                           r"connect\(\s*[\"']pressed")
    lines = src.splitlines()
    for i, line in enumerate(lines, 1):
        if not _btn.search(line):
            continue
        # 窗口 = 创建行 + 后 2 行（slice [i-1:i+2]——3 行；Godot 常见
        # `var b = Button.new()` 下一行 connect；宽窗口会把后续按钮的
        # 连接误算进来"救活"死按钮）
        block = "\n".join(lines[i - 1:i + 2])
        if not _pressed.search(block):
            issues.append({
                "rule": "no_interaction", "severity": "warning", "line": i,
                "msg": "Button 无按下处理（pressed.connect 缺失）——死按钮",
                "file": path})
    return issues


def scan_ui_dir(root: str, max_files: int = 100) -> list[dict]:
    """扫描目录下 .rs（Bevy）与 .gd（Godot）文件（限 max_files）；
    聚合检查相机存在性（目录级）。"""
    import os
    issues = []
    files = []
    gd_files = []
    any_ui = False
    any_camera = False
    for r, _, names in os.walk(root):
        for n in sorted(names):
            if n.endswith(".rs"):
                files.append(os.path.join(r, n))
            elif n.endswith(".gd"):
                gd_files.append(os.path.join(r, n))
            elif n.endswith(".cs"):
                # IDE 增强 267：Unity（.cs）UI 文件
                gd_files.append(os.path.join(r, n))  # 复用 gd 收集桶（下方按扩展分发）
            elif n.endswith(".dart"):
                # IDE 增强 274：Flutter（.dart）UI 文件
                gd_files.append(os.path.join(r, n))
            if len(files) + len(gd_files) >= max_files:
                break
        if len(files) + len(gd_files) >= max_files:
            break
    # Godot/Unity UI 规则（.gd/.cs——Bevy 规则不适用）
    for f in gd_files:
        try:
            size = os.path.getsize(f)
            if size > (1 << 20):
                continue
            with open(f, encoding="utf-8", errors="replace") as fh:
                gsrc = fh.read()
        except OSError:
            continue
        if f.endswith(".cs"):
            for iss in _scan_cs_ui(gsrc, f):
                issues.append(iss)
        elif f.endswith(".dart"):
            for iss in _scan_dart_ui(gsrc, f):
                issues.append(iss)
        else:
            for iss in _scan_gd_ui(gsrc, f):
                issues.append(iss)
    for f in files:
        try:
            size = os.path.getsize(f)
            if size > (1 << 20):
                issues.append({"file": f, "rule": "file_too_large", "severity": "warning",
                               "line": 0, "msg": "文件过大跳过"})
                continue
            with open(f, "r", encoding="utf-8", errors="replace") as fh:
                src = fh.read()
        except OSError as exc:
            issues.append({"file": f, "rule": "read_error", "severity": "warning",
                           "line": 0, "msg": f"读取失败: {exc}"})
            continue
        if _SPAWN_UI_RE.search(src) or _UI_COMPONENT_RE.search(src):
            any_ui = True
        if _CAMERA_RE.search(src):
            any_camera = True
        for issue in scan_ui_source(src, f, dir_mode=True):
            issue["file"] = f
            issues.append(issue)
    # 目录级相机检查（跨文件聚合，防单文件误报）
    if any_ui and not any_camera:
        issues.append({"file": root, "rule": "camera_missing", "severity": "error",
                       "line": 0, "msg": "目录含 UI 但未见相机（UI 不可见）"})
    return issues


# ══════════════ cov_scan（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""cov_scan —— 代码覆盖率分析（阶段3，定位"从未执行的代码"=隐形炸弹）。

两级模式：
  static（默认，零依赖，确定性）：
    - Python AST：全库符号引用表 → 从未被引用的顶层函数/类/常量
      （死代码候选）+ 未使用的 import——"选择性插桩"的静态等价
    - 排除：__init__/main/下划线开头/typing 导入/测试文件自身
  dynamic（opt-in，需 coverage.py）：
    - subprocess `coverage run -m pytest` → `coverage report --skip-covered`
    - 输出未覆盖文件/行 TOP + 建议补测点
    失败自动降级 static（cov 不可用/无测试时诚实降级，不假装覆盖数据）

跨语言：Python 全量 AST；Rust/其他文件只统计（提示可用 vuln_scan/llvm-cov）。
"""

import subprocess
import sys


# 忽略的符号前缀/名字（框架入口/魔法方法）
_IGNORE_PREFIX = ("_", "test_", "test")
_IGNORE_NAMES = {"main", "setup", "teardown", "app", "server", "create_app",
                 "get_app", "run", "start", "main_loop"}


def _iter_py_files_cov(root: str, limit: int = 2000):
    """递归收集 .py 文件（跳过常见噪音目录）。"""
    skip = {".git", "node_modules", "__pycache__", ".venv", "venv", "target",
            "vendor", ".pytest_cache", "build", "dist", ".unified-rx-index"}
    count = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)
                count += 1
                if count >= limit:
                    return


def _collect_symbols(files: list[str]) -> tuple[dict, dict]:
    """两遍：定义表 {symbol: [file:line]} + 引用表 {symbol: count}。"""
    defined: dict[str, list[dict]] = {}
    used: dict[str, int] = {}
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                src = f.read()
            tree = ast.parse(src)
        except (SyntaxError, OSError):
            continue
        # 定义（顶层 def/class/Assign/AsyncFunctionDef）
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.setdefault(node.name, []).append(
                    {"file": path, "line": node.lineno})
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        defined.setdefault(t.id, []).append(
                            {"file": path, "line": node.lineno})
        # 赋值目标集合（Assign/AnnAssign/AugAssign 的 target 不算引用）
        targets: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                tgt = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in tgt:
                    for sub in ast.walk(t):
                        if isinstance(sub, ast.Name):
                            targets.add(sub.id)
        # 引用（全树 Name/Attribute 的 id，排除赋值目标）
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                if node.id not in targets:
                    used[node.id] = used.get(node.id, 0) + 1
            elif isinstance(node, ast.Attribute):
                used[node.attr] = used.get(node.attr, 0) + 1
            elif isinstance(node, ast.Import):
                for a in node.names:
                    used[a.asname or a.name.split(".")[0]] = \
                        used.get(a.asname or a.name.split(".")[0], 0) + 1
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    used[a.asname or a.name] = \
                        used.get(a.asname or a.name, 0) + 1
    return defined, used


def _unused_imports(files: list[str]) -> list[dict]:
    """未使用的 import（模块级导入名未被引用）。"""
    out = []
    for path in files:
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                src = f.read()
            tree = ast.parse(src)
        except (SyntaxError, OSError):
            continue
        used = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                used.add(node.id)
            elif isinstance(node, ast.Attribute):
                used.add(node.attr)
        for node in tree.body:
            names = []
            if isinstance(node, ast.Import):
                names = [a.asname or a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [a.asname or a.name for a in node.names]
            for n in names:
                if n not in used and not n.startswith("_"):
                    out.append({"file": path, "line": node.lineno,
                                "symbol": n, "kind": "unused_import"})
    return out


def cov_scan(path: str, mode: str = "static", limit: int = 2000) -> dict:
    """覆盖率/死代码扫描主入口。mode: static | dynamic | auto"""
    if not os.path.isdir(path):
        return {"ok": False, "error": f"路径不存在: {path}"}
    files = list(_iter_py_files_cov(path, limit))
    result: dict = {"ok": True, "path": path, "mode": mode,
                    "py_files": len(files)}

    # ── 动态覆盖（coverage.py，失败诚实降级） ─────────────────
    if mode in ("dynamic", "auto"):
        try:
            import coverage  # noqa: F401
            cov = _run_coverage(path)
            if cov is not None:
                result.update(cov)
                result["mode"] = "dynamic"
                return result
        except ImportError:
            pass
        if mode == "dynamic":
            result["mode"] = "static"
            result["degraded"] = "coverage.py 不可用或无 pytest 测试——降级静态分析"

    # ── 静态死代码（零依赖） ─────────────────────────────────
    defined, used = _collect_symbols(files)
    dead = []
    for sym, locs in defined.items():
        if sym in _IGNORE_NAMES or sym.startswith(_IGNORE_PREFIX):
            continue
        if used.get(sym, 0) == 0:  # 零真实引用（定义处不算）
            for loc in locs[:3]:
                dead.append({"file": loc["file"], "line": loc["line"],
                             "symbol": sym, "kind": "never_referenced"})
    unused_imp = _unused_imports(files)
    result["dead_code"] = dead[:100]
    result["unused_imports"] = unused_imp[:100]
    result["dead_count"] = len(dead)
    result["unused_import_count"] = len(unused_imp)
    result["hint"] = ("'从未被执行的代码'是隐形炸弹——dead_code 为从未被引用的"
                      "顶层符号，建议确认后删除或补测试；动态覆盖用 mode=dynamic")
    return result


def _run_coverage(root: str) -> dict | None:
    """coverage run -m pytest → report（未覆盖 TOP）。耗时操作，超时保护。"""
    try:
        import coverage
        # 找测试入口
        test_cmd = ["python", "-m", "pytest", root, "-q", "--no-header"]
        r = subprocess.run(["python", "-m", "coverage", "run", "--branch",
                            "-m", "pytest", root, "-q", "--no-header",
                            "-x", "--timeout=120"],
                           capture_output=True, text=True, timeout=300,
                           encoding="utf-8", errors="replace")
        if r.returncode not in (0, 1):  # 1=有测试失败（仍可出覆盖报告）
            return None
        rep = subprocess.run(["python", "-m", "coverage", "report",
                              "--skip-covered", "--format=json"],
                             capture_output=True, text=True, timeout=60,
                             encoding="utf-8", errors="replace")
        if rep.returncode != 0:
            return None
        data = json.loads(rep.stdout)
        files_ = data.get("files", {})
        total = data.get("totals", {})
        uncovered = []
        for fname, finfo in files_.items():
            s = finfo.get("summary", {})
            missing = finfo.get("missing_lines", [])
            if s.get("missing_lines", 0) > 0:
                uncovered.append({"file": fname,
                                  "percent_covered": s.get("percent_covered", 0),
                                  "missing": len(missing),
                                  "missing_lines": missing[:20]})
        uncovered.sort(key=lambda x: x["percent_covered"])
        return {
            "dynamic": True,
            "coverage_percent": total.get("percent_covered", 0),
            "covered_lines": total.get("covered_lines", 0),
            "missing_lines": total.get("missing_lines", 0),
            "uncovered_files_top": uncovered[:20],
            "hint": "uncovered_files_top 为覆盖最差文件——优先补测",
        }
    except Exception:  # noqa: BLE001
        return None


if __name__ == "__main__":  # CLI 调试入口
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    mode = sys.argv[2] if len(sys.argv) > 2 else "static"
    print(json.dumps(cov_scan(path, mode), ensure_ascii=False, indent=1))


# ══════════════ cross_taint（合并） ══════════════



_SINKS = ("open", "eval", "system", "Popen", "execute")


def cross_taint_scan(tree: "ast.AST", path: str, lines: list,
                     taint_results: list) -> None:
    """调用边污点传播：调用方污点实参 → 被调形参 → 被调内 sink。"""
    if taint_results is None:
        return
    # 预构建：函数签名 / 函数节点 / 函数内 sink 调用（各一次全树 walk）
    fns: dict[str, list[str]] = {}
    fn_sinks: dict[str, list] = {}
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # setdefault：与原实现（walk 首个同名定义）一致——重复定义取首定义
        fns.setdefault(n.name, [a.arg for a in n.args.args])
        sinks: list = []
        for s in ast.walk(n):
            if not isinstance(s, ast.Call):
                continue
            _fn = ""
            if isinstance(s.func, ast.Attribute):
                _fn = s.func.attr
            elif isinstance(s.func, ast.Name):
                _fn = s.func.id
            if _fn in _SINKS:
                sinks.append(s)
        fn_sinks.setdefault(n.name, sinks)
    # 每个函数：污点变量 = 形参 + 函数内 source 赋值（args.get/input）
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        tainted = {a.arg for a in fn.args.args}
        for st in ast.walk(fn):
            if isinstance(st, ast.Assign) and isinstance(st.value, ast.Call):
                _v = st.value
                _fnv = _v.func.attr if isinstance(_v.func, ast.Attribute) else (
                    _v.func.id if isinstance(_v.func, ast.Name) else "")
                _objv = getattr(getattr(_v.func, "value", None), "id", "")
                if (_fnv in ("get", "input", "getenv")
                        and _objv in ("", "args", "os", "sys")):
                    for t in st.targets:
                        if isinstance(t, ast.Name):
                            tainted.add(t.id)
        for st in ast.walk(fn):
            if not isinstance(st, ast.Call):
                continue
            if isinstance(st.func, ast.Name):
                callee = st.func.id
            elif (isinstance(st.func, ast.Attribute)
                    and isinstance(st.func.value, ast.Name)):
                callee = st.func.attr
            else:
                continue
            if callee not in fns:
                continue
            params = fns[callee]
            for idx, arg in enumerate(st.args):
                if not (isinstance(arg, ast.Name)
                        and arg.id in tainted
                        and idx < len(params)):
                    continue
                # 调用方污点 → 被调形参（sink 查预构建索引）
                for s in fn_sinks.get(callee, []):
                    _fn = ""
                    if isinstance(s.func, ast.Attribute):
                        _fn = s.func.attr
                    elif isinstance(s.func, ast.Name):
                        _fn = s.func.id
                    arg_names = {a.id for a in s.args
                                 if isinstance(a, ast.Name)}
                    if params[idx] in arg_names:
                        taint_results.append({
                            "file": str(path),
                            "line": s.lineno, "col": 0,
                            "rule": "taint_flow",
                            "severity": "warning",
                            "msg": (f"污点流（跨函数）："
                                    f"{callee} 形参 {params[idx]} 由调用方"
                                    f"污点变量 {arg.id} 传入"
                                    f" → {_fn}()——输入不可信时注入风险"),
                            "snippet": (lines[s.lineno - 1].strip()[:80]
                                        if s.lineno <= len(lines) else ""),
                        })
                        break

# ══════════════ rust_scan（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""rust_scan.py — Rust 静态 bug 扫描（tree-sitter-rust，P2a 补 Rust 支持）。

抄 semgrep 模式规则思路 + tree-sitter 语法树。检测（基于 tree-sitter-rust 实际节点类型）：
  - unwrap()/expect() 方法调用（panic 风险）——field_expression + call_expression
  - panic!/unreachable!/todo!/unimplemented! 宏调用——macro_invocation
  - unsafe 块——unsafe_block
  - as 裸 cast——type_cast_expression
  - indexing 越界风险提示（静态不可判定时标 info）

用法：
  scan_rust_file(path) -> (issues, line_count)
"""
import re  # IDE 增强 128：_UNSAFE_RE 需要（此前缺失——模块级编译即炸）

try:
    import tree_sitter as ts
    import tree_sitter_rust as tsr
    _PARSER = ts.Parser(ts.Language(tsr.language()))
except Exception:
    _PARSER = None

# 宏名 → (描述, 严重度)
_MACRO_RULES = {
    "panic": ("panic! 显式崩溃点", "error"),
    "unreachable": ("unreachable!() 不可达分支（触发即崩溃）", "error"),
    "todo": ("todo!() 未实现标记（运行即崩溃）", "error"),
    "unimplemented": ("unimplemented!() 未实现（运行即崩溃）", "error"),
}

# 方法调用 → (描述, 严重度)
_METHOD_RULES = {
    "unwrap": ("unwrap() 裸用（None/Err 时 panic——建议 match/ok_or/?)", "warn"),
    "expect": ("expect() 裸用（失败即 panic——建议返回 Result）", "warn"),
}

# IDE 增强 128：unsafe 块/裸指针（安全敏感——unsafe 代码需逐处审查标注）
_UNSAFE_RE = re.compile(r"\bunsafe\s*\{|\bunsafe\s+fn\b|\bunsafe\s+impl\b")

# as 目标类型三级分类（SCAN_QUALITY_ISSUES.md 问题 A 修复，2026-08-13）：
# - NARROW_WARN：真实窄化（任意来源 → 更窄整数）——warn，必报（截断/溢出真风险）
# - PRECISION_INFO：精度损失（f64→f32 类）——info，对齐 clippy cast_precision_loss
#   （allow-by-default 的 pedantic lint，工程常规，不污染 warn）
# - CHECK_INFO：可能窄化（u32/i32 目标，源宽度静态不可知；同宽符号转换 u32↔i32
#   是坐标/索引常见且安全）——info，仅提示确认源宽度
# - 其余目标（u64/i64/usize/isize/f64）——加宽/同宽/浮点，跳过
_NARROW_WARN = ("u8", "i8", "u16", "i16")
_PRECISION_INFO = ("f32",)
_CHECK_INFO = ("u32", "i32")


def _as_severity(target: str) -> tuple[str, str] | None:
    """as 目标类型 → (severity, message)。返回 None 表示安全跳过。"""
    if target in _NARROW_WARN:
        return ("warn",
                f"as {target} 窄化截断（宽整数→{target} 可能溢出/截断——建议 try_from/from）")
    if target in _PRECISION_INFO:
        return ("info",
                f"as {target} 精度损失（f64→f32 类，确认可接受；clippy cast_precision_loss 同级）")
    if target in _CHECK_INFO:
        return ("info",
                f"as {target} 可能窄化（源宽度未知；u32↔i32 同宽符号转换常见——确认源类型）")
    return None


def _node_text(node) -> str:
    try:
        return node.text.decode("utf-8", "ignore")
    except Exception:
        return ""


def _is_test_attr(node) -> bool:
    """判断 attribute_item 是否为 #[test]（含 #[test] / #[tokio::test] 等）。"""
    txt = _node_text(node)
    return "test" in txt and "cfg(test)" not in txt


def _scan_tree(root, path: str, lines: list[str]) -> list[dict]:
    """tree-sitter 语法树扫描（跳过 #[cfg(test)] 测试模块——测试里 unwrap 合理）。

    先收集所有 cfg(test) 模块的字节范围，扫描时跳过（生产代码报告更精确）。
    """
    # 1. 收集测试范围（mod tests 块 + 顶层 #[test] 函数）
    test_ranges: list[tuple[int, int]] = []
    stack = [root]
    while stack:
        n = stack.pop()
        if n.type == "attribute_item" and "cfg(test)" in _node_text(n):
            # cfg(test) 是 mod_item 的兄弟 attribute（tree-sitter-rust 结构：
            # attribute_item 与 mod_item 平级）——取下一个 named 兄弟
            sib = n.next_named_sibling
            while sib is not None and sib.type not in ("mod_item", "function_item"):
                sib = sib.next_named_sibling
            if sib is not None and sib.type in ("mod_item", "function_item"):
                test_ranges.append((sib.start_byte, sib.end_byte))
        elif n.type == "attribute_item" and _is_test_attr(n):
            # #[test] 修饰的是下一个 function_item（集成测试顶层 fn）
            sib = n.next_named_sibling
            while sib is not None and sib.type not in ("function_item", "mod_item"):
                sib = sib.next_named_sibling
            if sib is not None and sib.type in ("function_item", "mod_item"):
                test_ranges.append((sib.start_byte, sib.end_byte))
        stack.extend(n.children)
    # 2. 主扫描（跳过测试范围）
    issues = []
    stack = [root]
    while stack:
        n = stack.pop()
        t = n.type
        if test_ranges and any(a <= n.start_byte and n.end_byte <= b
                               for a, b in test_ranges):
            continue  # 测试模块内：跳过
        line_no = n.start_point[0] + 1
        snippet = lines[line_no - 1][:120] if line_no - 1 < len(lines) else ""
        if t == "macro_invocation":
            # panic!/todo!/... 宏名是第一个子节点（identifier）
            for ch in n.children:
                if ch.type == "identifier":
                    name = _node_text(ch)
                    if name in _MACRO_RULES:
                        desc, sev = _MACRO_RULES[name]
                        issues.append({"file": path, "line": line_no,
                                       "message": desc, "severity": sev,
                                       "rule": name, "col": n.start_point[1] + 1,
                                       "snippet": snippet})
                    break
        elif t == "unsafe_block":
            issues.append({"file": path, "line": line_no,
                           "message": "unsafe 块（需人工审查：裸指针/未定义行为风险）",
                           "severity": "info", "rule": "unsafe",
                           "col": n.start_point[1] + 1, "snippet": snippet})
        elif t == "call_expression" and any(
                k in _node_text(n) for k in
                ("ptr::read", "ptr::write", "ptr::null", "zeroed",
                 "from_utf8_unchecked", "mem::forget", "ManuallyDrop")):
            # 挖漏洞增强（2026-08-15）：Rust 未定义行为族（树解析——
            # 覆盖 tree-sitter 可用路径；文本降级同步有）
            issues.append({"file": path, "line": line_no,
                           "message": "unsafe 内存操作（ptr::read/write/null/zeroed/"
                                      "from_utf8_unchecked/forget/ManuallyDrop——"
                                      "未定义行为/泄漏风险）",
                           "severity": "warning", "rule": "unsafe_mem",
                           "col": n.start_point[1] + 1, "snippet": snippet})
        elif t == "call_expression" and "transmute" in _node_text(n):
            # IDE 增强 128：mem::transmute 高危类型转换（未定义行为风险——
            # 布局假设错误即 UB；建议 from_raw/安全转换）
            issues.append({"file": path, "line": line_no,
                           "message": "transmute() 高危类型转换（布局假设错误即 UB——"
                                      "建议安全转换/from_raw）",
                           "severity": "warning", "rule": "transmute",
                           "col": n.start_point[1] + 1, "snippet": snippet})
        elif t == "type_cast_expression":
            # as 裸 cast：按目标类型三级分类（SCAN_QUALITY_ISSUES.md 问题 A）——
            # 真实窄化 warn、精度损失/可能窄化 info、加宽/同宽/浮点跳过。
            # 不再把 f32/i32/u32/u64 一律标 warn（体素坐标/尺寸/质量转换是工程常规，
            # 旧规则在 VoxelForge 46 文件产出 232 条误报，淹没了真 unwrap/panic）。
            cast_txt = _node_text(n)
            for ch in n.children:
                if ch.type == "as":
                    # 提取目标类型（as 后面的 type_identifier / primitive_type）
                    target = ""
                    nxt = ch.next_named_sibling
                    if nxt is not None:
                        target = _node_text(nxt)
                    sev_msg = _as_severity(target)
                    if sev_msg is not None:
                        sev, msg = sev_msg
                        issues.append({"file": path, "line": line_no,
                                       "message": msg,
                                       "severity": sev, "rule": "as",
                                       "col": n.start_point[1] + 1, "snippet": snippet})
                    break
        elif t == "field_expression":
            # unwrap()/expect()：field_expression 的最后一个 field_identifier
            for ch in reversed(n.children):
                if ch.type == "field_identifier":
                    name = _node_text(ch)
                    if name in _METHOD_RULES:
                        desc, sev = _METHOD_RULES[name]
                        issues.append({"file": path, "line": line_no,
                                       "message": desc, "severity": sev,
                                       "rule": name, "col": n.start_point[1] + 1,
                                       "snippet": snippet})
                    break
        stack.extend(n.children)
    return issues


def scan_rust_file(path: str) -> tuple[list, int]:
    """扫单个 Rust 文件，返回 (issues, line_count)。tree-sitter 不可用时降级文本扫描。"""
    issues = []
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            content = fh.read(1_000_000)
    except OSError as exc:
        return [{"file": path, "line": 0, "message": f"读取失败: {exc}",
                 "severity": "error", "rule": "io", "col": 0}], 0
    lines = content.splitlines()
    total = len(lines)

    if _PARSER is not None:
        try:
            tree = _PARSER.parse(content.encode("utf-8"))
            return _scan_tree(tree.root_node, path, lines), total
        except Exception:
            pass  # 降级文本扫描

    # 文本降级
    for i, line in enumerate(lines, 1):
        for token, (desc, sev) in _MACRO_RULES.items():
            if f"{token}!" in line:
                issues.append({"file": path, "line": i, "message": desc,
                               "severity": sev, "rule": token, "col": 0,
                               "snippet": line[:120]})
        for token, (desc, sev) in _METHOD_RULES.items():
            if f".{token}(" in line:
                issues.append({"file": path, "line": i, "message": desc,
                               "severity": sev, "rule": token, "col": 0,
                               "snippet": line[:120]})
        if "unsafe {" in line or "unsafe{" in line:
            issues.append({"file": path, "line": i,
                           "message": "unsafe 块（需人工审查）",
                           "severity": "info", "rule": "unsafe", "col": 0,
                           "snippet": line[:120]})
        # IDE 增强 129：文本降级同步 transmute（tree-sitter 不可用时保持覆盖；
        # 名字匹配——transmute::<T,U>( 泛型写法也要命中）
        if "transmute" in line:
            issues.append({"file": path, "line": i,
                           "message": "transmute() 高危类型转换（布局假设错误即 UB）",
                           "severity": "warning", "rule": "transmute", "col": 0,
                           "snippet": line[:120]})
        # 挖漏洞增强（2026-08-15）：Rust 未定义行为族（文本降级——
        # 树解析分支已报同 token 的在此跳过防双报；ptr::null 排除
        # null_mut（安全 API 不误伤））
        for token, desc in (
            ("ptr::read", "ptr::read 裸读（悬垂/未初始化即 UB——建议 safe 引用）"),
            ("ptr::write", "ptr::write 裸写（别名/对齐违反即 UB——建议 safe 引用）"),
            ("ptr::null", "ptr::null 裸指针（判空后解引用风险——建议 Option/NonNull）"),
            ("zeroed", "mem::zeroed 零初始化（非零合法类型即 UB——建议 Default）"),
            ("from_utf8_unchecked", "from_utf8_unchecked 跳过校验（非法 UTF-8 即 UB）"),
            ("mem::forget", "mem::forget 泄漏（资源不释放——需显式理由）"),
            ("ManuallyDrop", "ManuallyDrop 手动释放（析构不跑——泄漏风险）"),
        ):
            if token in line and not (token == "ptr::null" and "null_mut" in line):
                issues.append({"file": path, "line": i,
                               "message": desc,
                               "severity": "warning", "rule": "unsafe_mem",
                               "col": 0, "snippet": line[:120]})
        if " as " in line:
            issues.append({"file": path, "line": i,
                           "message": "as 类型转换（文本降级模式，目标类型未知——仅提示）",
                           "severity": "info", "rule": "as", "col": 0,
                           "snippet": line[:120]})
    return issues, total


# ══════════════ sage_scan（合并） ══════════════
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""sage_scan —— 语义回归优先级（阶段3，SAGE 式）。

输入仓库 + 提交范围（默认最近 1 个提交）：
  ① git diff --name-only 提取变更文件（只读）
  ② commit 消息语义分析 → 标签（bugfix/feature/perf/ui/physics/network/
     test/security/refactor——中英文关键词表）
  ③ 测试影响映射：复用 pr_oracle 的 TestMapper（经 server._call_ext 调
     pr_oracle_map_local）→ 候选测试；扩展不可用则降级为变更文件→
     测试文件路径启发式匹配
  ④ 输出"优先测试清单"（与本次更新最相关）——海量内容锁定风险区

全部只读。安全：git 命令只读（log/diff/show），无写操作。
"""


# 语义标签关键词表（中文+英文）
_TAG_KEYWORDS = [
    ("bugfix", ["fix", "bug", "修复", "修", "回归", "崩溃", "panic", "挂"]),
    ("feature", ["feat", "feature", "新增", "功能", "add", "支持"]),
    ("perf", ["perf", "性能", "优化", "卡顿", "慢", "提速", "fast"]),
    ("ui", ["ui", "界面", "菜单", "hud", "布局", "样式", "screen"]),
    ("physics", ["phys", "物理", "碰撞", "重力", "刚体", "collision", "gravity"]),
    ("network", ["net", "网络", "联机", "同步", "sync", "延迟", "弱网"]),
    ("test", ["test", "测试", "spec", "pytest", "覆盖率"]),
    ("security", ["sec", "security", "安全", "漏洞", "注入", "越界", "权限"]),
    ("refactor", ["refactor", "重构", "清理", "rename", "移动", "拆"]),
]


def _git(root: str, args: list[str]) -> str:
    r = subprocess.run(["git", "-C", root, *args], capture_output=True,
                       text=True, timeout=20, encoding="utf-8",
                       errors="replace")
    return r.stdout if r.returncode == 0 else ""


def _semantic_tags(messages: list[str]) -> list[dict]:
    """commit 消息 → 语义标签（含命中关键词证据）。"""
    blob = " ".join(messages).lower()
    out = []
    for tag, kws in _TAG_KEYWORDS:
        hit = [k for k in kws if k.lower() in blob]
        if hit:
            out.append({"tag": tag, "matched": hit[:4]})
    return out


def _test_heuristic(changed: list[str], repo: str) -> list[dict]:
    """降级映射：变更文件 → 同目录/同名 test 文件（关键词匹配）。"""
    out = []
    for cf in changed:
        base = os.path.basename(cf)
        stem = os.path.splitext(base)[0]
        # 同名 test_<stem>.py / <stem>_test.py / test/ 目录下同名
        cands = [
            f"test_{stem}.py", f"{stem}_test.py",
            f"test_{stem}.rs", f"{stem}_test.rs",
        ]
        for c in cands:
            # 在变更文件同目录或 test 目录查找
            d = os.path.dirname(cf)
            for sub in (d, os.path.join("tests", d), os.path.join(d, "tests")):
                p = os.path.join(repo, sub, c) if sub else os.path.join(repo, c)
                if os.path.exists(p):
                    out.append({"test": os.path.relpath(p, repo),
                                "reason": f"变更 {cf} 的对应测试"})
    # 去重
    seen = set()
    dedup = []
    for t in out:
        if t["test"] not in seen:
            seen.add(t["test"])
            dedup.append(t)
    return dedup


def sage_scan(root: str, commits: int = 1, since: str = "") -> dict:
    """语义回归优先级扫描主入口。"""
    if not os.path.isdir(root):
        return {"ok": False, "error": f"路径不存在: {root}"}
    # ① 提交与变更文件
    if since:
        log = _git(root, ["log", "--since", since,
                          "--format=%h|%s", "-50"])
        diff = _git(root, ["diff", f"$(git -C {root} log --since={since} "
                                   f"--format=%H | tail -1)..HEAD",
                           "--name-only"])
    else:
        log = _git(root, ["log", f"-{max(1, commits)}", "--format=%h|%s"])
        diff = _git(root, ["diff", f"HEAD~{max(1, commits)}..HEAD",
                           "--name-only"])
    commits_list = []
    for line in log.strip().splitlines():
        if "|" in line:
            h, s = line.split("|", 1)
            commits_list.append({"hash": h, "message": s[:100]})
    changed = [l.strip() for l in diff.strip().splitlines()
               if l.strip() and not l.startswith("diff ")]
    if not commits_list and not changed:
        return {"ok": False, "error": "无提交或变更（空仓库/无历史）"}

    # ② 语义标签
    tags = _semantic_tags([c["message"] for c in commits_list])

    # ③ 测试影响：优先 pr_oracle TestMapper，降级启发式
    tests: list[dict] = []
    mapper_used = False
    try:
        import server
        r = server._call_ext("pr_oracle_map_local",
                             {"repo_path": root, "changed_files": changed})
        text = r[0].text if r else ""
        data = json.loads(text) if text and not text.startswith("Error") else {}
        mappings = data.get("mappings") or []
        for m in mappings:
            for t in (m.get("candidate_tests") or []):
                reason = m.get("mapping_reason") or f"变更 {m.get('source_file', '')}"
                tests.append({"test": str(t),
                              "reason": str(reason)[:80]})
            if m.get("candidate_tests"):
                mapper_used = True
    except Exception:  # noqa: BLE001 —— 扩展不可用降级
        pass
    if not tests:
        tests = _test_heuristic(changed, root)

    # ④ 优先测试清单（按标签相关度加权排序）
    for t in tests:
        t["priority"] = 1
        if any(tg["tag"] in ("bugfix", "security", "perf") for tg in tags):
            t["priority"] = 0  # 高风险变更 → 最高优先
    tests.sort(key=lambda t: t["priority"])
    test_paths = [t["test"] for t in tests]

    return {
        "ok": True,
        "root": root,
        "commits": commits_list[:10],
        "changed_files": changed[:50],
        "semantic_tags": tags,
        "mapper": "pr_oracle" if mapper_used else "heuristic(降级)",
        "prioritized_tests": tests[:30],
        "test_paths": test_paths[:30],
        "hint": ("prioritized_tests 是与本次更新最相关的测试——SAGE 语义回归："
                 "先跑高风险（bugfix/security/perf）对应的测试，再跑其余"),
    }


if __name__ == "__main__":  # CLI 调试入口
    import sys
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    print(json.dumps(sage_scan(root, n), ensure_ascii=False, indent=1))


# ── 兼容：旧模块名 import 无缝映射到本引擎 ──
import sys as _sys
_sys.modules.setdefault('bug_scan_core', _sys.modules[__name__])
_sys.modules.setdefault('std_core', _sys.modules[__name__])
_sys.modules.setdefault('ui_check_core', _sys.modules[__name__])
_sys.modules.setdefault('cov_scan', _sys.modules[__name__])
_sys.modules.setdefault('cross_taint', _sys.modules[__name__])
_sys.modules.setdefault('rust_scan', _sys.modules[__name__])
_sys.modules.setdefault('sage_scan', _sys.modules[__name__])
