#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 bfxh
# SPDX-License-Identifier: MIT
"""unified-rx — 极简架构单文件 MCP（适配 Reasonix）。

设计目标（用户要求：强 / 性能强 / 快 / 内存小 / 架构好，运用到极致）：
  - 单文件：所有工具内联，零 importlib 反射，启动 <100ms
  - 静态注册表：name -> (fn, schema)，无动态发现，调用 O(1)
  - 懒加载：重型能力（httpx/子进程）按需 import，基线内存最小
  - 安全文件层：路径校验 + 大小限制 + 沙盒边界（替代裸 read/write）
  - 错误隔离：单工具异常转结构化文本，绝不拖垮网关

工具分类（前缀分组，全部保留统一入口）：
  - fs_*    文件安全层（read/write/stat/list，路径校验）
  - math_*  数学纯函数（CI-Optimization 内联）
  - str_*   字符串纯函数
  - json_*  JSON 工具
  - sort_*  排序
  - search_* 搜索
  - stat_*   统计
  - geo_*    几何
  - conv_*   温度/单位转换
  - valid_*  数据校验
  - prime_*  素数
  - fib_*    斐波那契/阶乘
  - bug_*    代码缺陷扫描（未定义变量/None 解引用/资源泄漏/除零/越界）+ 报错精准定位
  - misc_*   杂项

运行:  python server.py            (stdio)
自检:  python server.py --selftest
"""
import argparse
import ast
import asyncio
import builtins
import json
import math
import os
import re
import sys
import time
from pathlib import Path

# mcp 库懒加载（性能优化）：纯工具/自检路径零 mcp 依赖，


class _TC:
    """轻量文本内容（协议层转换为 types.TextContent）。

    role/summary 供 Tool 角色回喂（Aether AiRole::Tool 启发）：UI 可将
    工具结果渲染为简洁工具卡片，不冒充用户气泡。默认 None 保持既有行为。
    """
    __slots__ = ("type", "text", "role", "summary")

    def __init__(self, text: str, type_: str = "text", role: str | None = None, summary: str | None = None):
        self.type = type_
        self.text = text
        self.role = role
        self.summary = summary


class _TR:
    """结构化工具结果（Tool 角色回喂卡片视图）。

    AetherStudio PR #106 启发：工具结果以 Tool 角色记录、UI 渲染为简洁卡片
    （不显示为用户气泡、无角色标签）。text 为 JSON（role/ok/summary/detail），
    协议层原样透传，RX/Aether UI 可解析为卡片；纯文本工具不受影响。
    """
    __slots__ = ("role", "ok", "summary", "detail")

    def __init__(self, ok: bool, summary: str, detail: object | None = None):
        self.role = "tool"
        self.ok = bool(ok)
        self.summary = summary
        self.detail = detail

    def to_text(self) -> str:
        payload: dict = {"role": self.role, "ok": self.ok, "summary": self.summary}
        if self.detail is not None:
            payload["detail"] = self.detail
        return json.dumps(payload, ensure_ascii=False, default=str)


def _tr(ok: bool, summary: str, detail: object | None = None) -> _TC:
    """构造 Tool 角色卡片结果（_TC 包装，role=tool）。"""
    return _TC(_TR(ok, summary, detail).to_text(), role="tool", summary=summary)


class _ToolDef:
    """轻量工具定义（协议层转换为 types.Tool）。"""
    __slots__ = ("name", "description", "inputSchema")

    def __init__(self, name: str, description: str, inputSchema: dict):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema
# 仅在 run() 启动协议层时才 import（import 2.5s→<10ms，基线内存 33MB→<5MB）

# ─────────────────────────────────────────────────────────────
# 安全文件层（替代裸 open；路径校验 + 大小限制 + 沙盒边界）
# ─────────────────────────────────────────────────────────────
_MAX_READ = 1 << 20          # 单文件读取上限 1MB（防 OOM）
_MAX_WRITE = 1 << 20         # 单文件写入上限 1MB
# 默认沙盒：锚定到进程启动时的工作目录（security 审查修复——默认不允许任意路径读写）
# 可用环境变量 UNIFIED_RX_SANDBOX 覆盖（分号分隔多个根）；空字符串 = 显式禁用沙盒
_SANDBOX_ROOTS = [
    r.strip() for r in os.environ.get("UNIFIED_RX_SANDBOX", os.getcwd()).split(";") if r.strip()
]


def _check_path(path: str) -> Path:
    """路径安全校验：拒绝空/绝对路径逃逸/NUL；沙盒外拒绝。"""
    if not path or "\x00" in path:
        raise ValueError("路径不能为空或含 NUL")
    p = Path(path)
    if not _SANDBOX_ROOTS:
        return p  # 显式禁用沙盒（UNIFIED_RX_SANDBOX=""）时允许任意路径
    abs_p = p.resolve()
    for root in _SANDBOX_ROOTS:
        try:
            if abs_p.is_relative_to(Path(root).resolve()):
                return abs_p  # 返回 resolve 后路径（防符号链接替换 TOCTOU）
        except OSError:
            continue
    raise ValueError(f"路径越界（沙盒外）: {path}")


def _tool_fs_read(args: dict) -> "list[types.TextContent]":
    p = _check_path(str(args["path"]))
    if not p.is_file():
        raise ValueError(f"文件不存在: {p}")
    size = p.stat().st_size
    if size > _MAX_READ:
        raise ValueError(f"文件过大（{size} > {_MAX_READ}），拒绝读取")
    text = p.read_text(encoding="utf-8", errors="replace")
    # 读后复核大小（防 TOCTOU：读取期间文件被替换增长，review nit 修复）
    if p.stat().st_size > _MAX_READ:
        raise ValueError(f"文件读取后超限（>{_MAX_READ}），拒绝返回")
    return [_TC(text)]


def _tool_fs_write(args: dict) -> "list[types.TextContent]":
    p = _check_path(str(args["path"]))
    content = str(args["content"])
    if len(content.encode("utf-8")) > _MAX_WRITE:
        raise ValueError(f"内容过大（>{_MAX_WRITE}），拒绝写入")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return [_TC(f"已写入 {p} ({len(content)} 字符)")]


