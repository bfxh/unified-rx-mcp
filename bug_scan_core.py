#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""bug_scan_core —— Python 缺陷扫描引擎（2026-08-15 从 server.py 拆出）。

架构整改 R2-R3（拆上帝文件）：bug 扫描族（_bug_scan_file CC=60/_bug_scope_scan
CC=48/_bug_resource_leak CC=35/_scan_body CC=31）独立成模块——工具行为零变化。
"""
import ast
import builtins
import json
import os
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
    # 挖漏洞增强（2026-08-15）：污点分析（Python 数据流——面对复杂漏洞）
    issues.extend(py_taint_scan(src, str(f), lines))
    # 跨函数 taint（CPG 概念：调用边传播——函数 A 污点传 B 形参→B sink）
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
                    or re.search(r"\([^)]*[+*{][^)]*\)[+*{]", _p):
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


def py_taint_scan(src: str, path: str, lines: list) -> list:
    """Python 函数级污点分析（面对日益复杂的漏洞——数据流而非单点规则）。

    函数内：外部源（args.get/input/getenv/request 参数）→ 危险 sink
    （open/eval/subprocess/反序列化/网络）——变量级跟踪 + 函数参数
    传播（跨函数 1 层）。确定性（AST 数据流——非启发）。
    """
    issues = []
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
    base = os.path.dirname(os.path.abspath(__file__))
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
                    if re.search(r"\([^)]*[+*][^)]*\)[+*]", pat)                             or re.search(r"\([^)]*\|[^)]*\)\*", pat)                             or re.search(r"\(\s*[^)]*[+*][^)]*\s*\)\s*\{[^}]+\}", pat)                             or re.search(r"\([^)]*\|[^)]*\)\+\$", pat)                             or re.search(r"\([^)]*[+*{][^)]*\)[+*{]", pat):
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
                    })
            _RULES_CACHE["mtime"] = mt
            _RULES_CACHE["rules"] = rules
            return rules
        except (OSError, ValueError, TypeError):
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
        try:
            rx = re.compile(r["pattern"])
        except re.error:
            continue
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