def _tool_fs_stat(args: dict) -> "list[types.TextContent]":
    p = _check_path(str(args["path"]))
    if not p.exists():
        return [_TC(json.dumps({"exists": False}))]
    st = p.stat()
    return [_TC(json.dumps({
        "exists": True, "is_file": p.is_file(), "is_dir": p.is_dir(),
        "size": st.st_size, "mtime": st.st_mtime,
    }))]


def _tool_fs_list(args: dict) -> "list[types.TextContent]":
    p = _check_path(str(args["path"]))
    if not p.is_dir():
        raise ValueError(f"目录不存在: {p}")
    depth = int(args.get("depth", 1))
    entries = []
    for child in sorted(p.iterdir()):
        if depth > 1 and child.is_dir():
            try:
                sub = [c.name for c in sorted(child.iterdir())][:50]
            except OSError:
                sub = []  # 无权限子目录跳过，不废掉整个列目录（review nit 修复）
            entries.append({"name": child.name, "type": "dir", "children": sub})
        else:
            entries.append({"name": child.name, "type": "file" if child.is_file() else "dir"})
        if len(entries) >= 200:
            break
    return [_TC(json.dumps({"entries": entries}, ensure_ascii=False))]


# ─────────────────────────────────────────────────────────────
# 纯函数层（CI-Optimization 内联，零依赖）
# ─────────────────────────────────────────────────────────────
def _math_add(args): return str(args["a"] + args["b"])
def _math_sub(args): return str(args["a"] - args["b"])
def _math_mul(args): return str(args["a"] * args["b"])
def _math_div(args):
    if args["b"] == 0:
        raise ValueError("除数不能为 0")
    return str(args["a"] / args["b"])


def _math_power(args):
    base = args["base"]
    exp = args["exponent"]
    if abs(exp) > 1000:
        raise ValueError("指数绝对值过大（>1000），拒绝计算")
    if abs(base) > 1e9:
        raise ValueError("底数过大（>1e9），拒绝计算")
    return str(base ** exp)
def _math_sqrt(args):
    if args["x"] < 0:
        raise ValueError("负数无实数平方根")
    return str(math.sqrt(args["x"]))


def _math_abs(args): return str(abs(args["x"]))
def _math_factorial(args):
    n = int(args["n"])
    if n < 0:
        raise ValueError("阶乘要求非负整数")
    if n > 1000:
        raise ValueError("n 过大（>1000，超过 Python int→str 位限）")
    return str(math.factorial(n))


def _fib_fibonacci(args):
    n = int(args["n"])
    if n < 0:
        raise ValueError("n 不能为负")
    if n > 20000:
        raise ValueError("n 过大（>20000，超过 Python int→str 位限）")
    a, b = 0, 1
    for _ in range(n):
        a, b = b, a + b
    return str(a)


def _str_reverse(args): return args["s"][::-1]
def _str_upper(args): return args["s"].upper()
def _str_lower(args): return args["s"].lower()
def _str_palindrome(args):
    s = args["s"]
    return str(s == s[::-1])


def _sort_quick(args):
    arr = list(args["arr"])
    if len(arr) > 100000:
        raise ValueError("数组过大（>100000）")
    arr.sort()
    return json.dumps(arr)


def _sort_bubble(args):
    arr = list(args["arr"])
    if len(arr) > 2000:
        raise ValueError("数组过大（>2000，冒泡 O(n²) 防 DoS）")
    n = len(arr)
    for i in range(n):
        for j in range(n - i - 1):
            if arr[j] > arr[j + 1]:
                arr[j], arr[j + 1] = arr[j + 1], arr[j]
    return json.dumps(arr)


def _search_binary(args):
    arr = list(args["arr"])
    if len(arr) > 100000:
        raise ValueError("数组过大（>100000）")
    target = args["target"]
    lo, hi = 0, len(arr) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if arr[mid] == target:
            return str(mid)
        if arr[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return str(-1)


def _stat_mean(args):
    data = list(args["data"])
    if not data:
        raise ValueError("数据为空")
    if len(data) > 100000:
        raise ValueError("数据过大（>100000）")
    return str(sum(data) / len(data))


def _stat_median(args):
    data = list(args["data"])
    if not data:
        raise ValueError("数据为空")
    if len(data) > 100000:
        raise ValueError("数据过大（>100000）")
    data = sorted(data)
    n = len(data)
    if n % 2:
        return str(data[n // 2])
    return str((data[n // 2 - 1] + data[n // 2]) / 2)


def _geo_circle(args):
    r = args["radius"]
    if r < 0:
        raise ValueError("半径不能为负")
    return str(math.pi * r * r)


def _geo_rect(args):
    l, w = args["length"], args["width"]
    if l < 0 or w < 0:
        raise ValueError("边长不能为负")
    return str(2 * (l + w))


def _conv_c2f(args): return str(args["celsius"] * 9 / 5 + 32)
def _conv_f2c(args): return str((args["fahrenheit"] - 32) * 5 / 9)


def _json_parse(args):
    try:
        return json.dumps(json.loads(args["json_string"]), ensure_ascii=False)
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON 解析失败: {e}")


def _json_valid(args):
    try:
        json.loads(args["json_string"])
        return "true"
    except json.JSONDecodeError:
        return "false"


def _valid_email(args):
    return str(bool(re.match(r"^[\w.+-]+@[\w-]+\.[\w.]+$", args["email"])))


def _prime_is_prime(args):
    n = int(args["n"])
    if n < 2:
        return "false"
    if n > 10_000_000:
        raise ValueError("n 过大（>10M）")
    for i in range(2, int(math.isqrt(n)) + 1):
        if n % i == 0:
            return "false"
    return "true"


def _prime_generate(args):
    limit = int(args["limit"])
    if limit > 1_000_000:
        raise ValueError("limit 过大（>1M）")
    sieve = bytearray(b"\x01") * (limit + 1)
    sieve[0:2] = b"\x00\x00"
    for i in range(2, int(limit ** 0.5) + 1):
        if sieve[i]:
            sieve[i * i::i] = b"\x00" * len(sieve[i * i::i])
    return json.dumps([i for i in range(2, limit + 1) if sieve[i]])


def _list_unique(args):
    seen, out = set(), []
    for x in args["lst"]:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return json.dumps(out)


def _list_flatten(args):
    out = []

    def _flat(x):
        if isinstance(x, list):
            for i in x:
                _flat(i)
        else:
            out.append(x)

    _flat(args["nested_list"])
    return json.dumps(out)


# ─────────────────────────────────────────────────────────────
# 代码缺陷扫描层（bug_*：纯 ast/re 零依赖静态分析 + 报错精准定位）
# ─────────────────────────────────────────────────────────────
_BUG_MAX_FILES = 100            # 单次扫描文件数上限（防 DoS）
_BUG_MAX_TOTAL_LINES = 200_000  # 扫描总行数上限
_BUG_CONTEXT = 3                # 定位上下文行数（前后各 N 行）

# 未定义变量检测白名单（builtins + 常见隐式名，降低误报）
_BUG_BUILTINS = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__",
    "__annotations__", "__class__", "__dict__", "__main__", "self", "cls",
    "True", "False", "None", "NotImplemented", "Ellipsis",
}


# 报错定位正则：traceback 风格 `File "x.py", line 42, in foo` + 简洁风格 `x.py:42`
_TRACEBACK_RE = re.compile(r'File "([^"]+)", line (\d+)(?:, in ([^\n]+))?')
_SIMPLE_POS_RE = re.compile(r'((?:[A-Za-z]:[\\/])?[^\s:"]+\.py):(\d+)(?::(\d+))?')


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


def _bug_check_deref(node, none_vars, path, lines, issues):
    """None 变量被解引用（属性/下标/调用）检测；线性近似，可能漏报/误报。"""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.Attribute, ast.Subscript)) and isinstance(child.value, ast.Name) \
                and child.value.id in none_vars:
            issues.append(_bug_issue(path, child, "none_deref", "warning",
                                     f"'{child.value.id}' 可能为 None，此处解引用会抛异常", lines))
        elif isinstance(child, ast.Call) and isinstance(child.func, ast.Name) \
                and child.func.id in none_vars:
            issues.append(_bug_issue(path, child, "none_deref", "warning",
                                     f"'{child.func.id}' 可能为 None，调用会抛 TypeError", lines))
        else:
            _bug_check_deref(child, none_vars, path, lines, issues)


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
            val_alias = isinstance(stmt.value, ast.Name) and stmt.value.id in none_vars
            seq_len = _bug_seq_len(stmt.value)
            for t in stmt.targets:
                if isinstance(t, ast.Name):
                    if val_none or val_alias:
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
        # 越界：字面量容器 + 字面量索引（确定性）
        seq_len = _bug_seq_len(n.value) if isinstance(n, ast.Subscript) else None
        if seq_len is not None and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, int):
            idx = n.slice.value
            if idx >= seq_len or idx < -seq_len:
                issues.append(_bug_issue(str(f), n, "index_out_of_range", "error",
                                         f"索引 {idx} 越界（容器长度 {seq_len}）", lines))
    return issues, len(lines)


def _tool_bug_scan(args: dict) -> "list[types.TextContent]":
    """静态扫描 bug 模式：未定义变量/None 解引用/资源泄漏/除零/越界。

    多文件目录扫描用 ProcessPoolExecutor 并行（CPU 密集 AST，子进程不吃 GIL）；
    进程池不可用（受限环境）时串行 fallback。结果与串行完全一致。
    """
    p = _check_path(str(args["path"]))
    max_files = int(args.get("max_files", _BUG_MAX_FILES))
    if not 1 <= max_files <= 500:
        raise ValueError("max_files 须在 1..500")
    files = []
    if p.is_file():
        if p.suffix == ".py":
            files = [p]
        else:
            raise ValueError(f"仅支持 Python 文件: {p}")
    elif p.is_dir():
        for root, _, names in os.walk(p):
            for name in sorted(names):
                if name.endswith(".py"):
                    files.append(Path(root) / name)
                    if len(files) >= max_files:
                        break
            if len(files) >= max_files:
                break
    else:
        raise ValueError(f"路径不存在: {p}")

    issues: list = []
    total_lines = 0
    # 单文件直接扫；多文件并行（≥16 文件才值得起进程池——spawn 开销 vs AST 并行收益）
    if len(files) >= 16:
        try:
            import concurrent.futures as _cf
            with _cf.ProcessPoolExecutor(max_workers=min(4, os.cpu_count() or 2)) as ex:
                for fi, ln in ex.map(_bug_scan_file, [str(f) for f in files]):
                    issues.extend(fi)
                    total_lines += ln
        except Exception:
            # 进程池不可用（受限环境）→ 串行 fallback
            issues, total_lines = [], 0
            for f in files:
                fi, ln = _bug_scan_file(str(f))
                issues.extend(fi)
                total_lines += ln
    else:
        for f in files:
            fi, ln = _bug_scan_file(str(f))
            issues.extend(fi)
            total_lines += ln

    if total_lines > _BUG_MAX_TOTAL_LINES:
        raise ValueError(f"扫描总量超限（>{_BUG_MAX_TOTAL_LINES} 行），请缩小范围")
    issues.sort(key=lambda i: (i["file"], i["line"], i["col"]))
    return [_TC(json.dumps({
        "ok": True, "files": len(files), "issue_count": len(issues), "issues": issues,
    }, ensure_ascii=False))]


def _tool_ui_check(args: dict) -> "list[types.TextContent]":
    """Bevy UI 静态检查（程序驱动，非 skill）：扫描 Rust UI 代码的崩溃/不可见模式。
    规则：ui_root_missing/camera_missing/mode_isolation/focus_pass/font_missing/z_ordering。
    目录扫描走沙盒校验 + 上限（max_files 1..500/单文件 1MB）。"""
    p = _check_path(str(args["path"]))
    max_files = int(args.get("max_files", 100))
    if not 1 <= max_files <= 500:
        raise ValueError("max_files 须在 1..500")
    try:
        from ui_check_core import scan_ui_dir, scan_ui_source
    except ImportError:
        # 回退：直接按文件扫描（ui_check_core 与 server.py 同目录）
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        from ui_check_core import scan_ui_dir, scan_ui_source  # noqa: F811
    if p.is_dir():
        issues = scan_ui_dir(str(p), max_files=max_files)
    elif p.is_file() and p.suffix == ".rs":
        if p.stat().st_size > _MAX_READ:
            raise ValueError(f"文件过大（>{_MAX_READ}）")
        src = p.read_text(encoding="utf-8", errors="replace")
        issues = scan_ui_source(src, str(p))
        for i in issues:
            i["file"] = str(p)
    else:
        raise ValueError(f"仅支持 .rs 文件或目录: {p}")
    return [_TC(json.dumps({
        "ok": True, "issue_count": len(issues), "issues": issues,
    }, ensure_ascii=False))]


def _tool_ds_lookup(args: dict) -> "list[types.TextContent]":
    """设计系统 token 查询（AI 引流）：返回全部 tokens（组/名/值），AI 生成 UI 时引用。"""
    try:
        from ds_core import lookup_tokens
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        from ds_core import lookup_tokens  # noqa: F811
    result = lookup_tokens()
    return [_TC(json.dumps(result, ensure_ascii=False))]


def _tool_ds_check(args: dict) -> "list[types.TextContent]":
    """设计系统合规检查：扫描 Rust UI 代码的硬编码值/规则偏离（AI 写完 UI 后验证）。"""
    p = _check_path(str(args["path"]))
    max_files = int(args.get("max_files", 200))
    if not 1 <= max_files <= 500:
        raise ValueError("max_files 须在 1..500")
    try:
        from ds_core import check_directory, check_ui_code
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        from ds_core import check_directory, check_ui_code  # noqa: F811
    if p.is_dir():
        result = check_directory(str(p), max_files=max_files)
    elif p.is_file() and p.suffix == ".rs":
        if p.stat().st_size > _MAX_READ:
            raise ValueError(f"文件过大（>{_MAX_READ}）")
        src = p.read_text(encoding="utf-8", errors="replace")
        issues = check_ui_code(src, str(p))
        result = {"ok": True, "file_count": 1, "issue_count": len(issues), "issues": issues}
    else:
        raise ValueError(f"仅支持 .rs 文件或目录: {p}")
    return [_TC(json.dumps(result, ensure_ascii=False))]


def _tool_std_check(args: dict) -> "list[types.TextContent]":
    """通用工程标准检查（软件/游戏/前端/UI 通用，AetherStudio 启发）。

    检查：text_placeholder（占位/假数据/套话）、name_conflict（重复定义）、
    ui_hardcode（UI 硬编码颜色/尺寸）、magic_number（裸魔法数字）。
    标准契约：默认按此标准执行；项目有特殊条件时，调用方在提示词中
    提前告知（本工具不臆测），否则按默认标准兼容绝大多数项目。
    """
    p = _check_path(str(args["path"]))
    max_files = int(args.get("max_files", 200))
    if not 1 <= max_files <= 500:
        raise ValueError("max_files 须在 1..500")
    try:
        from std_core import scan_directory, scan_file
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        from std_core import scan_directory, scan_file  # noqa: F811
    if p.is_dir():
        result = scan_directory(str(p), max_files=max_files)
    elif p.is_file():
        if p.stat().st_size > _MAX_READ:
            raise ValueError(f"文件过大（>{_MAX_READ}）")
        result = scan_file(str(p))
    else:
        raise ValueError(f"仅支持文件或目录: {p}")
    return [_TC(json.dumps(result, ensure_ascii=False))]


def _tool_cb_index(args: dict) -> "list[types.TextContent]":
    """代码库索引（认知层）：全库扫描构建/更新持久化索引（文件树+符号+哈希），
    返回变更感知（changed/added/removed）——工具知道代码库全貌和你改了哪。"""
    p = _check_path(str(args["path"]))
    try:
        from cb_index_core import index_repo
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        from cb_index_core import index_repo  # noqa: F811
    result = index_repo(str(p))
    # 剥离 files（符号表巨大，输出只需统计与变更；files 仅供 scan_repo 内部使用）
    result.pop("files", None)
    return [_TC(json.dumps(result, ensure_ascii=False))]


def _tool_cb_status(args: dict) -> "list[types.TextContent]":
    """代码库状态（认知层）：读取索引摘要（文件树+符号+上次变更），不重建。"""
    p = _check_path(str(args["path"]))
    try:
        from cb_index_core import repo_status
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        from cb_index_core import repo_status  # noqa: F811
    result = repo_status(str(p))
    return [_TC(json.dumps(result, ensure_ascii=False))]


def _tool_cb_scan(args: dict) -> "list[types.TextContent]":
    """全库扫描（认知层）：增量索引 + UI 规则全库扫描，变更优先排序。"""
    p = _check_path(str(args["path"]))
    max_files = int(args.get("max_files", 200))
    if not 1 <= max_files <= 500:
        raise ValueError("max_files 须在 1..500")
    try:
        from cb_index_core import scan_repo
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        from cb_index_core import scan_repo  # noqa: F811
    result = scan_repo(str(p), max_files=max_files)
    return [_TC(json.dumps(result, ensure_ascii=False))]


def _tool_locate_edit(args: dict) -> "list[types.TextContent]":
    """Qoder 式代码定位：自然语言/符号 → 具体修改位置（AI 引导）。

    输入 query（符号名/关键词/报错片段）+ path（仓库根），返回按相关度排序的
    候选位置 [{file, line, symbol, snippet, score, reason}]，告诉 AI 改哪里。
    引导 hint：改前用 cae_code_context 取上下文，改后跑 cae_change_impact。
    """
    p = _check_path(str(args["path"]))
    query = str(args.get("query", "")).strip()
    max_files = int(args.get("max_files", 200))
    limit = int(args.get("limit", 10))
    if not query:
        raise ValueError("query 必填")
    if not 1 <= max_files <= 500:
        raise ValueError("max_files 须在 1..500")
    if not 1 <= limit <= 30:
        raise ValueError("limit 须在 1..30")
    try:
        from locate_core import locate
    except ImportError:
        _dir = os.path.dirname(os.path.abspath(__file__))
        sys.path.insert(0, _dir)
        from locate_core import locate  # noqa: F811
    result = locate(str(p), query, max_files=max_files, limit=limit)
    return [_TC(json.dumps(result, ensure_ascii=False))]


def _tool_bug_locate(args: dict) -> "list[types.TextContent]":
    """报错文本 → file:line 精准定位（含上下文片段，走沙盒校验）。"""
    text = str(args["error_text"])
    matches = []
    for m in _TRACEBACK_RE.finditer(text):
        matches.append((m.group(1), int(m.group(2)), 0, (m.group(3) or "").strip()))
    if not matches:
        for m in _SIMPLE_POS_RE.finditer(text):
            matches.append((m.group(1), int(m.group(2)), int(m.group(3) or 0), ""))
    locations = []
    for raw, line, col, func in matches:
        try:
            p = _check_path(raw)
        except ValueError as exc:
            locations.append({"file": raw, "line": line, "col": col, "func": func,
                              "status": "blocked", "reason": str(exc), "context": []})
            continue
        if not p.is_file():
            locations.append({"file": str(p), "line": line, "col": col, "func": func,
                              "status": "missing", "reason": "文件不存在", "context": []})
            continue
        # 读前大小检查（与 fs_read 一致，防超大文件整读入内存 DoS，review should-fix）
        try:
            fsize = p.stat().st_size
        except OSError as exc:
            locations.append({"file": str(p), "line": line, "col": col, "func": func,
                              "status": "unreadable", "reason": str(exc), "context": []})
            continue
        if fsize > _MAX_READ:
            locations.append({"file": str(p), "line": line, "col": col, "func": func,
                              "status": "too_large", "reason": f"文件过大（{fsize} > {_MAX_READ}）", "context": []})
            continue
        try:
            src_lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            # 读后复核（TOCTOU：读取期间文件被替换增长，与 fs_read/bug_scan 一致，security LOW 修复）
            if p.stat().st_size > _MAX_READ:
                locations.append({"file": str(p), "line": line, "col": col, "func": func,
                                  "status": "too_large", "reason": "文件读取后超限（>1MB）", "context": []})
                continue
        except OSError as exc:
            locations.append({"file": str(p), "line": line, "col": col, "func": func,
                              "status": "unreadable", "reason": str(exc), "context": []})
            continue
        start, end = max(1, line - _BUG_CONTEXT), min(len(src_lines), line + _BUG_CONTEXT)
        locations.append({"file": str(p.resolve()), "line": line, "col": col, "func": func,
                          "status": "ok",
                          "context": [f"{i}: {src_lines[i - 1]}" for i in range(start, end + 1)]})
    return [_TC(json.dumps({
        "ok": True, "matched": bool(locations), "locations": locations,
    }, ensure_ascii=False))]


# ─────────────────────────────────────────────────────────────
# 静态注册表（O(1) 分发，零反射）
# ─────────────────────────────────────────────────────────────
def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


_S = lambda t, d: {"type": t, "description": d}  # noqa: E731


def _tool_card(args: dict) -> "list[types.TextContent]":
    """调用任意工具并返回 Tool 角色卡片视图（Aether AiRole::Tool 启发）。

    与直接调用不同：结果被封装为 {role:"tool", ok, summary, detail} JSON，
    RX/Aether UI 据此渲染为简洁工具卡片（不冒充用户消息、无角色标签）。
    summary 优先取原结果的 summary 字段；detail 保留完整结果。
    可选 max_detail_len（字符上限，默认 20000）：detail 超限时截断为前 N 条
    并附 truncated 标记——防止大结果（如全库 bug_scan）撑爆上下文（省 token）。
    _call 在模块加载完成后才被调用（运行时解析），此处定义顺序无碍。
    """
    name = str(args.get("name", ""))
    sub = args.get("arguments") or {}
    max_detail = int(args.get("max_detail_len", 20000))
    if not 1 <= max_detail <= 500000:
        raise ValueError("max_detail_len 须在 1..500000")
    if not name:
        return [_tr(False, "tool_card: 缺少工具名", {"error": "name 必填"})]
    try:
        out = _call(name, sub)
    except Exception as exc:
        return [_tr(False, f"tool_card: 调用 {name} 失败", {"error": str(exc)})]
    text = "\n".join(c.text for c in out)
    if text.startswith("Error") or text.startswith("Error in"):
        return [_tr(False, text, None)]
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "role" in parsed:
            # 已是结构化结果：透传卡片字段（同样受 max_detail_len 约束）
            detail = parsed.get("detail", parsed)
            return [_tr(parsed.get("ok", True), parsed.get("summary", name), _truncate_detail(detail, max_detail))]
        summary = f"{name}: {text[:200]}{'…' if len(text) > 200 else ''}"
        return [_tr(True, summary, _truncate_detail(parsed, max_detail))]
    except (ValueError, TypeError):
        summary = f"{name}: {text[:200]}{'…' if len(text) > 200 else ''}"
        return [_tr(True, summary, text[:max_detail] + ("…(truncated)" if len(text) > max_detail else ""))]


def _truncate_detail(detail, max_len: int):
    """detail 超限截断：dict/list 按条目截（保留结构），str 按字符截；附 truncated 标记。"""
    if isinstance(detail, str):
        if len(detail) <= max_len:
            return detail
        return detail[:max_len] + "…(truncated)"
    if isinstance(detail, list):
        if len(detail) <= 20:
            # security MEDIUM：即使条数少，条目总字符超限也截断
            total_chars = sum(len(json.dumps(x, ensure_ascii=False, default=str)) for x in detail)
            if total_chars <= max_len:
                return detail
            kept, used = [], 0
            for x in detail:
                xs = json.dumps(x, ensure_ascii=False, default=str)
                if used + len(xs) > max_len:
                    break
                kept.append(x)
                used += len(xs)
            return {"truncated": True, "total": len(detail), "shown": len(kept), "items": kept}
        return {"truncated": True, "total": len(detail), "shown": 20, "items": detail[:20]}
    if isinstance(detail, dict):
        try:
            s = json.dumps(detail, ensure_ascii=False)
        except (TypeError, ValueError):
            return detail
        if len(s) <= max_len:
            return detail
        # 大 dict：保留 summary 级字段 + 截断大数组/大字符串
        out: dict = {}
        for k, v in detail.items():
            vs = json.dumps(v, ensure_ascii=False, default=str) if not isinstance(v, str) else v
            if len(vs) <= max_len // 4:
                out[k] = v
            elif isinstance(v, list) and len(v) > 10:
                out[k] = {"truncated": True, "total": len(v), "shown": 10, "items": v[:10]}
            elif isinstance(v, str):
                out[k] = v[:max_len // 4] + "…(truncated)"
            else:
                out[k] = "…(truncated)"
            if len(json.dumps(out, ensure_ascii=False, default=str)) > max_len:
                break
        out.setdefault("truncated", True)
        return out
    return detail


_TOOLS: dict[str, tuple] = {
    # 文件层
    "fs_read": (_tool_fs_read, _schema({"path": _S("string", "文件路径")}, ["path"]), "安全读取文件（≤1MB，路径校验）"),
    "fs_write": (_tool_fs_write, _schema({"path": _S("string", "文件路径"), "content": _S("string", "内容")}, ["path", "content"]), "安全写入文件（≤1MB）"),
    "fs_stat": (_tool_fs_stat, _schema({"path": _S("string", "路径")}, ["path"]), "文件元信息（存在/大小/mtime）"),
    "fs_list": (_tool_fs_list, _schema({"path": _S("string", "目录"), "depth": _S("integer", "深度(默认1)")}, ["path"]), "列目录（≤200 项）"),
    # 数学
    "math_add": (_math_add, _schema({"a": _S("number", "加数"), "b": _S("number", "加数")}, ["a", "b"]), "加法"),
    "math_sub": (_math_sub, _schema({"a": _S("number", ""), "b": _S("number", "")}, ["a", "b"]), "减法"),
    "math_mul": (_math_mul, _schema({"a": _S("number", ""), "b": _S("number", "")}, ["a", "b"]), "乘法"),
    "math_div": (_math_div, _schema({"a": _S("number", ""), "b": _S("number", "")}, ["a", "b"]), "除法（b≠0）"),
    "math_power": (_math_power, _schema({"base": _S("number", ""), "exponent": _S("number", "")}, ["base", "exponent"]), "幂"),
    "math_sqrt": (_math_sqrt, _schema({"x": _S("number", "")}, ["x"]), "平方根"),
    "math_abs": (_math_abs, _schema({"x": _S("number", "")}, ["x"]), "绝对值"),
    "math_factorial": (_math_factorial, _schema({"n": _S("integer", "≤1000")}, ["n"]), "阶乘"),
    # 斐波那契
    "fib_fibonacci": (_fib_fibonacci, _schema({"n": _S("integer", "≤20000")}, ["n"]), "斐波那契第 n 项"),
    # 字符串
    "str_reverse": (_str_reverse, _schema({"s": _S("string", "")}, ["s"]), "反转字符串"),
    "str_upper": (_str_upper, _schema({"s": _S("string", "")}, ["s"]), "转大写"),
    "str_lower": (_str_lower, _schema({"s": _S("string", "")}, ["s"]), "转小写"),
    "str_palindrome": (_str_palindrome, _schema({"s": _S("string", "")}, ["s"]), "是否回文"),
    # 排序
    "sort_quick": (_sort_quick, _schema({"arr": _S("array", "≤100000")}, ["arr"]), "快速排序"),
    "sort_bubble": (_sort_bubble, _schema({"arr": _S("array", "≤2000")}, ["arr"]), "冒泡排序"),
    # 搜索
    "search_binary": (_search_binary, _schema({"arr": _S("array", "已排序"), "target": _S("number", "")}, ["arr", "target"]), "二分查找"),
    # 统计
    "stat_mean": (_stat_mean, _schema({"data": _S("array", "数字")}, ["data"]), "均值"),
    "stat_median": (_stat_median, _schema({"data": _S("array", "数字")}, ["data"]), "中位数"),
    # 几何
    "geo_circle_area": (_geo_circle, _schema({"radius": _S("number", "")}, ["radius"]), "圆面积"),
    "geo_rect_perimeter": (_geo_rect, _schema({"length": _S("number", ""), "width": _S("number", "")}, ["length", "width"]), "矩形周长"),
    # 转换
    "conv_c2f": (_conv_c2f, _schema({"celsius": _S("number", "")}, ["celsius"]), "摄氏→华氏"),
    "conv_f2c": (_conv_f2c, _schema({"fahrenheit": _S("number", "")}, ["fahrenheit"]), "华氏→摄氏"),
    # JSON
    "json_parse": (_json_parse, _schema({"json_string": _S("string", "")}, ["json_string"]), "解析 JSON"),
    "json_valid": (_json_valid, _schema({"json_string": _S("string", "")}, ["json_string"]), "校验 JSON"),
    # 校验
    "valid_email": (_valid_email, _schema({"email": _S("string", "")}, ["email"]), "邮箱格式校验"),
    # 素数
    "prime_is_prime": (_prime_is_prime, _schema({"n": _S("integer", "≤10M")}, ["n"]), "素数判断"),
    "prime_generate": (_prime_generate, _schema({"limit": _S("integer", "≤1M")}, ["limit"]), "生成素数"),
    # 列表
    "list_unique": (_list_unique, _schema({"lst": _S("array", "")}, ["lst"]), "去重"),
    "list_flatten": (_list_flatten, _schema({"nested_list": _S("array", "")}, ["nested_list"]), "展平嵌套列表"),
    # 代码缺陷扫描 + 精准定位
    "tool_card": (_tool_card, _schema({
        "name": _S("string", "要调用的工具名"),
        "arguments": _S("object", "工具参数（可选）"),
        "max_detail_len": _S("integer", "detail 字符上限(默认20000，防大结果撑爆上下文)"),
    }, ["name"]), "Tool 角色回喂：调用任意工具并返回结构化卡片 {role,ok,summary,detail}（Aether AiRole::Tool 启发）"),
    "bug_scan": (_tool_bug_scan, _schema({"path": _S("string", "Python 文件或目录"), "max_files": _S("integer", "最大文件数(默认100)")}, ["path"]), "静态扫描 bug 模式（未定义变量/None 解引用/资源泄漏/除零/越界）"),
    "bug_locate": (_tool_bug_locate, _schema({"error_text": _S("string", "报错/traceback 文本")}, ["error_text"]), "报错文本 → 定位 file:line（含上下文片段）"),
    "ui_check": (_tool_ui_check, _schema({"path": _S("string", ".rs 文件或目录"), "max_files": _S("integer", "扫描文件上限(默认100)")}, ["path"]), "Bevy UI 静态检查（崩溃/不可见模式）"),
    "cb_index": (_tool_cb_index, _schema({"path": _S("string", "代码库根目录")}, ["path"]), "代码库索引（全库符号+哈希+变更感知）"),
    "cb_status": (_tool_cb_status, _schema({"path": _S("string", "代码库根目录")}, ["path"]), "代码库状态（索引摘要，不重建）"),
    "cb_scan": (_tool_cb_scan, _schema({"path": _S("string", "代码库根目录"), "max_files": _S("integer", "扫描上限(默认200)")}, ["path"]), "全库扫描（变更优先 UI 规则）"),
    "locate_edit": (_tool_locate_edit, _schema({
        "path": _S("string", "代码库根目录"),
        "query": _S("string", "符号名/关键词/报错片段（要改什么）"),
        "max_files": _S("integer", "扫描上限(默认200)"),
        "limit": _S("integer", "候选数(默认10)"),
    }, ["path", "query"]), "Qoder 式定位：自然语言→代码具体位置（file:line+符号+snippet，AI 改哪里的引导）"),
    "ds_lookup": (_tool_ds_lookup, _schema({}, []), "设计系统 token 查询（AI 生成 UI 时引用）"),
    "ds_check": (_tool_ds_check, _schema({"path": _S("string", ".rs 文件或目录"), "max_files": _S("integer", "扫描上限(默认200)")}, ["path"]), "设计系统合规检查（硬编码值/规则偏离）"),
    "std_check": (_tool_std_check, _schema({"path": _S("string", "文件或目录"), "max_files": _S("integer", "扫描上限(默认200)")}, ["path"]), "通用工程标准检查（占位文字/命名冲突/UI硬编码/魔法数字；默认标准兼容绝大多数项目）"),
}


_DEFS_CACHE: list | None = None


def _definitions() -> list:
    """工具定义（缓存）：list_tools 重复调用零重建（性能优化）。"""
    global _DEFS_CACHE
    if _DEFS_CACHE is None:
        _DEFS_CACHE = [
            _ToolDef(n, d, sc)
            for n, (_, sc, d) in _TOOLS.items()
        ] + _ext_definitions()
    return _DEFS_CACHE


# ─────────────────────────────────────────────────────────────
# 扩展层（懒加载合并：pr-oracle / tautest / code-analysis-enhance）
#   - 首次调用才加载（保持启动极简，内存基线最小）
#   - 工具名前缀：pr_oracle_* / tautest_* / cae_*
#   - 按路径加载（避免同名 server.py import 冲突）
#   - 加载失败：单工具返回错误文本，网关存活
# ─────────────────────────────────────────────────────────────
import importlib.util as _ilu

_EXT_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_EXT_LOADED: dict[str, object] = {}


def _load_ext(label: str) -> object | None:
    if label in _EXT_LOADED:
        return _EXT_LOADED[label]
    path = os.path.join(_EXT_BASE, label, "server.py")
    try:
        spec = _ilu.spec_from_file_location(f"unifiedrx_{label}", path)
        if spec is None or spec.loader is None:
            return None
        mod = _ilu.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _EXT_LOADED[label] = mod
        return mod
    except Exception as exc:
        print(f"[unified-rx] WARNING: 扩展 {label} 加载失败: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return None


def _ext_call(label: str, kind: str, name: str, arguments: dict) -> "list[types.TextContent]":
    """扩展调用统一入口：kind='pure'（_call 返回 str）或 'fn'（_tool_* 返回 list）。"""
    mod = _load_ext(label)
    if mod is None:
        return [_TC(f"Error: 扩展 {label} 不可用（加载失败）")]
    try:
        if kind == "pure":
            text = mod._call(name, arguments)
            return [_TC(text)]
        fn = getattr(mod, name)
        return fn(arguments)
    except Exception as exc:
        return [_TC(f"Error in {label}.{name}: {type(exc).__name__}: {exc}")]


_EXT_DEFS: dict[str, tuple] = {}


async def _ext_definitions_async() -> "list[types.Tool]":
    if not _EXT_DEFS:
        await _build_ext_defs()
    return [t for (_, _, t) in _EXT_DEFS.values()]


def _ext_definitions() -> "list[types.Tool]":
    """同步包装（selftest/测试用）；协议层走 _ext_definitions_async。"""
    if not _EXT_DEFS:
        import asyncio as _ai
        _ai.run(_build_ext_defs())
    return [t for (_, _, t) in _EXT_DEFS.values()]


async def _build_ext_defs() -> None:
    """构建扩展工具定义（pr-oracle 3 + tautest 4 + cae 13 = 20）。"""
    try:
        pr = _load_ext("pr-oracle")
        if pr is not None:
            for t in pr._tool_definitions():
                # pr-oracle 工具名已带 pr_oracle_ 前缀，直接注册
                _EXT_DEFS[t.name] = ("pr-oracle", "pure", t)
    except Exception as exc:
        print(f"[unified-rx] WARNING: pr-oracle 扩展定义构建失败: {exc}", file=sys.stderr)
    try:
        tt = _load_ext("tautest")
        if tt is not None:
            for t in tt._tool_definitions():
                # tautest 工具名已带 tautest_ 前缀，直接注册
                _EXT_DEFS[t.name] = ("tautest", "pure", t)
    except Exception as exc:
        print(f"[unified-rx] WARNING: tautest 扩展定义构建失败: {exc}", file=sys.stderr)
    try:
        cae = _load_ext("code-analysis-enhance")
        if cae is not None:
            # cae 用 FastMCP 装饰器（async list_tools）；工具名固定，直接建映射
            _CAE_TOOLS = [
                "file_dedup_state", "change_impact", "lesson_recall", "aether_probe",
                "code_context", "aether_agent_parse", "aether_lang_support",
                "aether_goto_parse", "lsp_position_convert", "lsp_semantic_tokens_decode",
                "lsp_edit_merge", "aether_model_provider", "lsp_query",
            ]
            # 工具名 → 实现函数名映射（file_dedup_state 的实现是 _tool_file_dedup）
            # 单一来源：_EXT_FN_MAP（模块级，_call_ext 同用）
            # 拉取 cae 真实 schema（async list_tools；本函数已是 async，直接 await）
            _cae_schemas: dict[str, dict] = {}
            if hasattr(cae, "list_tools"):
                try:
                    import asyncio as _ai
                    for t in await _ai.wait_for(cae.list_tools(), timeout=10):
                        _cae_schemas[t.name] = t.inputSchema
                except Exception:
                    _cae_schemas = {}
            for tn in _CAE_TOOLS:
                fn_name = _EXT_FN_MAP.get(tn, tn)
                if hasattr(cae, f"_tool_{fn_name}"):
                    _EXT_DEFS[f"cae_{tn}"] = (
                        "code-analysis-enhance", "fn",
                        _tool_proxy(tn, _cae_schemas.get(tn, {"type": "object", "properties": {}, "required": []})),
                    )
    except Exception as exc:
        print(f"[unified-rx] WARNING: code-analysis-enhance 扩展定义构建失败: {exc}", file=sys.stderr)


def _tool_proxy(tn: str, schema: dict | None = None):
    """构造带前缀的 Tool 定义（name=cae_{tn}，schema 从 cae 动态拉取）。"""
    return _ToolDef(f"cae_{tn}", f"code-analysis-enhance: {tn}", schema or {
        "type": "object", "properties": {}, "required": []})


def _call_ext(name: str, arguments: dict) -> "list[types.TextContent]":
    """扩展工具分发（name 带前缀）。"""
    if not _EXT_DEFS:
        # 防御：协议层 list_tools 已构建；直接调用（tool_card/selftest）可能未构建。
        # _build_ext_defs 是 async，用 _ext_definitions 的同步包装（asyncio.run）。
        _ext_definitions()
    if name not in _EXT_DEFS:
        return [_TC(f"Error: unknown tool: {name}")]
    label, kind, tdef = _EXT_DEFS[name]
    if kind == "pure":
        return _ext_call(label, "pure", tdef.name, arguments)
    # tdef.name 是 cae_{tn}；剥离前缀得 tn，再映射实现函数名
    tn = tdef.name.split("_", 1)[1] if tdef.name.startswith("cae_") else tdef.name
    fn_name = _EXT_FN_MAP.get(tn, tn)
    return _ext_call(label, "fn", f"_tool_{fn_name}", arguments)


# 工具名 → 实现函数名映射（单一来源；_build_ext_defs 与 _call_ext 共同消费）
_EXT_FN_MAP = {"file_dedup_state": "file_dedup"}


def _call(name: str, arguments: dict | None) -> "list[types.TextContent]":
    try:
        if name in _TOOLS:
            fn, _, _ = _TOOLS[name]
            result = fn(arguments or {})
            if isinstance(result, list):
                return result
            return [_TC(str(result))]
        if name.startswith(("pr_oracle_", "tautest_", "cae_")):
            return _call_ext(name, arguments or {})
        raise ValueError(f"unknown tool: {name}")
    except Exception as exc:
        return [_TC(f"Error: {exc}")]


# ─────────────────────────────────────────────────────────────
# MCP 协议层
# ─────────────────────────────────────────────────────────────

async def run() -> None:
    """协议层（懒加载 mcp 库 + 动态注册）：仅在真正启动时 import。"""
    import mcp.server.stdio
    import mcp.types as types
    from mcp.server import NotificationOptions, Server
    from mcp.server.models import InitializationOptions

    server = Server("unified-rx")

    @server.list_tools()
    async def handle_list_tools() -> list[types.Tool]:
        # 协议层必须用 async 版（_ext_definitions 同步版内部 asyncio.run 在事件循环内会炸）
        return [
            types.Tool(name=t.name, description=t.description, inputSchema=t.inputSchema)
            for t in _definitions()
        ] + await _ext_definitions_async()

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict | None) -> "list[types.TextContent]":
        out = _call(name, arguments)
        return [types.TextContent(type=getattr(c, "type", "text"), text=c.text) for c in out]

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="unified-rx",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def _selftest() -> None:
    # selftest 的 fs 用例写 server.py 同目录，需禁用沙盒（review 修复）
    os.environ["UNIFIED_RX_SANDBOX"] = ""
    # 重新锚定沙盒根（环境变量在 import 时读取）
    global _SANDBOX_ROOTS
    _SANDBOX_ROOTS = []
    start = time.perf_counter()
    n = len(_TOOLS)
    assert n == len(_definitions()) - len(_EXT_DEFS), "定义数不一致"
    # 抽样调用
    assert _call("math_add", {"a": 2, "b": 3})[0].text == "5"
    assert _call("str_reverse", {"s": "abc"})[0].text == "cba"
    assert _call("prime_is_prime", {"n": 17})[0].text == "true"
    assert _call("json_valid", {"json_string": "{bad}"})[0].text == "false"
    assert "Error" in _call("math_div", {"a": 1, "b": 0})[0].text
    # 文件层
    tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_selftest_tmp.txt")
    _tool_fs_write({"path": tmp, "content": "hello"})
    assert _tool_fs_read({"path": tmp})[0].text == "hello"
    os.remove(tmp)
    # bug_ 扫描/定位（抽样：扫描自身，验证分发与输出契约）
    d = json.loads(_call("bug_scan", {"path": os.path.abspath(__file__)})[0].text)
    assert d["ok"] and "issues" in d
    d2 = json.loads(_call("bug_locate", {"error_text": f'{os.path.abspath(__file__)}:1'})[0].text)
    assert d2["matched"]
    # 扩展层（懒加载验证）
    ext_n = len(_EXT_DEFS)
    # 扩展可能不可用（CI 无扩展/依赖）：仅在有扩展时断言其非空与分发
    if ext_n > 0:
        # 扩展工具能分发（不实际执行网络/子进程，只验证路由到扩展的错误处理）
        r = _call("pr_oracle_map_local", {"repo_path": os.path.dirname(os.path.abspath(__file__)),
                                          "changed_files": ["server.py"]})
        assert isinstance(r[0].text, str)
    elapsed = (time.perf_counter() - start) * 1000
    print(f"selftest passed: {n}+{ext_n}={n + ext_n} tools, {elapsed:.1f}ms")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        _selftest()
    else:
        asyncio.run(run())
